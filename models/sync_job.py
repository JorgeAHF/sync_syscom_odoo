from odoo import _, api, fields, models
from odoo.exceptions import UserError


JOB_FLOWS = {
    "categories_only": ["categories"],
    "full_catalog": ["categories", "brands", "brand_products"],
    "brands_products": ["brands", "brand_products"],
}


class SyncSyscomSyncJob(models.Model):
    _name = "sync.syscom.sync.job"
    _description = "Trabajo de sincronización SYSCOM"
    _order = "create_date desc"

    name = fields.Char(string="Nombre", required=True)
    job_type = fields.Selection(
        [
            ("categories_only", "Solo categorías"),
            ("full_catalog", "Catálogo completo"),
            ("brands_products", "Marcas y modelos"),
        ],
        string="Tipo",
        required=True,
        index=True,
    )
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
    stage = fields.Selection(
        [
            ("categories", "Categorías"),
            ("brands", "Marcas"),
            ("brand_products", "Modelos"),
        ],
        string="Etapa",
        required=True,
        index=True,
    )
    started_at = fields.Datetime(string="Inicio")
    finished_at = fields.Datetime(string="Fin")
    last_error = fields.Text(string="Último error")

    category_offset = fields.Integer(string="Offset categorías", default=0)
    brand_offset = fields.Integer(string="Offset marcas", default=0)
    product_offset = fields.Integer(string="Offset modelos", default=0)

    total_categories = fields.Integer(string="Total categorías", default=0)
    processed_categories = fields.Integer(string="Categorías revisadas", default=0)
    created_categories = fields.Integer(string="Categorías creadas", default=0)
    updated_categories = fields.Integer(string="Categorías actualizadas", default=0)

    total_brands = fields.Integer(string="Total marcas", default=0)
    processed_brands = fields.Integer(string="Marcas revisadas", default=0)
    created_brands = fields.Integer(string="Marcas creadas", default=0)
    updated_brands = fields.Integer(string="Marcas actualizadas", default=0)
    skipped_brand_timeouts = fields.Integer(string="Marcas timeout", default=0)

    total_product_brands = fields.Integer(string="Total marcas con modelos", default=0)
    processed_product_brands = fields.Integer(string="Marcas con modelos revisadas", default=0)
    created_products = fields.Integer(string="Modelos creados", default=0)
    updated_products = fields.Integer(string="Modelos actualizados", default=0)
    fetched_products = fields.Integer(string="Modelos leídos", default=0)
    product_errors = fields.Integer(string="Errores de modelos", default=0)

    @api.model
    def _flow_for(self, job_type):
        return JOB_FLOWS.get(job_type, ["categories"])

    def _next_stage(self):
        self.ensure_one()
        flow = self._flow_for(self.job_type)
        try:
            index = flow.index(self.stage)
        except ValueError:
            return False
        if index + 1 >= len(flow):
            return False
        return flow[index + 1]

    @api.model
    def _create_job(self, job_type, name):
        existing = self.search(
            [("job_type", "=", job_type), ("state", "in", ["pending", "running"])],
            order="create_date asc",
            limit=1,
        )
        if existing:
            return existing

        job = self.create({
            "name": name,
            "job_type": job_type,
            "stage": self._flow_for(job_type)[0],
        })
        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo de sync creado"),
            "kind": "info",
            "message": _("Job %(job)s programado. Tipo: %(type)s.") % {
                "job": job.display_name,
                "type": dict(self._fields["job_type"].selection).get(job.job_type),
            },
        })
        return job

    @api.model
    def create_categories_only_job(self):
        return self._create_job("categories_only", _("Sync categorías"))

    @api.model
    def create_full_catalog_job(self):
        return self._create_job("full_catalog", _("Sync catálogo completo"))

    @api.model
    def create_brands_products_job(self):
        return self._create_job("brands_products", _("Sync marcas y modelos"))

    def _mark_done(self):
        self.ensure_one()
        self.write({
            "state": "done",
            "finished_at": fields.Datetime.now(),
            "last_error": False,
        })
        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo de sync terminado"),
            "kind": "info",
            "message": _(
                "Job %(job)s terminado. Categorías: %(pc)s/%(tc)s. Marcas: %(pb)s/%(tb)s. Modelos: %(pp)s/%(tp)s."
            ) % {
                "job": self.display_name,
                "pc": self.processed_categories,
                "tc": self.total_categories,
                "pb": self.processed_brands,
                "tb": self.total_brands,
                "pp": self.processed_product_brands,
                "tp": self.total_product_brands,
            },
        })

    def _mark_error(self, message):
        self.ensure_one()
        self.write({
            "state": "error",
            "finished_at": fields.Datetime.now(),
            "last_error": message,
        })
        subject = _("Trabajo de sync con error")
        full_message = "%s: %s" % (self.display_name, message)
        self.env["sync.syscom.log"].sudo().notify_admin_on_critical_error(subject, full_message)

    def _start_if_needed(self):
        self.ensure_one()
        if self.state == "pending":
            self.write({
                "state": "running",
                "started_at": fields.Datetime.now(),
                "last_error": False,
            })

    def _advance_or_finish(self):
        self.ensure_one()
        next_stage = self._next_stage()
        if not next_stage:
            self._mark_done()
            return
        self.write({"stage": next_stage})
        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo de sync avanza de etapa"),
            "kind": "info",
            "message": _("Job %(job)s ahora procesa etapa %(stage)s.") % {
                "job": self.display_name,
                "stage": dict(self._fields["stage"].selection).get(next_stage),
            },
        })

    def _process_categories_stage(self):
        self.ensure_one()
        batch = self.env["sync.syscom.category"]._sync_categories_batch(offset=self.category_offset)
        self.write({
            "total_categories": batch["total"],
            "processed_categories": self.processed_categories + batch["processed"],
            "created_categories": self.created_categories + batch["created"],
            "updated_categories": self.updated_categories + batch["updated"],
            "category_offset": batch["next_offset"],
        })
        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo sync categorías (batch)"),
            "kind": "info",
            "message": _(
                "Job %(job)s batch categorías. Revisadas: %(processed)s, creadas: %(created)s, actualizadas: %(updated)s. Offset: %(offset)s/%(total)s."
            ) % {
                "job": self.display_name,
                "processed": batch["processed"],
                "created": batch["created"],
                "updated": batch["updated"],
                "offset": batch["next_offset"],
                "total": batch["total"],
            },
        })
        if batch["finished"]:
            self._advance_or_finish()

    def _process_brands_stage(self):
        self.ensure_one()
        batch = self.env["sync.syscom.brand"]._sync_brands_batch(offset=self.brand_offset)
        self.write({
            "total_brands": batch["total"],
            "processed_brands": self.processed_brands + batch["processed"],
            "created_brands": self.created_brands + batch["created"],
            "updated_brands": self.updated_brands + batch["updated"],
            "skipped_brand_timeouts": self.skipped_brand_timeouts + batch["timeout_skip"],
            "brand_offset": batch["next_offset"],
        })
        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo sync marcas (batch)"),
            "kind": "info",
            "message": _(
                "Job %(job)s batch marcas. Revisadas: %(processed)s, creadas: %(created)s, actualizadas: %(updated)s, timeout: %(timeout)s. Offset: %(offset)s/%(total)s."
            ) % {
                "job": self.display_name,
                "processed": batch["processed"],
                "created": batch["created"],
                "updated": batch["updated"],
                "timeout": batch["timeout_skip"],
                "offset": batch["next_offset"],
                "total": batch["total"],
            },
        })
        if batch["finished"]:
            self._advance_or_finish()

    def _process_brand_products_stage(self):
        self.ensure_one()
        batch = self.env["sync.syscom.brand"]._sync_local_brand_products_batch(offset=self.product_offset)
        self.write({
            "total_product_brands": batch["total"],
            "processed_product_brands": self.processed_product_brands + batch["processed"],
            "created_products": self.created_products + batch["created_products"],
            "updated_products": self.updated_products + batch["updated_products"],
            "fetched_products": self.fetched_products + batch["fetched_products"],
            "product_errors": self.product_errors + batch["errors"],
            "product_offset": batch["next_offset"],
        })
        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo sync modelos (batch)"),
            "kind": "info",
            "message": _(
                "Job %(job)s batch modelos. Marcas revisadas: %(processed)s, modelos leídos: %(fetched)s, creados: %(created)s, actualizados: %(updated)s, errores: %(errors)s. Offset: %(offset)s/%(total)s."
            ) % {
                "job": self.display_name,
                "processed": batch["processed"],
                "fetched": batch["fetched_products"],
                "created": batch["created_products"],
                "updated": batch["updated_products"],
                "errors": batch["errors"],
                "offset": batch["next_offset"],
                "total": batch["total"],
            },
        })
        if batch["finished"]:
            self._advance_or_finish()

    def _process_batch(self):
        self.ensure_one()
        if self.state in ("done", "error"):
            return
        self._start_if_needed()
        if self.stage == "categories":
            self._process_categories_stage()
            return
        if self.stage == "brands":
            self._process_brands_stage()
            return
        if self.stage == "brand_products":
            self._process_brand_products_stage()
            return
        self._mark_error(_("Etapa desconocida: %s") % self.stage)

    @api.model
    def _claim_next_job(self):
        self.env.cr.execute(
            """
            SELECT id
            FROM sync_syscom_sync_job
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
    def cron_process_sync_jobs(self):
        job = self._claim_next_job()
        if not job:
            return
        try:
            job._process_batch()
        except Exception as exc:
            job._mark_error(str(exc))
