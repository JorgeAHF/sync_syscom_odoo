from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .syscom_client import SyscomClient


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
    brand_offset = fields.Integer(string="Offset marcas", default=0)
    total_brands = fields.Integer(string="Total marcas")
    processed_brands = fields.Integer(string="Marcas revisadas")
    matched_brands = fields.Integer(string="Marcas aplicables")
    skipped_brands = fields.Integer(string="Marcas omitidas")
    timeout_brands = fields.Integer(string="Marcas con timeout")
    created_products = fields.Integer(string="Modelos creados")
    updated_products = fields.Integer(string="Modelos actualizados")
    queued_products = fields.Integer(string="Productos en cola")
    started_at = fields.Datetime(string="Inicio")
    finished_at = fields.Datetime(string="Fin")
    last_error = fields.Text(string="Último error")

    @property
    def _job_source_label(self):
        return "Categorias (%s)" % ", ".join(self.scope_category_ids.mapped("syscom_id"))

    @classmethod
    def _chunk_limit_default(cls):
        return 5

    def _get_client(self):
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("Configura el token en Ajustes antes de publicar por categoría."))
        base_url = params.get_param("sync_syscom.syscom_base_url") or "https://developers.syscom.mx/api/v1"
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or 30)
        return SyscomClient(base_url=base_url, token=token, timeout=timeout)

    def _get_brand_chunk_limit(self):
        params = self.env["ir.config_parameter"].sudo()
        try:
            limit = int(params.get_param("sync_syscom.category_publish_brand_chunk_limit") or self._chunk_limit_default())
        except Exception:
            limit = self._chunk_limit_default()
        return max(limit, 1)

    @api.model
    def create_for_categories(self, categories, include_children=True):
        categories = categories.exists()
        if not categories:
            raise UserError(_("Selecciona al menos una categoría para publicar."))

        scope_categories = categories._get_scope_categories(include_children=bool(include_children))
        job = self.create({
            "name": _("Publicación categorías: %(cats)s") % {
                "cats": ", ".join(categories.mapped("syscom_id")),
            },
            "include_children": bool(include_children),
            "category_ids": [(6, 0, categories.ids)],
            "scope_category_ids": [(6, 0, scope_categories.ids)],
        })
        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo publicación categorías creado"),
            "kind": "info",
            "message": _(
                "Job %(job)s programado. Categorías base: %(base)s. Scope: %(scope)s (subcategorías=%(child)s)."
            ) % {
                "job": job.display_name,
                "base": ", ".join(categories.mapped("syscom_id")),
                "scope": len(scope_categories),
                "child": "sí" if include_children else "no",
            },
        })
        return job

    def _mark_done(self):
        self.ensure_one()
        self.write({
            "state": "done",
            "brand_offset": 0,
            "finished_at": fields.Datetime.now(),
            "last_error": False,
        })
        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo publicación categorías terminado"),
            "kind": "info",
            "message": _(
                "Job %(job)s terminado. Marcas revisadas: %(processed)s/%(total)s, aplicables: %(matched)s, "
                "omitidas: %(skipped)s, timeout: %(timeout)s, modelos creados: %(created)s, actualizados: %(updated)s, en cola: %(queued)s."
            ) % {
                "job": self.display_name,
                "processed": self.processed_brands,
                "total": self.total_brands,
                "matched": self.matched_brands,
                "skipped": self.skipped_brands,
                "timeout": self.timeout_brands,
                "created": self.created_products,
                "updated": self.updated_products,
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
        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo publicación categorías con error"),
            "kind": "error",
            "message": "%s: %s" % (self.display_name, message),
        })

    def _process_batch(self):
        self.ensure_one()
        if self.state in ("done", "error"):
            return
        if not self.scope_category_ids:
            self._mark_error(_("El trabajo no tiene categorías en alcance."))
            return

        client = self._get_client()
        brand_model = self.env["sync.syscom.brand"]
        product_model = self.env["sync.syscom.product"]

        if self.state == "pending":
            self.write({
                "state": "running",
                "started_at": fields.Datetime.now(),
                "last_error": False,
            })

        all_brands = client.get_brands() or []
        total = len(all_brands)
        if total == 0:
            self.write({"total_brands": 0})
            self._mark_done()
            return

        offset = int(self.brand_offset or 0)
        if offset >= total:
            offset = 0
        chunk_limit = self._get_brand_chunk_limit()
        slice_brands = all_brands[offset : offset + chunk_limit]
        allowed_ids = set(self.scope_category_ids.mapped("syscom_id"))

        products_to_queue = product_model.browse([])
        batch_processed = len(slice_brands)
        batch_matched = 0
        batch_skipped = 0
        batch_timeout = 0
        batch_created = 0
        batch_updated = 0

        for brand_payload in slice_brands:
            try:
                stats = brand_model._sync_single_brand_for_scope(
                    client,
                    brand_payload,
                    allowed_category_syscom_ids=allowed_ids,
                )
            except Exception as exc:
                batch_timeout += 1
                self.env["sync.syscom.log"].sudo().create({
                    "name": _("Trabajo publicación categorías"),
                    "kind": "error",
                    "message": _("Error procesando marca en %(job)s: %(err)s") % {
                        "job": self.display_name,
                        "err": exc,
                    },
                })
                continue

            status = stats.get("status")
            if status == "matched":
                batch_matched += 1
                batch_created += stats.get("created", 0)
                batch_updated += stats.get("updated", 0)
                products_to_queue |= stats.get("products", product_model.browse([]))
            elif status == "timeout":
                batch_timeout += 1
            else:
                batch_skipped += 1

        queued = product_model.queue_products_for_background_publish(products_to_queue, source_label=self._job_source_label) if products_to_queue else 0

        next_offset = offset + batch_processed
        done = next_offset >= total
        self.write({
            "brand_offset": 0 if done else next_offset,
            "total_brands": total,
            "processed_brands": self.processed_brands + batch_processed,
            "matched_brands": self.matched_brands + batch_matched,
            "skipped_brands": self.skipped_brands + batch_skipped,
            "timeout_brands": self.timeout_brands + batch_timeout,
            "created_products": self.created_products + batch_created,
            "updated_products": self.updated_products + batch_updated,
            "queued_products": self.queued_products + queued,
        })

        self.env["sync.syscom.log"].sudo().create({
            "name": _("Trabajo publicación categorías (batch)"),
            "kind": "info",
            "message": _(
                "Job %(job)s batch. Revisadas: %(processed)s, aplicables: %(matched)s, omitidas: %(skipped)s, "
                "timeout: %(timeout)s, modelos creados: %(created)s, actualizados: %(updated)s, en cola: %(queued)s. Offset: %(offset)s/%(total)s."
            ) % {
                "job": self.display_name,
                "processed": batch_processed,
                "matched": batch_matched,
                "skipped": batch_skipped,
                "timeout": batch_timeout,
                "created": batch_created,
                "updated": batch_updated,
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
