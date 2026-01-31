import time

from collections import defaultdict

from odoo import _, fields, models
from odoo.exceptions import UserError

from .syscom_client import SyscomClient


class SyscomBrand(models.Model):
    _name = "sync.syscom.brand"
    _description = "Marca SYSCOM"
    _order = "name"

    name = fields.Char(string="Nombre", required=True)
    syscom_id = fields.Char(string="ID SYSCOM", required=True, index=True)
    title = fields.Char(string="Título")
    description = fields.Text(string="Descripción")
    logo_url = fields.Char(string="Logo URL")
    active = fields.Boolean(string="Activo", default=True)
    selected = fields.Boolean(
        string="Sincronizar",
        default=False,
        help="Incluye esta marca en acciones de sincronización manual.",
    )
    category_ids = fields.Many2many(
        "sync.syscom.category",
        "sync_syscom_brand_category_rel",
        "brand_id",
        "category_id",
        string="Categorías",
    )
    category_count = fields.Integer(
        string="# Categorías",
        compute="_compute_category_count",
        store=False,
    )

    _syscom_id_unique = models.Constraint(
        "UNIQUE(syscom_id)",
        "El ID SYSCOM debe ser único.",
    )

    def _compute_category_count(self):
        for record in self:
            record.category_count = len(record.category_ids)

    def name_get(self):
        result = []
        for record in self:
            display = record.syscom_id or record.name or _("Sin ID")
            result.append((record.id, display))
        return result

    def _get_selected_categories(self):
        return self.env["sync.syscom.category"].search([("selected", "=", True)])

    def cron_sync_all_brands_batch(self):
        """Ejecutado por cron: procesa un lote; se desactiva solo al completar todas las marcas."""
        self.action_sync_all_brands_batch()
        # Si el offset volvió a cero, ya dimos la vuelta completa: desactivar el cron
        params = self.env["ir.config_parameter"].sudo()
        offset = int(params.get_param("sync_syscom.brand_sync_offset") or 0)
        if offset == 0:
            cron = self.env.ref("sync_syscom.cron_sync_syscom_brands_full", raise_if_not_found=False)
            if cron:
                cron.active = False

    def action_sync_all_brands_batch(self):
        """Sincroniza marcas en lotes, usando offset persistido para evitar timeouts."""
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("Configura el token en Ajustes antes de sincronizar."))

        base_url = params.get_param("sync_syscom.syscom_base_url") or "https://developers.syscom.mx/api/v1"
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or 30)
        detail_timeout = int(params.get_param("sync_syscom.brand_detail_timeout") or 3)
        chunk_limit = int(params.get_param("sync_syscom.brand_detail_chunk_limit") or 10)
        client = SyscomClient(base_url=base_url, token=token, timeout=timeout)

        brands = client.get_brands() or []
        total = len(brands)
        offset = int(params.get_param("sync_syscom.brand_sync_offset") or 0)
        if offset >= total:
            offset = 0

        slice_brands = brands[offset : offset + chunk_limit]
        processed = 0
        created = updated = timeout_skip = 0

        for brand in slice_brands:
            syscom_id = str(brand.get("id") or "").strip()
            if not syscom_id:
                offset += 1
                continue
            try:
                detail = client.get_brand_detail(syscom_id, timeout=detail_timeout) or {}
            except UserError:
                timeout_skip += 1
                offset += 1
                continue

            categories = detail.get("categorías") or detail.get("categorias") or []
            cat_ids = []
            for category in categories:
                cat_syscom_id = str(category.get("id") or "").strip()
                if not cat_syscom_id:
                    continue
                cat_record = self.env["sync.syscom.category"].search(
                    [("syscom_id", "=", cat_syscom_id)],
                    limit=1,
                )
                if cat_record:
                    cat_ids.append(cat_record.id)

            vals = {
                "syscom_id": syscom_id,
                "name": detail.get("titulo") or brand.get("nombre") or syscom_id,
                "title": detail.get("titulo") or brand.get("nombre") or "",
                "description": detail.get("descripcion") or "",
                "logo_url": detail.get("logo") or "",
                "active": True,
            }
            record = self.search([("syscom_id", "=", syscom_id)], limit=1)
            if record:
                record.write(vals)
                updated += 1
            else:
                record = self.create(vals)
                created += 1

            if cat_ids:
                record.category_ids = [(6, 0, cat_ids)]

            processed += 1
            offset += 1

        # Persist new offset
        if offset >= total:
            offset = 0
        params.set_param("sync_syscom.brand_sync_offset", offset)

        self.env["sync.syscom.log"].create({
            "name": _("Sincronización de marcas (lotes)"),
            "kind": "info",
            "message": _("Marcas procesadas: %(p)s (creadas %(c)s, actualizadas %(u)s, timeout %(t)s). Quedan: %(r)s")
            % {
                "p": processed,
                "c": created,
                "u": updated,
                "t": timeout_skip,
                "r": max(total - offset, 0) if processed else max(total - offset, 0),
            },
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": _("Marcas procesadas: %(p)s (creadas %(c)s, actualizadas %(u)s, timeout %(t)s). Pendientes aprox: %(r)s.")
                % {
                    "p": processed,
                    "c": created,
                    "u": updated,
                    "t": timeout_skip,
                    "r": max(total - offset, 0),
                },
                "type": "success",
                "sticky": False,
            },
        }

    def action_sync_all_brands_full(self):
        """Sincroniza todas las marcas con sus categorías en una sola corrida."""
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("Configura el token en Ajustes antes de sincronizar."))

        base_url = params.get_param("sync_syscom.syscom_base_url") or "https://developers.syscom.mx/api/v1"
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or 30)
        detail_timeout = int(params.get_param("sync_syscom.brand_detail_timeout") or 5)
        client = SyscomClient(base_url=base_url, token=token, timeout=timeout)

        brands = client.get_brands() or []
        created = updated = timeout_skip = 0

        for brand in brands:
            syscom_id = str(brand.get("id") or "").strip()
            if not syscom_id:
                continue
            try:
                detail = client.get_brand_detail(syscom_id, timeout=detail_timeout) or {}
            except UserError:
                timeout_skip += 1
                continue

            categories = detail.get("categorías") or detail.get("categorias") or []
            cat_ids = []
            for category in categories:
                cat_syscom_id = str(category.get("id") or "").strip()
                if not cat_syscom_id:
                    continue
                cat_record = self.env["sync.syscom.category"].search(
                    [("syscom_id", "=", cat_syscom_id)],
                    limit=1,
                )
                if cat_record:
                    cat_ids.append(cat_record.id)

            vals = {
                "syscom_id": syscom_id,
                "name": detail.get("titulo") or brand.get("nombre") or syscom_id,
                "title": detail.get("titulo") or brand.get("nombre") or "",
                "description": detail.get("descripcion") or "",
                "logo_url": detail.get("logo") or "",
                "active": True,
                "selected": True,
            }
            record = self.search([("syscom_id", "=", syscom_id)], limit=1)
            if record:
                record.write(vals)
                updated += 1
            else:
                record = self.create(vals)
                created += 1

            if cat_ids:
                record.category_ids = [(6, 0, cat_ids)]

        self.env["sync.syscom.log"].create({
            "name": _("Sincronización de marcas (completa)"),
            "kind": "info",
            "message": _("Marcas creadas: %(c)s, actualizadas: %(u)s, omitidas por timeout: %(t)s")
            % {"c": created, "u": updated, "t": timeout_skip},
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": _("Marcas sincronizadas. Creadas: %(c)s, actualizadas: %(u)s, timeout: %(t)s.")
                % {"c": created, "u": updated, "t": timeout_skip},
                "type": "success",
                "sticky": False,
            },
        }

    def action_sync_models_selected(self):
        """Sincroniza modelos de marcas seleccionadas y categorías seleccionadas."""
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("Configura el token en Ajustes antes de sincronizar."))

        base_url = params.get_param("sync_syscom.syscom_base_url") or "https://developers.syscom.mx/api/v1"
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or 30)
        client = SyscomClient(base_url=base_url, token=token, timeout=timeout)

        categories_selected = self._get_selected_categories()
        selected_cat_ids = set(categories_selected.mapped("syscom_id"))
        if not selected_cat_ids:
            raise UserError(_("Marca al menos una categoría (columna Sel) antes de sincronizar modelos."))

        brands = self.search([("selected", "=", True)])
        if not brands:
            raise UserError(_("Marca al menos una marca (columna Sel) antes de sincronizar modelos."))

        created = updated = kept = 0
        category_product_links = defaultdict(list)

        for brand in brands:
            products = client.get_brand_products(brand.syscom_id) or []
            for product in products:
                prod_syscom_id = str(product.get("producto_id") or product.get("id") or "").strip()
                if not prod_syscom_id:
                    continue
                categories = product.get("categorías") or product.get("categorias") or []
                cat_ids = []
                match_selected = False
                for cat in categories:
                    cat_syscom_id = str(cat.get("id") or "").strip()
                    if not cat_syscom_id:
                        continue
                    cat_record = self.env["sync.syscom.category"].search(
                        [("syscom_id", "=", cat_syscom_id)],
                        limit=1,
                    )
                    if cat_record:
                        cat_ids.append(cat_record.id)
                        if cat_syscom_id in selected_cat_ids:
                            match_selected = True
                if not match_selected:
                    continue

                vals = {
                    "syscom_id": prod_syscom_id,
                    "model": product.get("modelo") or prod_syscom_id,
                    "name": product.get("titulo") or product.get("modelo") or prod_syscom_id,
                    "active": True,
                    "brand_id": brand.id,
                }
                prod_record = self.env["sync.syscom.product"].search(
                    [("syscom_id", "=", prod_syscom_id)],
                    limit=1,
                )
                if prod_record:
                    prod_record.write(vals)
                    updated += 1
                else:
                    prod_record = self.env["sync.syscom.product"].create(vals)
                    created += 1
                if cat_ids:
                    prod_record.category_ids = [(6, 0, cat_ids)]
                    for cid in cat_ids:
                        category_product_links[cid].append(prod_record.id)
                kept += 1

        for cat_id, prod_ids in category_product_links.items():
            category = self.env["sync.syscom.category"].browse(cat_id)
            category.product_ids = [(6, 0, list(set(prod_ids)))]

        self.env["sync.syscom.log"].create({
            "name": _("Sincronización de modelos (marcas/categorías seleccionadas)"),
            "kind": "info",
            "message": _("Productos creados: %(created)s, actualizados: %(updated)s, retenidos: %(kept)s")
            % {"created": created, "updated": updated, "kept": kept},
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": _("Modelos sincronizados: %(kept)s (creados: %(created)s, actualizados: %(updated)s).")
                % {"created": created, "updated": updated, "kept": kept},
                "type": "success",
                "sticky": False,
            },
        }

    def action_sync_all_models(self):
        """Sincroniza todos los modelos de esta marca, sin filtrar categorías."""
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("Configura el token en Ajustes antes de sincronizar."))

        base_url = params.get_param("sync_syscom.syscom_base_url") or "https://developers.syscom.mx/api/v1"
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or 30)
        client = SyscomClient(base_url=base_url, token=token, timeout=timeout)

        created = updated = 0
        category_product_links = defaultdict(list)

        for brand in self:
            products = client.get_brand_products(brand.syscom_id) or []
            for product in products:
                prod_syscom_id = str(product.get("producto_id") or product.get("id") or "").strip()
                if not prod_syscom_id:
                    continue
                vals = {
                    "syscom_id": prod_syscom_id,
                    "model": product.get("modelo") or prod_syscom_id,
                    "name": product.get("titulo") or product.get("modelo") or prod_syscom_id,
                    "active": True,
                    "brand_id": brand.id,
                }
                prod_record = self.env["sync.syscom.product"].search(
                    [("syscom_id", "=", prod_syscom_id)],
                    limit=1,
                )
                if prod_record:
                    prod_record.write(vals)
                    updated += 1
                else:
                    prod_record = self.env["sync.syscom.product"].create(vals)
                    created += 1

                cat_ids = []
                for category in product.get("categorías") or product.get("categorias") or []:
                    cat_syscom_id = str(category.get("id") or "").strip()
                    if not cat_syscom_id:
                        continue
                    cat_record = self.env["sync.syscom.category"].search(
                        [("syscom_id", "=", cat_syscom_id)],
                        limit=1,
                    )
                    if cat_record:
                        cat_ids.append(cat_record.id)
                        category_product_links[cat_record.id].append(prod_record.id)
                if cat_ids:
                    prod_record.category_ids = [(6, 0, cat_ids)]

        for cat_id, prod_ids in category_product_links.items():
            category = self.env["sync.syscom.category"].browse(cat_id)
            category.product_ids = [(6, 0, list(set(prod_ids)))]

        self.env["sync.syscom.log"].create({
            "name": _("Sincronización de modelos (todas las categorías de la marca)"),
            "kind": "info",
            "message": _("Productos creados: %(created)s, actualizados: %(updated)s")
            % {"created": created, "updated": updated},
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": _("Modelos sincronizados. Creados: %(created)s, actualizados: %(updated)s.")
                % {"created": created, "updated": updated},
                "type": "success",
                "sticky": False,
            },
        }
    def action_sync_syscom(self):
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("Configura el token en Ajustes antes de sincronizar."))

        base_url = params.get_param("sync_syscom.syscom_base_url") or "https://developers.syscom.mx/api/v1"
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or 30)
        client = SyscomClient(base_url=base_url, token=token, timeout=timeout)

        start_time = time.monotonic()
        brands = client.get_brands() or []
        created = 0
        updated = 0
        category_links = {}
        category_product_links = defaultdict(list)
        product_records = {}

        for brand in brands:
            syscom_id = str(brand.get("id") or "").strip()
            if not syscom_id:
                continue
            values = {
                "syscom_id": syscom_id,
                "name": brand.get("nombre") or syscom_id,
                "active": True,
            }
            detail = client.get_brand_detail(syscom_id) or {}
            if isinstance(detail, dict):
                values.update({
                    "title": detail.get("titulo") or values["name"],
                    "description": detail.get("descripcion") or "",
                    "logo_url": detail.get("logo") or "",
                })
                categories = detail.get("categorías") or detail.get("categorias") or []
                if categories:
                    category_links[syscom_id] = categories

            record = self.search([("syscom_id", "=", syscom_id)], limit=1)
            if record:
                record.write(values)
                updated += 1
            else:
                record = self.create(values)
                created += 1

            if syscom_id in category_links:
                category_ids = []
                for category in category_links[syscom_id]:
                    category_syscom_id = str(category.get("id") or "").strip()
                    if not category_syscom_id:
                        continue
                    category_record = self.env["sync.syscom.category"].search(
                        [("syscom_id", "=", category_syscom_id)],
                        limit=1,
                    )
                    if category_record:
                        category_ids.append(category_record.id)
                if category_ids:
                    record.category_ids = [(6, 0, category_ids)]

            # Productos por marca (con categorías)
            products = client.get_brand_products(syscom_id) or []
            for product in products:
                prod_syscom_id = str(product.get("producto_id") or product.get("id") or "").strip()
                if not prod_syscom_id:
                    continue
                prod_vals = {
                    "syscom_id": prod_syscom_id,
                    "model": product.get("modelo") or prod_syscom_id,
                    "name": product.get("titulo") or product.get("modelo") or prod_syscom_id,
                    "active": True,
                    "brand_id": record.id,
                }
                prod_record = self.env["sync.syscom.product"].search(
                    [("syscom_id", "=", prod_syscom_id)],
                    limit=1,
                )
                if prod_record:
                    prod_record.write(prod_vals)
                else:
                    prod_record = self.env["sync.syscom.product"].create(prod_vals)
                product_records[prod_syscom_id] = prod_record.id

                cat_ids = []
                for category in product.get("categorías") or product.get("categorias") or []:
                    cat_syscom_id = str(category.get("id") or "").strip()
                    if not cat_syscom_id:
                        continue
                    cat_record = self.env["sync.syscom.category"].search(
                        [("syscom_id", "=", cat_syscom_id)],
                        limit=1,
                    )
                    if cat_record:
                        cat_ids.append(cat_record.id)
                        category_product_links[cat_record.id].append(prod_record.id)
                if cat_ids:
                    prod_record.category_ids = [(6, 0, cat_ids)]

        duration = time.monotonic() - start_time
        self.env["sync.syscom.log"].create({
            "name": _("Sincronización de marcas"),
            "kind": "info",
            "message": _("Marcas creadas: %(created)s, actualizadas: %(updated)s. Duración: %(duration).2fs")
            % {
                "created": created,
                "updated": updated,
                "duration": duration,
            },
        })

        # Vincular productos a categorías (m2m) después de haberlos creado/actualizado
        for cat_id, prod_ids in category_product_links.items():
            category = self.env["sync.syscom.category"].browse(cat_id)
            category.product_ids = [(6, 0, list(set(prod_ids)))]

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
