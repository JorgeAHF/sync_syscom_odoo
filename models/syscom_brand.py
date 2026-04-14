import time

from odoo import _, fields, models
from odoo.exceptions import UserError

from .syscom_client import SyscomClient
from .constants import (
    SYSCOM_DEFAULT_BASE_URL,
    SYSCOM_DEFAULT_TIMEOUT,
    SYSCOM_BRAND_DETAIL_TIMEOUT,
    SYSCOM_PAGE_SIZE,
    SYSCOM_PAGE_LIMIT,
)


class SyscomBrand(models.Model):
    _name = "sync.syscom.brand"
    _description = "Marca SYSCOM"
    _order = "name"
    _rec_name = "syscom_id"

    name = fields.Char(string="Nombre", required=True)
    syscom_id = fields.Char(string="ID SYSCOM", required=True, index=True)
    title = fields.Char(string="Título")
    description = fields.Text(string="Descripción")
    logo_url = fields.Char(string="Logo URL")
    active = fields.Boolean(string="Activo", default=True)
    selected = fields.Boolean(
        string="Lote",
        default=False,
        help="Marca persistente para procesos batch manuales. No equivale a la selección visual de la vista.",
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
        "unique(syscom_id)",
        "El ID SYSCOM debe ser único.",
    )

    def _compute_category_count(self):
        for record in self:
            record.category_count = len(record.category_ids)

    def name_get(self):
        """Usa el ID SYSCOM como etiqueta principal en todas las vistas."""
        result = []
        for record in self:
            display = record.syscom_id or record.name or _("Sin ID")
            result.append((record.id, display))
        return result

    def _get_selected_categories(self):
        return self.env["sync.syscom.category"].search([("selected", "=", True)])

    def _get_marked_brands(self):
        return self.search([("selected", "=", True)])

    def _require_brands_for_view_action(self, label):
        brands = self.exists()
        if not brands:
            raise UserError(_("Selecciona al menos una marca en la vista antes de ejecutar '%s'.") % label)
        return brands

    def _require_marked_brands(self, label):
        brands = self._get_marked_brands()
        if not brands:
            raise UserError(_("Marca al menos una marca en la columna Lote antes de ejecutar '%s'.") % label)
        return brands

    def _build_syscom_client(self):
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("Configura el token en Ajustes antes de sincronizar."))

        base_url = params.get_param("sync_syscom.syscom_base_url") or SYSCOM_DEFAULT_BASE_URL
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or SYSCOM_DEFAULT_TIMEOUT)
        return SyscomClient(base_url=base_url, token=token, timeout=timeout), params

    def action_start_brand_sync(self):
        """Programa la sincronización de marcas y modelos en background."""
        job = self.env["sync.syscom.sync.job"].create_brands_products_job()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": _("Trabajo de sincronización de marcas/modelos programado: %s.") % job.display_name,
                "type": "success",
                "sticky": False,
            },
        }

    def _fetch_all_brand_products(self, client, brand_syscom_id, stock=None, timeout=None, page_limit=SYSCOM_PAGE_LIMIT):
        """Itera paginando /marcas/{id}/productos hasta agotar resultados o llegar al límite.

        Estrategia de terminación (en orden de prioridad):
        1. Si la API devuelve ``paginas`` → usarlo como referencia exacta.
        2. Si la API devuelve ``cantidad`` → detener cuando acumulados >= cantidad.
        3. Heurística de respaldo: si el batch devuelto tiene menos de SYSCOM_PAGE_SIZE
           ítems, asumimos que es la última página.

        Devuelve (all_products, pages_done, total_pages, total_count).
        """
        all_products = []
        page = 1
        total_pages = None
        total_count = 0
        while page <= page_limit:
            products = client.get_brand_products(brand_syscom_id, page=page, stock=stock)
            if not products:
                break
            # La API puede devolver lista directa o dict con metadatos de paginación.
            if isinstance(products, dict) and "productos" in products:
                batch = products.get("productos") or []
                # Actualizar metadatos solo si la API los devuelve (pueden ser None).
                if products.get("paginas") is not None:
                    try:
                        total_pages = int(products["paginas"])
                    except (TypeError, ValueError):
                        pass
                if products.get("cantidad") is not None:
                    try:
                        total_count = int(products["cantidad"])
                    except (TypeError, ValueError):
                        pass
            else:
                batch = products or []
            if not batch:
                break
            all_products.extend(batch)

            # Criterio 1: total de páginas conocido por la API.
            if total_pages is not None and page >= total_pages:
                break
            # Criterio 2: total de ítems conocido por la API.
            if total_count and len(all_products) >= total_count:
                break
            # Criterio 3: heurística — batch incompleto implica última página.
            if len(batch) < SYSCOM_PAGE_SIZE:
                break
            page += 1
        return all_products, page - 1, total_pages, total_count

    def _sync_brand_products_for_brand(self, client, brand_record, params):
        """Crea/actualiza stubs de productos y vincula categorías para una marca."""
        products, pages_done, pages_total, total_count = self._fetch_all_brand_products(
            client,
            brand_record.syscom_id,
            stock=params.get_param("sync_syscom.brand_products_stock"),
        ) or []
        created = 0
        updated = 0

        for product in products:
            prod_syscom_id = str(product.get("producto_id") or product.get("id") or "").strip()
            if not prod_syscom_id:
                continue
            prod_vals = {
                "syscom_id": prod_syscom_id,
                "model": product.get("modelo") or prod_syscom_id,
                "name": product.get("titulo") or product.get("modelo") or prod_syscom_id,
                "active": True,
                "brand_id": brand_record.id,
            }
            prod_record = self.env["sync.syscom.product"].search(
                [("syscom_id", "=", prod_syscom_id)],
                limit=1,
            )
            if prod_record:
                prod_record.write(prod_vals)
                updated += 1
            else:
                prod_record = self.env["sync.syscom.product"].create(prod_vals)
                created += 1

            prod_cat_ids = []
            for cat in product.get("categorías") or product.get("categorias") or []:
                cat_syscom_id = str(cat.get("id") or "").strip()
                if not cat_syscom_id:
                    continue
                cat_record = self.env["sync.syscom.category"].search(
                    [("syscom_id", "=", cat_syscom_id)],
                    limit=1,
                )
                if cat_record:
                    prod_cat_ids.append(cat_record.id)
            if prod_cat_ids:
                prod_record.category_ids = [(6, 0, prod_cat_ids)]

        # Registrar metadatos de lote
        self.env["sync.syscom.log"].sudo().create({
            "name": _("Sync productos marca %(b)s") % {"b": brand_record.syscom_id},
            "kind": "info",
            "message": _("Productos obtenidos: %(n)s, páginas: %(d)s/%(t)s, total reportado: %(c)s") % {
                "n": len(products),
                "d": pages_done,
                "t": pages_total or "¿?",
                "c": total_count or "¿?",
            },
        })

        return {
            "fetched": len(products),
            "created": created,
            "updated": updated,
            "pages_done": pages_done,
            "pages_total": pages_total,
            "total_count": total_count,
        }

    def cron_sync_all_brands_batch(self):
        """Compatibilidad hacia atrás: delega al worker de jobs."""
        self.env["sync.syscom.sync.job"].cron_process_sync_jobs()

    def cron_sync_brand_products_batch(self):
        """Compatibilidad hacia atrás: delega al worker de jobs."""
        self.env["sync.syscom.sync.job"].cron_process_sync_jobs()

    def _sync_brands_batch(self, client=None, offset=0, chunk_limit=None, detail_timeout=None):
        client, params = (client, None) if client else self._build_syscom_client()
        if params is None:
            params = self.env["ir.config_parameter"].sudo()
        if not chunk_limit:
            chunk_limit = int(params.get_param("sync_syscom.brand_detail_chunk_limit") or 10)
        if detail_timeout is None:
            detail_timeout = int(params.get_param("sync_syscom.brand_detail_timeout") or 3)

        brands = client.get_brands() or []
        total = len(brands)
        if total == 0:
            return {
                "total": 0,
                "processed": 0,
                "created": 0,
                "updated": 0,
                "timeout_skip": 0,
                "next_offset": 0,
                "finished": True,
            }

        offset = max(int(offset or 0), 0)
        if offset >= total:
            offset = 0

        slice_brands = brands[offset : offset + chunk_limit]
        processed = 0
        created = 0
        updated = 0
        timeout_skip = 0

        for brand in slice_brands:
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

        next_offset = offset + len(slice_brands)
        finished = next_offset >= total
        if finished:
            next_offset = 0

        return {
            "total": total,
            "processed": len(slice_brands),
            "created": created,
            "updated": updated,
            "timeout_skip": timeout_skip,
            "next_offset": next_offset,
            "finished": finished,
        }

    def _sync_local_brand_products_batch(self, client=None, offset=0, chunk_limit=None):
        client, params = (client, None) if client else self._build_syscom_client()
        if params is None:
            params = self.env["ir.config_parameter"].sudo()
        if not chunk_limit:
            chunk_limit = int(params.get_param("sync_syscom.brand_products_chunk_limit") or 5)

        brands = self.search([], order="id")
        total = len(brands)
        if total == 0:
            return {
                "total": 0,
                "processed": 0,
                "created_products": 0,
                "updated_products": 0,
                "fetched_products": 0,
                "errors": 0,
                "next_offset": 0,
                "finished": True,
            }

        offset = max(int(offset or 0), 0)
        if offset >= total:
            offset = 0

        slice_brands = brands[offset : offset + chunk_limit]
        processed = 0
        created_products = 0
        updated_products = 0
        fetched_products = 0
        errors = 0

        for brand in slice_brands:
            try:
                result = self._sync_brand_products_for_brand(client, brand, params)
                processed += 1
                created_products += result["created"]
                updated_products += result["updated"]
                fetched_products += result["fetched"]
            except Exception as exc:
                errors += 1
                self.env["sync.syscom.log"].sudo().create({
                    "name": _("Sync productos marca %(b)s") % {"b": brand.syscom_id},
                    "kind": "error",
                    "message": _("Error sincronizando marca: %s") % exc,
                })

        next_offset = offset + len(slice_brands)
        finished = next_offset >= total
        if finished:
            next_offset = 0

        return {
            "total": total,
            "processed": len(slice_brands),
            "created_products": created_products,
            "updated_products": updated_products,
            "fetched_products": fetched_products,
            "errors": errors,
            "next_offset": next_offset,
            "finished": finished,
        }

    def action_sync_all_brands_batch(self):
        """Procesa un solo lote de marcas sin tocar crons ni offsets globales."""
        batch = self._sync_brands_batch()

        self.env["sync.syscom.log"].create({
            "name": _("Sincronización de marcas (lotes)"),
            "kind": "info",
            "message": _("Marcas procesadas: %(p)s (creadas %(c)s, actualizadas %(u)s, timeout %(t)s). Quedan: %(r)s")
            % {
                "p": batch["processed"],
                "c": batch["created"],
                "u": batch["updated"],
                "t": batch["timeout_skip"],
                "r": max(batch["total"] - batch["next_offset"], 0),
            },
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": _("Marcas procesadas: %(p)s (creadas %(c)s, actualizadas %(u)s, timeout %(t)s). Pendientes aprox: %(r)s.")
                % {
                    "p": batch["processed"],
                    "c": batch["created"],
                    "u": batch["updated"],
                    "t": batch["timeout_skip"],
                    "r": max(batch["total"] - batch["next_offset"], 0),
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

            # Recoger productos para obtener categorías nivel 3 también
            products, _pages_done, _pages_total, _total_count = self._fetch_all_brand_products(
                client,
                syscom_id,
                stock=params.get_param("sync_syscom.brand_products_stock"),
            )

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

            # Complementar con categorías derivadas de los productos (niveles 2/3)
            for product in products or []:
                for cat in product.get("categorías") or product.get("categorias") or []:
                    cat_syscom_id = str(cat.get("id") or "").strip()
                    if not cat_syscom_id:
                        continue
                    cat_record = self.env["sync.syscom.category"].search(
                        [("syscom_id", "=", cat_syscom_id)],
                        limit=1,
                    )
                    if cat_record and cat_record.id not in cat_ids:
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

            # Crear/actualizar stubs de productos (sin detalle) para catálogo
            for product in products or []:
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
                prod_cat_ids = []
                for cat in product.get("categorías") or product.get("categorias") or []:
                    cat_syscom_id = str(cat.get("id") or "").strip()
                    if not cat_syscom_id:
                        continue
                    cat_record = self.env["sync.syscom.category"].search(
                        [("syscom_id", "=", cat_syscom_id)],
                        limit=1,
                    )
                    if cat_record:
                        prod_cat_ids.append(cat_record.id)
                if prod_cat_ids:
                    prod_record.category_ids = [(6, 0, prod_cat_ids)]

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
        """Compatibilidad: usa el modo explícito de marcados en lote."""
        return self.action_sync_models_marked()

    def action_sync_models_for_brands(self):
        """Sincroniza modelos para marcas seleccionadas en la vista y categorías marcadas en lote."""
        categories_selected = self._get_selected_categories()
        selected_cat_ids = set(categories_selected.mapped("syscom_id"))
        if not selected_cat_ids:
            raise UserError(_("Marca al menos una categoría en la columna Lote antes de sincronizar modelos."))

        brands = self._require_brands_for_view_action("Sincronizar modelos selección vista")
        return self._run_sync_models_action(
            brands,
            selected_cat_ids,
            source_label=_("selección vista"),
        )

    def action_sync_models_marked(self):
        """Sincroniza modelos para marcas y categorías marcadas en lote."""
        categories_selected = self._get_selected_categories()
        selected_cat_ids = set(categories_selected.mapped("syscom_id"))
        if not selected_cat_ids:
            raise UserError(_("Marca al menos una categoría en la columna Lote antes de sincronizar modelos."))

        brands = self._require_marked_brands("Sincronizar modelos marcados en lote")
        return self._run_sync_models_action(
            brands,
            selected_cat_ids,
            source_label=_("marcados en lote"),
        )

    def _run_sync_models_action(self, brands, selected_cat_ids, source_label):
        stats = self._sync_models_for_brands(brands, allowed_category_syscom_ids=selected_cat_ids)
        created = stats["created"]
        updated = stats["updated"]
        kept = stats["kept"]

        self.env["sync.syscom.log"].create({
            "name": _("Sincronización de modelos (marcas/categorías)"),
            "kind": "info",
            "message": _(
                "Origen: %(source)s. Marcas: %(brands)s. Categorías lote: %(cats)s. Productos creados: %(created)s, actualizados: %(updated)s, retenidos: %(kept)s."
            )
            % {
                "source": source_label,
                "brands": ", ".join(brands.mapped("syscom_id")),
                "cats": ", ".join(sorted(selected_cat_ids)),
                "created": created,
                "updated": updated,
                "kept": kept,
            },
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": _(
                    "Modelos sincronizados desde %(source)s: %(kept)s (creados: %(created)s, actualizados: %(updated)s)."
                )
                % {"source": source_label, "created": created, "updated": updated, "kept": kept},
                "type": "success",
                "sticky": False,
            },
        }

    def _sync_models_for_brands(self, brands, allowed_category_syscom_ids=None):
        """Sync staging products for brands, optionally filtering by allowed SYSCOM categories."""
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("Configura el token en Ajustes antes de sincronizar."))

        base_url = params.get_param("sync_syscom.syscom_base_url") or "https://developers.syscom.mx/api/v1"
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or 30)
        client = SyscomClient(base_url=base_url, token=token, timeout=timeout)

        allowed_set = set(allowed_category_syscom_ids or [])
        created = updated = kept = 0
        Product = self.env["sync.syscom.product"]
        products_synced = Product.browse([])

        for brand in brands:
            products, _pages_done, _pages_total, _total_count = self._fetch_all_brand_products(
                client,
                brand.syscom_id,
                stock=params.get_param("sync_syscom.brand_products_stock"),
            ) or []
            for product in products:
                prod_syscom_id = str(product.get("producto_id") or product.get("id") or "").strip()
                if not prod_syscom_id:
                    continue

                categories = product.get("categorías") or product.get("categorias") or []
                cat_ids = []
                match_scope = not allowed_set
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
                        if allowed_set and cat_syscom_id in allowed_set:
                            match_scope = True
                if not match_scope:
                    continue

                vals = {
                    "syscom_id": prod_syscom_id,
                    "model": product.get("modelo") or prod_syscom_id,
                    "name": product.get("titulo") or product.get("modelo") or prod_syscom_id,
                    "active": True,
                    "brand_id": brand.id,
                }
                prod_record = Product.search([("syscom_id", "=", prod_syscom_id)], limit=1)
                if prod_record:
                    prod_record.write(vals)
                    updated += 1
                else:
                    prod_record = Product.create(vals)
                    created += 1

                if cat_ids:
                    prod_record.category_ids = [(6, 0, cat_ids)]

                products_synced |= prod_record
                kept += 1

        return {
            "created": created,
            "updated": updated,
            "kept": kept,
            "products": products_synced,
        }

    def action_publish_scope_brands(self):
        """Sync models for brands selected in the current view and queue them."""
        brands = self._require_brands_for_view_action("Publicar selección vista")
        return self._run_publish_scope_brands(brands, source_label=_("selección vista"))

    def action_publish_marked_brands(self):
        brands = self._require_marked_brands("Publicar marcadas en lote")
        return self._run_publish_scope_brands(brands, source_label=_("marcadas en lote"))

    def _run_publish_scope_brands(self, brands, source_label):
        stats = self._sync_models_for_brands(brands, allowed_category_syscom_ids=None)
        queued = self.env["sync.syscom.product"].queue_products_for_background_publish(
            stats["products"],
            source_label="Marcas %s (%s)" % (source_label, ", ".join(brands.mapped("syscom_id"))),
        )
        if not queued:
            raise UserError(_("No se encontraron productos para publicar en las marcas indicadas."))

        self.env["sync.syscom.log"].sudo().create({
            "name": _("Publicación por marcas (programada)"),
            "kind": "info",
            "message": _("Origen: %(source)s. Marcas: %(brands)s. Modelos sync: %(kept)s (creados %(created)s, actualizados %(updated)s). En cola: %(queued)s.")
            % {
                "source": source_label,
                "brands": ", ".join(brands.mapped("syscom_id")),
                "kept": stats["kept"],
                "created": stats["created"],
                "updated": stats["updated"],
                "queued": queued,
            },
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": _("Publicación por marca iniciada en segundo plano desde %(source)s. En cola: %(queued)s.")
                % {"source": source_label, "queued": queued},
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

        for brand in self:
            products, _pages_done, _pages_total, _total_count = self._fetch_all_brand_products(
                client,
                brand.syscom_id,
                stock=params.get_param("sync_syscom.brand_products_stock"),
            ) or []
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
                if cat_ids:
                    prod_record.category_ids = [(6, 0, cat_ids)]

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
            products, _pages_done, _pages_total, _total_count = self._fetch_all_brand_products(
                client,
                syscom_id,
                stock=params.get_param("sync_syscom.brand_products_stock"),
            ) or []
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
