from odoo import _, api, fields, models

from .constants import DEFAULT_PRODUCT_DATA_BATCH_SIZE


class SyncSyscomProductDataJob(models.Model):
    _name = "sync.syscom.product.data.job"
    _description = "Trabajo de enriquecimiento de productos SYSCOM"
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
    product_offset = fields.Integer(string="Offset productos", default=0)
    total_products = fields.Integer(string="Total productos", default=0)
    processed_products = fields.Integer(string="Productos revisados", default=0)
    updated_products = fields.Integer(string="Staging actualizados", default=0)
    updated_templates = fields.Integer(string="Plantillas actualizadas", default=0)
    remote_fetches = fields.Integer(string="Detalles consultados a SYSCOM", default=0)
    skipped_products = fields.Integer(string="Productos omitidos", default=0)
    started_at = fields.Datetime(string="Inicio")
    finished_at = fields.Datetime(string="Fin")
    last_error = fields.Text(string="Último error")

    @classmethod
    def _default_batch_size(cls):
        return DEFAULT_PRODUCT_DATA_BATCH_SIZE

    def _get_batch_size(self):
        params = self.env["ir.config_parameter"].sudo()
        try:
            size = int(params.get_param("sync_syscom.product_data_batch_size") or self._default_batch_size())
        except (TypeError, ValueError):
            size = self._default_batch_size()
        return max(size, 1)

    @api.model
    def create_sync_all_job(self):
        existing = self.search([("state", "in", ["pending", "running"])], order="create_date asc", limit=1)
        if existing:
            return existing
        job = self.create({"name": _("Enriquecer datos extendidos de productos SYSCOM")})
        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo datos extendidos creado"),
            "kind": "info",
            "message": _("Job %(job)s programado para enriquecer garantía, dimensiones, peso y características.") % {
                "job": job.display_name,
            },
        })
        return job

    def _mark_done(self):
        self.ensure_one()
        self.write({
            "state": "done",
            "product_offset": 0,
            "finished_at": fields.Datetime.now(),
            "last_error": False,
        })
        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo datos extendidos terminado"),
            "kind": "info",
            "message": _(
                "Job %(job)s terminado. Revisados: %(processed)s/%(total)s. Staging: %(products)s. Plantillas: %(templates)s. Consultas remotas: %(remote)s. Omitidos: %(skipped)s."
            ) % {
                "job": self.display_name,
                "processed": self.processed_products,
                "total": self.total_products,
                "products": self.updated_products,
                "templates": self.updated_templates,
                "remote": self.remote_fetches,
                "skipped": self.skipped_products,
            },
        })

    def _mark_error(self, message):
        self.ensure_one()
        self.write({
            "state": "error",
            "finished_at": fields.Datetime.now(),
            "last_error": message,
        })
        subject = _("Trabajo datos extendidos con error")
        full_message = "%s: %s" % (self.display_name, message)
        self.env["sync.syscom.log"].sudo().notify_admin_on_critical_error(subject, full_message)

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

        Product = self.env["sync.syscom.product"]
        total = Product.search_count([])
        if total == 0:
            self._mark_done()
            return

        offset = int(self.product_offset or 0)
        if offset >= total:
            offset = 0
        batch_size = self._get_batch_size()
        batch_products = Product.search([], order="id asc", offset=offset, limit=batch_size)

        client = None
        updated_products = 0
        updated_templates = 0
        remote_fetches = 0
        skipped_products = 0

        for product in batch_products:
            detail = product.payload if isinstance(product.payload, dict) else {}
            if not Product._detail_has_extended_values(detail):
                client = client or Product._get_client()
                detail = client.get_product_detail(product.syscom_id) or {}
                remote_fetches += 1
            if not Product._detail_has_extended_values(detail):
                skipped_products += 1
                continue

            Product._apply_extended_values_to_product(product, detail)
            if isinstance(detail, dict):
                product.write({"payload": detail, "synced_at": fields.Datetime.now(), "sync_error": False})
            updated_products += 1

            template = Product._find_template_for_existing_product(product)
            if template:
                Product._apply_extended_values_to_template(template, detail, staging_product=product)
                updated_templates += 1

        next_offset = offset + len(batch_products)
        done = next_offset >= total
        self.write({
            "product_offset": 0 if done else next_offset,
            "total_products": total,
            "processed_products": self.processed_products + len(batch_products),
            "updated_products": self.updated_products + updated_products,
            "updated_templates": self.updated_templates + updated_templates,
            "remote_fetches": self.remote_fetches + remote_fetches,
            "skipped_products": self.skipped_products + skipped_products,
        })

        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo datos extendidos (batch)"),
            "kind": "info",
            "message": _(
                "Job %(job)s batch. Revisados: %(processed)s, staging: %(products)s, plantillas: %(templates)s, remoto: %(remote)s, omitidos: %(skipped)s. Offset: %(offset)s/%(total)s."
            ) % {
                "job": self.display_name,
                "processed": len(batch_products),
                "products": updated_products,
                "templates": updated_templates,
                "remote": remote_fetches,
                "skipped": skipped_products,
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
            FROM sync_syscom_product_data_job
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
    def cron_process_product_data_jobs(self):
        job = self._claim_next_job()
        if not job:
            return
        try:
            job._process_batch()
        except Exception as exc:
            job._mark_error(str(exc))
