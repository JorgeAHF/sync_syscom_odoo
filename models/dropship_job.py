from odoo import _, api, fields, models


class SyncSyscomDropshipJob(models.Model):
    _name = "sync.syscom.dropship.job"
    _description = "Trabajo de configuración dropshipping SYSCOM"
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
    supplier_created = fields.Integer(string="Líneas proveedor creadas", default=0)
    supplier_updated = fields.Integer(string="Líneas proveedor actualizadas", default=0)
    routes_added = fields.Integer(string="Rutas triangulación agregadas", default=0)
    purchase_enabled = fields.Integer(string="Compras habilitadas", default=0)
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
            size = int(params.get_param("sync_syscom.dropship_batch_size") or self._default_batch_size())
        except Exception:
            size = self._default_batch_size()
        return max(size, 1)

    @api.model
    def create_configure_all_job(self):
        existing = self.search([("state", "in", ["pending", "running"])], order="create_date asc", limit=1)
        if existing:
            return existing
        job = self.create({"name": _("Regularizar dropshipping SYSCOM")})
        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo dropshipping creado"),
            "kind": "info",
            "message": _("Job %(job)s programado para configurar proveedor, precio proveedor y triangulación en productos SYSCOM.") % {
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
            "name": _("Trabajo dropshipping terminado"),
            "kind": "info",
            "message": _(
                "Job %(job)s terminado. Revisadas: %(processed)s/%(total)s. Proveedor creado: %(created)s, proveedor actualizado: %(updated)s, triangulación agregada: %(routes)s, compras habilitadas: %(purchase)s, omitidas: %(skipped)s."
            ) % {
                "job": self.display_name,
                "processed": self.processed_templates,
                "total": self.total_templates,
                "created": self.supplier_created,
                "updated": self.supplier_updated,
                "routes": self.routes_added,
                "purchase": self.purchase_enabled,
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
            "name": _("Trabajo dropshipping con error"),
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
        total = Template.search_count([
            ("syscom_is_product", "=", True),
            ("syscom_product_id", "!=", False),
        ])
        if total == 0:
            self._mark_done()
            return

        offset = int(self.template_offset or 0)
        if offset >= total:
            offset = 0
        batch_size = self._get_batch_size()
        templates = Template.search([
            ("syscom_is_product", "=", True),
            ("syscom_product_id", "!=", False),
        ], order="id asc", offset=offset, limit=batch_size)

        created = updated = routes = purchase = skipped = 0
        for template in templates:
            result = Product._ensure_syscom_procurement_setup(template, vendor_price=template.standard_price)
            if not any(result.values()):
                skipped += 1
                continue
            if result.get("supplier_created"):
                created += 1
            if result.get("supplier_updated"):
                updated += 1
            if result.get("route_added"):
                routes += 1
            if result.get("purchase_enabled"):
                purchase += 1

        next_offset = offset + len(templates)
        done = next_offset >= total
        self.write({
            "template_offset": 0 if done else next_offset,
            "total_templates": total,
            "processed_templates": self.processed_templates + len(templates),
            "supplier_created": self.supplier_created + created,
            "supplier_updated": self.supplier_updated + updated,
            "routes_added": self.routes_added + routes,
            "purchase_enabled": self.purchase_enabled + purchase,
            "skipped_templates": self.skipped_templates + skipped,
        })

        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo dropshipping (batch)"),
            "kind": "info",
            "message": _(
                "Job %(job)s batch. Revisadas: %(processed)s, proveedor creado: %(created)s, proveedor actualizado: %(updated)s, triangulación agregada: %(routes)s, compras habilitadas: %(purchase)s, omitidas: %(skipped)s. Offset: %(offset)s/%(total)s."
            ) % {
                "job": self.display_name,
                "processed": len(templates),
                "created": created,
                "updated": updated,
                "routes": routes,
                "purchase": purchase,
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
            FROM sync_syscom_dropship_job
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
    def cron_process_dropship_jobs(self):
        job = self._claim_next_job()
        if not job:
            return
        try:
            job._process_batch()
        except Exception as exc:
            job._mark_error(str(exc))
