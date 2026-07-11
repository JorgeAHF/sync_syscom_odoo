from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .constants import DEFAULT_CATEGORY_PUBLISH_PRODUCT_CHUNK


class SyncSyscomPublishJob(models.Model):
    _name = "sync.syscom.publish.job"
    _description = "Trabajo de publicación SYSCOM"
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
    include_children = fields.Boolean(string="Incluir subcategorías", default=True)
    category_ids = fields.Many2many(
        "sync.syscom.category",
        "sync_syscom_publish_job_category_rel",
        "job_id",
        "category_id",
        string="Categorías base",
    )
    scope_category_ids = fields.Many2many(
        "sync.syscom.category",
        "sync_syscom_publish_job_scope_category_rel",
        "job_id",
        "category_id",
        string="Categorías en alcance",
    )
    product_ids = fields.Many2many(
        "sync.syscom.product",
        "sync_syscom_publish_job_product_rel",
        "job_id",
        "product_id",
        string="Productos objetivo",
    )
    product_offset = fields.Integer(string="Offset productos", default=0)
    total_products = fields.Integer(string="Total productos")
    processed_products = fields.Integer(string="Productos revisados")
    queued_products = fields.Integer(string="Productos en cola")
    started_at = fields.Datetime(string="Inicio")
    finished_at = fields.Datetime(string="Fin")
    last_error = fields.Text(string="Último error")

    @property
    def _job_source_label(self):
        return "Categorias (%s)" % ", ".join(self.scope_category_ids.mapped("syscom_id"))

    @classmethod
    def _chunk_limit_default(cls):
        return DEFAULT_CATEGORY_PUBLISH_PRODUCT_CHUNK

    def _get_product_chunk_limit(self):
        params = self.env["ir.config_parameter"].sudo()
        try:
            limit = int(params.get_param("sync_syscom.category_publish_product_chunk_limit") or self._chunk_limit_default())
        except (TypeError, ValueError):
            limit = self._chunk_limit_default()
        return max(limit, 1)

    @api.model
    def create_for_categories(self, categories, include_children=True):
        categories = categories.exists()
        if not categories:
            raise UserError(_("Selecciona al menos una categoría para publicar."))

        scope_categories = categories._get_scope_categories(include_children=bool(include_children))
        products = self.env["sync.syscom.product"].search([
            ("category_ids", "in", scope_categories.ids),
            ("active", "=", True),
        ], order="id asc")
        if not products:
            raise UserError(_("No hay modelos SYSCOM en staging para las categorías seleccionadas. Primero sincroniza catálogos."))

        job = self.create({
            "name": _("Publicación categorías: %(cats)s") % {
                "cats": ", ".join(categories.mapped("syscom_id")),
            },
            "include_children": bool(include_children),
            "category_ids": [(6, 0, categories.ids)],
            "scope_category_ids": [(6, 0, scope_categories.ids)],
            "product_ids": [(6, 0, products.ids)],
            "total_products": len(products),
        })
        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo publicación categorías creado"),
            "kind": "info",
            "message": _(
                "Job %(job)s programado. Categorías base: %(base)s. Scope: %(scope)s (subcategorías=%(child)s). Productos locales: %(products)s."
            ) % {
                "job": job.display_name,
                "base": ", ".join(categories.mapped("syscom_id")),
                "scope": len(scope_categories),
                "child": "sí" if include_children else "no",
                "products": len(products),
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
            "name": _("Trabajo publicación categorías terminado"),
            "kind": "info",
            "message": _(
                "Job %(job)s terminado. Productos revisados: %(processed)s/%(total)s. Encolados: %(queued)s."
            ) % {
                "job": self.display_name,
                "processed": self.processed_products,
                "total": self.total_products,
                "queued": self.queued_products,
            },
        })

    def _mark_error(self, message):
        self.ensure_one()
        self.write({
            "state": "error",
            "finished_at": fields.Datetime.now(),
            "last_error": message,
        })
        subject = _("Trabajo publicación categorías con error")
        full_message = "%s: %s" % (self.display_name, message)
        self.env["sync.syscom.log"].sudo().notify_admin_on_critical_error(subject, full_message)

    def _process_batch(self):
        self.ensure_one()
        if self.state in ("done", "error"):
            return
        if not self.scope_category_ids:
            self._mark_error(_("El trabajo no tiene categorías en alcance."))
            return
        product_model = self.env["sync.syscom.product"]
        products = self.product_ids.sorted(key=lambda prod: prod.id)
        total = len(products)
        if total == 0:
            self._mark_error(_("El trabajo no tiene productos locales para publicar."))
            return

        if self.state == "pending":
            self.write({
                "state": "running",
                "started_at": fields.Datetime.now(),
                "last_error": False,
            })

        offset = int(self.product_offset or 0)
        if offset >= total:
            offset = 0
        chunk_limit = self._get_product_chunk_limit()
        batch_products = products[offset : offset + chunk_limit]
        batch_processed = len(batch_products)
        queued = product_model.queue_products_for_background_publish(
            batch_products,
            source_label=self._job_source_label,
        ) if batch_products else 0

        next_offset = offset + batch_processed
        done = next_offset >= total
        self.write({
            "product_offset": 0 if done else next_offset,
            "total_products": total,
            "processed_products": self.processed_products + batch_processed,
            "queued_products": self.queued_products + queued,
        })

        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo publicación categorías (batch)"),
            "kind": "info",
            "message": _(
                "Job %(job)s batch. Productos revisados: %(processed)s, en cola: %(queued)s. Offset: %(offset)s/%(total)s."
            ) % {
                "job": self.display_name,
                "processed": batch_processed,
                "queued": queued,
                "offset": 0 if done else next_offset,
                "total": total,
            },
        })

        if done:
            self._mark_done()

    @api.model
    def cron_process_publish_jobs(self):
        jobs = self.search([("state", "in", ["pending", "running"])], order="create_date asc", limit=1)
        for job in jobs:
            try:
                job._process_batch()
            except Exception as exc:
                job._mark_error(str(exc))
