from odoo import _, api, fields, models


class SyncSyscomCostJob(models.Model):
    _name = "sync.syscom.cost.job"
    _description = "Trabajo de recálculo de costos SYSCOM"
    _order = "create_date desc"

    name = fields.Char(string="Nombre", required=True)
    state = fields.Selection(
        [
            ("pending", "Pendiente"),
            ("running", "Procesando"),
            ("done", "Terminado"),
            ("error", "Error"),
        ],
        string="Estado",
        default="pending",
        required=True,
        index=True,
    )
    template_offset = fields.Integer(string="Offset plantillas", default=0)
    total_templates = fields.Integer(string="Total plantillas", default=0)
    processed_templates = fields.Integer(string="Plantillas revisadas", default=0)
    updated_templates = fields.Integer(string="Costos actualizados", default=0)
    skipped_templates = fields.Integer(string="Plantillas omitidas", default=0)
    started_at = fields.Datetime(string="Inicio")
    finished_at = fields.Datetime(string="Fin")
    last_error = fields.Text(string="Último error")

    @classmethod
    def _default_batch_size(cls):
        return 200

    def _get_batch_size(self):
        params = self.env["ir.config_parameter"].sudo()
        try:
            size = int(params.get_param("sync_syscom.cost_recompute_batch_size") or self._default_batch_size())
        except Exception:
            size = self._default_batch_size()
        return max(size, 1)

    @api.model
    def create_recompute_all_job(self):
        existing = self.search([("state", "in", ["pending", "running"])], order="create_date asc", limit=1)
        if existing:
            return existing
        job = self.create({"name": _("Recálculo masivo de costos SYSCOM")})
        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo recálculo de costos creado"),
            "kind": "info",
            "message": _("Job %(job)s programado para recalcular costos de productos SYSCOM existentes.") % {
                "job": job.display_name,
            },
        })
        return job

    def _mark_done(self):
        self.ensure_one()
        self.write({
            "state": "done",
            "template_offset": 0,
            "finished_at": fields.Datetime.now(),
            "last_error": False,
        })
        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo recálculo de costos terminado"),
            "kind": "info",
            "message": _(
                "Job %(job)s terminado. Revisadas: %(processed)s/%(total)s. Actualizadas: %(updated)s. Omitidas: %(skipped)s."
            ) % {
                "job": self.display_name,
                "processed": self.processed_templates,
                "total": self.total_templates,
                "updated": self.updated_templates,
                "skipped": self.skipped_templates,
            },
        })

    def _mark_error(self, message):
        self.ensure_one()
        self.write({
            "state": "error",
            "finished_at": fields.Datetime.now(),
            "last_error": message,
        })
        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo recálculo de costos con error"),
            "kind": "error",
            "message": "%s: %s" % (self.display_name, message),
        })

    def _process_batch(self):
        self.ensure_one()
        if self.state in ("done", "error"):
            return

        if self.state == "pending":
            self.write({
                "state": "running",
                "started_at": fields.Datetime.now(),
                "last_error": False,
            })

        Template = self.env["product.template"].sudo()
        Product = self.env["sync.syscom.product"]
        params = self.env["ir.config_parameter"].sudo()

        templates = Template.search([
            ("syscom_is_product", "=", True),
            ("syscom_product_id", "!=", False),
        ], order="id asc")
        total = len(templates)
        if total == 0:
            self._mark_done()
            return

        offset = int(self.template_offset or 0)
        if offset >= total:
            offset = 0
        batch_size = self._get_batch_size()
        batch_templates = templates[offset : offset + batch_size]
        staging_map = {
            (prod.syscom_id or "").strip(): prod
            for prod in Product.search([
                ("syscom_id", "in", [tmpl.syscom_product_id for tmpl in batch_templates if tmpl.syscom_product_id]),
            ])
        }

        updated = 0
        skipped = 0
        for tmpl in batch_templates:
            staging_product = staging_map.get((tmpl.syscom_product_id or "").strip())
            if Product._recompute_syscom_template_cost(tmpl, staging_product=staging_product, params=params):
                updated += 1
            else:
                skipped += 1

        next_offset = offset + len(batch_templates)
        done = next_offset >= total
        self.write({
            "template_offset": 0 if done else next_offset,
            "total_templates": total,
            "processed_templates": self.processed_templates + len(batch_templates),
            "updated_templates": self.updated_templates + updated,
            "skipped_templates": self.skipped_templates + skipped,
        })

        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo recálculo de costos (batch)"),
            "kind": "info",
            "message": _(
                "Job %(job)s batch. Revisadas: %(processed)s, actualizadas: %(updated)s, omitidas: %(skipped)s. Offset: %(offset)s/%(total)s."
            ) % {
                "job": self.display_name,
                "processed": len(batch_templates),
                "updated": updated,
                "skipped": skipped,
                "offset": 0 if done else next_offset,
                "total": total,
            },
        })

        if done:
            self._mark_done()

    @api.model
    def _claim_next_job(self):
        self.env.cr.execute(
            """
            SELECT id
            FROM sync_syscom_cost_job
            WHERE state IN ('pending', 'running')
            ORDER BY create_date ASC, id ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        )
        row = self.env.cr.fetchone()
        if not row:
            return self.browse()
        return self.browse(row[0])

    @api.model
    def cron_process_cost_jobs(self):
        job = self._claim_next_job()
        if not job:
            return
        try:
            job._process_batch()
        except Exception as exc:
            job._mark_error(str(exc))
