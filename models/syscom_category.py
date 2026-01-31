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
    brand_ids = fields.Many2many(
        "sync.syscom.brand",
        "sync_syscom_brand_category_rel",
        "category_id",
        "brand_id",
        string="Marcas",
    )
    selected = fields.Boolean(string="Sel", default=False)
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
        string="Marcas (árbol)",
        store=False,
    )
    model_names = fields.Char(string="Modelos", compute="_compute_model_names", store=False)
    level1_name = fields.Char(string="Nivel 1", compute="_compute_level_names", store=True)
    level2_name = fields.Char(string="Nivel 2", compute="_compute_level_names", store=True)
    level3_name = fields.Char(string="Nivel 3", compute="_compute_level_names", store=True)

    _syscom_id_unique = models.Constraint(
        "UNIQUE(syscom_id)",
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

    def action_sync_syscom(self):
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("Configura el token en Ajustes antes de sincronizar."))

        base_url = params.get_param("sync_syscom.syscom_base_url") or "https://developers.syscom.mx/api/v1"
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or 30)
        client = SyscomClient(base_url=base_url, token=token, timeout=timeout)

        start_time = time.monotonic()
        categories = client.get_categories() or []
        data_map = {}
        parent_map = {}

        def parse_level(value, fallback):
            try:
                lvl = int(value)
                return lvl if lvl > 0 else fallback
            except Exception:
                return fallback

        def add_category(payload, parent_syscom_id=None, level_hint=None):
            if not isinstance(payload, dict):
                return None
            syscom_id = str(payload.get("id") or "").strip()
            if not syscom_id:
                return None
            level_val = parse_level(payload.get("nivel"), level_hint)
            data_map[syscom_id] = {
                "syscom_id": syscom_id,
                "name": payload.get("nombre") or syscom_id,
                "level": level_val,
                "active": True,
            }
            if parent_syscom_id:
                parent_map[syscom_id] = str(parent_syscom_id)
            return syscom_id, level_val

        queue = []
        for category in categories:
            item = add_category(category, level_hint=1)
            if item:
                queue.append(item)

        visited = set()
        # Recursivo hasta 3 niveles usando detalle de cada categoría
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

            def iter_entries(entries):
                if isinstance(entries, list):
                    for entry in entries:
                        yield entry
                elif isinstance(entries, dict):
                    yield entries

            for origin in iter_entries(origin_entries):
                item = add_category(origin, level_hint=current_level - 1 if current_level else None)
                if item and (item[1] or 0) < 3:
                    queue.append((item[0], item[1] or (current_level or 1)))

            subcats = detail.get("subcategorías") or detail.get("subcategorias") or []
            for subcat in iter_entries(subcats):
                item = add_category(subcat, parent_syscom_id=detail.get("id"), level_hint=(current_level or 1) + 1)
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
                self.create(values)
                created += 1

        for child_syscom_id, parent_syscom_id in parent_map.items():
            child = self.search([("syscom_id", "=", child_syscom_id)], limit=1)
            parent = self.search([("syscom_id", "=", parent_syscom_id)], limit=1)
            if child and parent:
                child.parent_id = parent.id

        duration = time.monotonic() - start_time
        self.env["sync.syscom.log"].create({
            "name": _("Sincronización de categorías"),
            "kind": "info",
            "message": _("Categorías creadas: %(created)s, actualizadas: %(updated)s. Duración: %(duration).2fs")
            % {
                "created": created,
                "updated": updated,
                "duration": duration,
            },
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": _("Sincronización completada. Creadas: %(created)s, actualizadas: %(updated)s.")
                % {"created": created, "updated": updated},
                "type": "success",
                "sticky": False,
            },
        }

    def action_sync_brands_from_selected(self):
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("Configura el token en Ajustes antes de sincronizar."))

        selected_categories = self.search([("selected", "=", True)])
        if not selected_categories:
            raise UserError(_("Marca al menos una categoría (columna Sel) antes de sincronizar marcas."))

        selected_syscom_ids = set(selected_categories.mapped("syscom_id"))

        base_url = params.get_param("sync_syscom.syscom_base_url") or "https://developers.syscom.mx/api/v1"
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or 30)
        chunk_limit = int(params.get_param("sync_syscom.brand_chunk_limit") or 50)
        client = SyscomClient(base_url=base_url, token=token, timeout=timeout)

        brands = client.get_brands() or []
        kept = 0
        skipped = 0
        skipped_timeout = 0
        processed = 0
        remaining = 0

        for brand in brands:
            if processed >= chunk_limit:
                remaining += 1
                continue
            syscom_id = str(brand.get("id") or "").strip()
            if not syscom_id:
                continue
            # Intentar usar categorías del listado para evitar muchas llamadas de detalle
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
                if not cat_syscom_id:
                    continue
                if cat_syscom_id in selected_syscom_ids:
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
            kept += 1
            processed += 1

        self.env["sync.syscom.log"].create({
            "name": _("Sincronización de marcas (categorías seleccionadas)"),
            "kind": "info",
            "message": _("Marcas vinculadas: %(kept)s, omitidas por categoría: %(skipped)s, timeout: %(t)s, procesadas en este lote: %(p)s, restantes estimadas: %(r)s")
            % {"kept": kept, "skipped": skipped, "t": skipped_timeout, "p": processed, "r": remaining},
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": _("Marcas sincronizadas: %(kept)s (omitidas categoría: %(skipped)s, timeout: %(t)s). Lote procesado: %(p)s, restantes estimadas: %(r)s.")
                % {"kept": kept, "skipped": skipped, "t": skipped_timeout, "p": processed, "r": remaining},
                "type": "success",
                "sticky": False,
            },
        }

    def action_sync_categories_and_brands(self):
        """Programa sincronización completa en background (categorías y luego marcas)."""
        params = self.env["ir.config_parameter"].sudo()
        # Resetear offset de marcas
        params.set_param("sync_syscom.brand_sync_offset", 0)
        # Activar cron de categorías (que al terminar activará la de marcas)
        cron_cat = self.env.ref("sync_syscom.cron_sync_syscom_categories", raise_if_not_found=False)
        if cron_cat:
            cron_cat.active = True
            cron_cat.nextcall = fields.Datetime.now()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": _("Sincronización en proceso en segundo plano (categorías y luego marcas)."),
                "type": "success",
                "sticky": False,
            },
        }

    def cron_sync_categories(self):
        """Cron: sincroniza categorías y luego lanza la cron de marcas."""
        self.action_sync_syscom()
        cron_brand = self.env.ref("sync_syscom.cron_sync_syscom_brands_full", raise_if_not_found=False)
        if cron_brand:
            cron_brand.active = True
            cron_brand.nextcall = fields.Datetime.now()
