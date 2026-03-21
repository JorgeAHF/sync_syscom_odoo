import time

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .syscom_client import SyscomClient


class SyscomCategory(models.Model):
    _name = "sync.syscom.category"
    _description = "Categoría SYSCOM"
    _order = "name"

    name = fields.Char(string="Nombre", required=True)
    syscom_id = fields.Char(string="ID SYSCOM", required=True, index=True)
    level = fields.Integer(string="Nivel")
    active = fields.Boolean(string="Activo", default=True)
    parent_id = fields.Many2one(
        "sync.syscom.category",
        string="Categoría padre",
        ondelete="set null",
    )
    child_ids = fields.One2many(
        "sync.syscom.category",
        "parent_id",
        string="Subcategorías",
    )
    product_category_id = fields.Many2one(
        "product.category",
        string="Categoría Odoo",
        ondelete="set null",
        help="Categoría equivalente en Odoo para asignar a productos publicados.",
    )
    public_category_id = fields.Many2one(
        "product.public.category",
        string="Categoría eCommerce",
        ondelete="set null",
        help="Categoría equivalente en eCommerce (product.public.category).",
    )
    syscom_sequence = fields.Integer(
        string="Orden SYSCOM",
        default=10,
        help="Orden relativo dentro de sus hermanas (para replicar el orden en eCommerce).",
    )
    brand_ids = fields.Many2many(
        "sync.syscom.brand",
        "sync_syscom_brand_category_rel",
        "category_id",
        "brand_id",
        string="Marcas",
        help="Vínculos directos devueltos por SYSCOM en /marcas/{id}.",
    )
    selected = fields.Boolean(
        string="Lote",
        default=False,
        help="Marca persistente para procesos batch manuales. No equivale a la selección visual de la vista.",
    )
    product_ids = fields.Many2many(
        "sync.syscom.product",
        "sync_syscom_category_product_rel",
        "category_id",
        "product_id",
        string="Modelos vinculados",
    )
    brand_ids_tree = fields.Many2many(
        "sync.syscom.brand",
        compute="_compute_brand_ids_tree",
        string="Marcas heredadas",
        store=False,
        help="Marcas directas de la categoría y de todas sus descendientes (solo visual).",
    )
    model_names = fields.Char(string="Modelos", compute="_compute_model_names", store=False)
    level1_name = fields.Char(string="Nivel 1", compute="_compute_level_names", store=True)
    level2_name = fields.Char(string="Nivel 2", compute="_compute_level_names", store=True)
    level3_name = fields.Char(string="Nivel 3", compute="_compute_level_names", store=True)
    model_count = fields.Integer(string="# Modelos", compute="_compute_model_count", store=False)

    _syscom_id_unique = models.Constraint(
        "unique(syscom_id)",
        "El ID SYSCOM debe ser único.",
    )

    @api.depends("name", "level", "parent_id")
    def _compute_level_names(self):
        for record in self:
            names = [None, None, None]
            current = record
            # Walk up the tree to root, fill from level index-1
            while current:
                lvl = int(current.level or 0)
                idx = min(max(lvl, 1), 3) - 1
                names[idx] = current.name
                current = current.parent_id
            record.level1_name = names[0]
            record.level2_name = names[1]
            record.level3_name = names[2]

    @api.depends("product_ids.model")
    def _compute_model_names(self):
        for record in self:
            models = [m for m in record.product_ids.mapped("model") if m]
            record.model_names = ", ".join(sorted(set(models))) if models else False

    def _compute_brand_ids_tree(self):
        Brand = self.env["sync.syscom.brand"]
        Category = self.env["sync.syscom.category"]
        for record in self:
            # Obtener la categoría y todas sus descendientes
            cat_ids = Category.search([("id", "child_of", record.id)]).ids
            brands = Brand.browse([])
            if cat_ids:
                brands = Brand.search([("category_ids.id", "in", cat_ids)])
            record.brand_ids_tree = brands

    def _compute_model_count(self):
        for record in self:
            record.model_count = len(record.product_ids)

    def _get_marked_categories(self):
        return self.search([("selected", "=", True)])

    def _require_categories_for_view_action(self, label):
        categories = self.exists()
        if not categories:
            raise UserError(_("Selecciona al menos una categoría en la vista antes de ejecutar '%s'.") % label)
        return categories

    def _require_marked_categories(self, label):
        categories = self._get_marked_categories()
        if not categories:
            raise UserError(_("Marca al menos una categoría en la columna Lote antes de ejecutar '%s'.") % label)
        return categories

    def _build_syscom_client(self):
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("Configura el token en Ajustes antes de sincronizar."))

        base_url = params.get_param("sync_syscom.syscom_base_url") or "https://developers.syscom.mx/api/v1"
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or 30)
        return SyscomClient(base_url=base_url, token=token, timeout=timeout), params

    def _get_category_chunk_limit(self):
        params = self.env["ir.config_parameter"].sudo()
        try:
            chunk_limit = int(params.get_param("sync_syscom.category_chunk_limit") or 5)
        except Exception:
            chunk_limit = 5
        return max(chunk_limit, 1)

    def _sync_public_categories_subset(self, categories):
        categories = categories.sudo().exists()
        if not categories:
            return
        for category in categories.sorted(
            key=lambda rec: (int(rec.level or 0), int(rec.syscom_sequence or 10), rec.name or "")
        ):
            self._ensure_public_category(category)

    def _sync_categories_batch(self, client=None, offset=0, chunk_limit=None):
        client = client or self._build_syscom_client()[0]
        chunk_limit = chunk_limit or self._get_category_chunk_limit()

        categories = client.get_categories() or []
        total = len(categories)
        if total == 0:
            return {
                "total": 0,
                "processed": 0,
                "created": 0,
                "updated": 0,
                "next_offset": 0,
                "finished": True,
                "duration": 0.0,
            }

        offset = max(int(offset or 0), 0)
        if offset >= total:
            offset = 0

        start_time = time.monotonic()
        data_map = {}
        parent_map = {}
        synced_records = self.browse([])

        def parse_level(value, fallback):
            try:
                lvl = int(value)
                return lvl if lvl > 0 else fallback
            except Exception:
                return fallback

        def add_category(payload, parent_syscom_id=None, level_hint=None, sequence=None):
            if not isinstance(payload, dict):
                return None
            syscom_id = str(payload.get("id") or "").strip()
            if not syscom_id:
                return None
            level_val = parse_level(payload.get("nivel"), level_hint)
            vals = {
                "syscom_id": syscom_id,
                "name": payload.get("nombre") or syscom_id,
                "level": level_val,
                "active": True,
            }
            if sequence is not None:
                try:
                    vals["syscom_sequence"] = int(sequence)
                except Exception:
                    pass
            data_map[syscom_id] = vals
            if parent_syscom_id:
                parent_map[syscom_id] = str(parent_syscom_id)
            return syscom_id, level_val

        def iter_entries(entries):
            if isinstance(entries, list):
                for entry in entries:
                    yield entry
            elif isinstance(entries, dict):
                yield entries

        queue = []
        categories_slice = categories[offset : offset + chunk_limit]
        for index, category in enumerate(categories_slice):
            item = add_category(category, level_hint=1, sequence=index * 10)
            if item:
                queue.append(item)

        visited = set()
        while queue:
            current_syscom_id, current_level = queue.pop(0)
            if current_syscom_id in visited:
                continue
            visited.add(current_syscom_id)

            if current_level and current_level >= 3:
                continue

            detail = client.get_category_detail(current_syscom_id) or {}
            origin_entries = detail.get("origen")
            parent_origin_id = None
            if isinstance(origin_entries, list) and origin_entries:
                parent_origin_id = origin_entries[0].get("id")
            elif isinstance(origin_entries, dict):
                parent_origin_id = origin_entries.get("id")

            add_category(detail, parent_syscom_id=parent_origin_id, level_hint=current_level or 1)

            for origin in iter_entries(origin_entries):
                item = add_category(origin, level_hint=current_level - 1 if current_level else None)
                if item and (item[1] or 0) < 3:
                    queue.append((item[0], item[1] or (current_level or 1)))

            subcats = detail.get("subcategorías") or detail.get("subcategorias") or []
            subcats_list = list(iter_entries(subcats))
            for index, subcat in enumerate(subcats_list):
                item = add_category(
                    subcat,
                    parent_syscom_id=detail.get("id"),
                    level_hint=(current_level or 1) + 1,
                    sequence=index * 10,
                )
                if item and (item[1] or 0) < 3:
                    queue.append((item[0], item[1] or ((current_level or 1) + 1)))

        created = 0
        updated = 0
        for values in data_map.values():
            record = self.search([("syscom_id", "=", values["syscom_id"])], limit=1)
            if record:
                record.write(values)
                updated += 1
            else:
                record = self.create(values)
                created += 1
            synced_records |= record

        for child_syscom_id, parent_syscom_id in parent_map.items():
            child = self.search([("syscom_id", "=", child_syscom_id)], limit=1)
            parent = self.search([("syscom_id", "=", parent_syscom_id)], limit=1)
            if child and parent:
                child.parent_id = parent.id
                synced_records |= child | parent

        self._sync_public_categories_subset(synced_records)

        processed = len(categories_slice)
        next_offset = offset + processed
        finished = next_offset >= total
        if finished:
            next_offset = 0

        return {
            "total": total,
            "processed": processed,
            "created": created,
            "updated": updated,
            "next_offset": next_offset,
            "finished": finished,
            "duration": time.monotonic() - start_time,
        }

    def action_sync_syscom(self):
        batch = self._sync_categories_batch()

        self.env["sync.syscom.log"].create({
            "name": _("Sincronización de categorías"),
            "kind": "info",
            "message": _("Categorías creadas: %(created)s, actualizadas: %(updated)s. Duración: %(duration).2fs. Offset: %(offset)s/%(total)s")
            % {
                "created": batch["created"],
                "updated": batch["updated"],
                "duration": batch["duration"],
                "offset": batch["next_offset"],
                "total": batch["total"],
            },
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": _("Sincronización completada. Creadas: %(created)s, actualizadas: %(updated)s. Offset: %(offset)s/%(total)s.")
                % {
                    "created": batch["created"],
                    "updated": batch["updated"],
                    "offset": batch["next_offset"],
                    "total": batch["total"],
                },
                "type": "success",
                "sticky": False,
            },
        }

    def _ensure_public_category(self, syscom_category):
        """Create/link a product.public.category matching the SYSCOM category tree (website_id=False)."""
        if not syscom_category:
            return None
        syscom_category = syscom_category.sudo()

        def _seq():
            try:
                return int(syscom_category.syscom_sequence or 10)
            except Exception:
                return 10

        if syscom_category.public_category_id:
            if "sequence" in syscom_category.public_category_id._fields:
                syscom_category.public_category_id.sudo().write({"sequence": _seq()})
            return syscom_category.public_category_id

        parent_public = None
        if syscom_category.parent_id:
            parent_public = self._ensure_public_category(syscom_category.parent_id)

        PublicCategory = self.env["product.public.category"].sudo()
        domain = [("name", "=", syscom_category.name)]
        if "website_id" in PublicCategory._fields:
            domain.append(("website_id", "=", False))
        domain.append(("parent_id", "=", parent_public.id if parent_public else False))
        public_cat = PublicCategory.search(domain, limit=1)
        if not public_cat:
            vals = {"name": syscom_category.name}
            if parent_public:
                vals["parent_id"] = parent_public.id
            if "website_id" in PublicCategory._fields:
                vals["website_id"] = False
            if "sequence" in PublicCategory._fields:
                vals["sequence"] = _seq()
            public_cat = PublicCategory.create(vals)
        else:
            if "sequence" in public_cat._fields:
                public_cat.write({"sequence": _seq()})

        syscom_category.write({"public_category_id": public_cat.id})
        return public_cat

    def _sync_public_categories_from_syscom(self):
        """Ensure all SYSCOM categories have a corresponding public category with correct sequence."""
        all_cats = self.sudo().search([], order="level asc, syscom_sequence asc, name asc")
        for cat in all_cats:
            self._ensure_public_category(cat)

    def action_sync_brands_from_selected(self):
        return self.action_sync_brands_marked()

    def action_sync_brands_for_categories(self):
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("Configura el token en Ajustes antes de sincronizar."))

        selected_categories = self._require_categories_for_view_action("Sincronizar marcas selección vista")
        return self._run_sync_brands_for_categories(selected_categories, source_label=_("selección vista"))

    def action_sync_brands_marked(self):
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("Configura el token en Ajustes antes de sincronizar."))

        selected_categories = self._require_marked_categories("Sincronizar marcas marcadas en lote")
        return self._run_sync_brands_for_categories(selected_categories, source_label=_("marcadas en lote"))

    def _run_sync_brands_for_categories(self, selected_categories, source_label):
        params = self.env["ir.config_parameter"].sudo()
        selected_syscom_ids = set(selected_categories.mapped("syscom_id"))
        chunk_limit = int(params.get_param("sync_syscom.brand_chunk_limit") or 50)
        stats = self._sync_brands_for_scope(selected_syscom_ids, chunk_limit=chunk_limit)

        self.env["sync.syscom.log"].create({
            "name": _("Sincronización de marcas (categorías)"),
            "kind": "info",
            "message": _("Origen: %(source)s. Categorías: %(cats)s. Marcas vinculadas: %(kept)s, omitidas por categoría: %(skipped)s, timeout: %(t)s, procesadas en este lote: %(p)s, restantes estimadas: %(r)s")
            % {
                "source": source_label,
                "cats": ", ".join(selected_categories.mapped("syscom_id")),
                "kept": stats["kept"],
                "skipped": stats["skipped"],
                "t": stats["skipped_timeout"],
                "p": stats["processed"],
                "r": stats["remaining"],
            },
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": _("Marcas sincronizadas desde %(source)s: %(kept)s (omitidas categoría: %(skipped)s, timeout: %(t)s). Lote procesado: %(p)s, restantes estimadas: %(r)s.")
                % {
                    "source": source_label,
                    "kept": stats["kept"],
                    "skipped": stats["skipped"],
                    "t": stats["skipped_timeout"],
                    "p": stats["processed"],
                    "r": stats["remaining"],
                },
                "type": "success",
                "sticky": False,
            },
        }

    def _sync_brands_for_scope(self, selected_syscom_ids, chunk_limit=None):
        """Sync brand records linked to selected SYSCOM categories."""
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("Configura el token en Ajustes antes de sincronizar."))
        if not selected_syscom_ids:
            return {
                "kept": 0,
                "skipped": 0,
                "skipped_timeout": 0,
                "processed": 0,
                "remaining": 0,
                "brands": self.env["sync.syscom.brand"].browse([]),
            }

        base_url = params.get_param("sync_syscom.syscom_base_url") or "https://developers.syscom.mx/api/v1"
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or 30)
        client = SyscomClient(base_url=base_url, token=token, timeout=timeout)

        all_brands = client.get_brands() or []
        kept = 0
        skipped = 0
        skipped_timeout = 0
        processed = 0
        remaining = 0
        brand_records = self.env["sync.syscom.brand"].browse([])

        for brand in all_brands:
            if chunk_limit and processed >= chunk_limit:
                remaining += 1
                continue
            syscom_id = str(brand.get("id") or "").strip()
            if not syscom_id:
                continue

            categories = brand.get("categorías") or brand.get("categorias") or []
            detail = None
            if not categories:
                try:
                    detail = client.get_brand_detail(syscom_id, timeout=10) or {}
                    categories = detail.get("categorías") or detail.get("categorias") or []
                except UserError:
                    skipped_timeout += 1
                    continue

            cat_ids = []
            for category in categories:
                cat_syscom_id = str(category.get("id") or "").strip()
                if not cat_syscom_id or cat_syscom_id not in selected_syscom_ids:
                    continue
                cat_record = self.search([("syscom_id", "=", cat_syscom_id)], limit=1)
                if cat_record:
                    cat_ids.append(cat_record.id)
            if not cat_ids:
                skipped += 1
                continue

            if detail is None:
                try:
                    detail = client.get_brand_detail(syscom_id, timeout=10) or {}
                except UserError:
                    skipped_timeout += 1
                    continue

            brand_vals = {
                "syscom_id": syscom_id,
                "name": detail.get("titulo") or brand.get("nombre") or syscom_id,
                "title": detail.get("titulo") or brand.get("nombre") or "",
                "description": detail.get("descripcion") or "",
                "logo_url": detail.get("logo") or "",
                "active": True,
                "selected": True,
            }
            brand_record = self.env["sync.syscom.brand"].search([("syscom_id", "=", syscom_id)], limit=1)
            if brand_record:
                brand_record.write(brand_vals)
            else:
                brand_record = self.env["sync.syscom.brand"].create(brand_vals)
            brand_record.category_ids = [(6, 0, cat_ids)]

            brand_records |= brand_record
            kept += 1
            processed += 1

        return {
            "kept": kept,
            "skipped": skipped,
            "skipped_timeout": skipped_timeout,
            "processed": processed,
            "remaining": remaining,
            "brands": brand_records,
        }

    def _get_scope_categories(self, include_children=True):
        """Return categories in scope from the current recordset."""
        categories = self.exists()
        if not categories:
            return categories
        if not include_children:
            return categories
        return self.search([("id", "child_of", categories.ids)])

    def action_publish_scope_categories(self, include_children=None):
        """Schedule category publication in background."""
        categories = self._require_categories_for_view_action("Publicar selección vista")
        return self._run_publish_scope_categories(categories, include_children, source_label=_("selección vista"))

    def action_publish_marked_categories(self, include_children=None):
        categories = self._require_marked_categories("Publicar marcadas en lote")
        return self._run_publish_scope_categories(categories, include_children, source_label=_("marcadas en lote"))

    def _run_publish_scope_categories(self, categories, include_children, source_label):
        params = self.env["ir.config_parameter"].sudo()
        if include_children is None:
            include_children = params.get_param("sync_syscom.publish_include_subcategories", "1").lower() in ("1", "true", "yes")
        job = self.env["sync.syscom.publish.job"].create_for_categories(
            categories,
            include_children=bool(include_children),
        )

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": _("Trabajo de publicación por categoría programado desde %(source)s: %(job)s.")
                % {"source": source_label, "job": job.display_name},
                "type": "success",
                "sticky": False,
            },
        }

    def action_sync_categories_and_brands(self):
        """Programa sincronización completa en background."""
        job = self.env["sync.syscom.sync.job"].create_full_catalog_job()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": _("Trabajo de sincronización completa programado: %s.") % job.display_name,
                "type": "success",
                "sticky": False,
            },
        }

    def cron_sync_categories(self):
        """Compatibilidad hacia atrás: delega al worker de jobs."""
        self.env["sync.syscom.sync.job"].cron_process_sync_jobs()

    def action_start_category_sync(self):
        job = self.env["sync.syscom.sync.job"].create_categories_only_job()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": _("Trabajo de sincronización de categorías programado: %s.") % job.display_name,
                "type": "success",
                "sticky": False,
            },
        }

    def action_start_sync_pipeline(self):
        return self.action_sync_categories_and_brands()
