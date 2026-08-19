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

# Ruta de menú que se cita en los mensajes, para que el usuario sepa dónde está
# el detalle de lo que acaba de correr.
MENU_LOGS = "SyncSyscom › Logs"

# Marcas que se detallan una por una en el log antes de resumir el resto. El cron
# de purga (80) puede estar apagado, así que un clic sobre 300 marcas no debe
# dejar 300 líneas.
MAX_MARCAS_DETALLE_LOG = 20


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
        # Se cuenta al consumir, no al salir: los tres `break` de abajo ocurren DESPUÉS
        # de haber usado la página, así que deducirlo de `page` al final reportaba una
        # página de menos en todas las salidas por criterio de terminación.
        pages_done = 0
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
            pages_done += 1

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
        return all_products, pages_done, total_pages, total_count

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
                "name": brand.get("nombre") or detail.get("titulo") or syscom_id,
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
                "name": brand.get("nombre") or detail.get("titulo") or syscom_id,
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
        """Sincroniza modelos para marcas seleccionadas en la vista.

        Si hay categorías marcadas en lote filtra por ellas; si no, sincroniza
        todos los modelos de las marcas seleccionadas sin filtro de categoría.
        """
        categorias_alcance = self._get_selected_categories()

        brands = self._require_brands_for_view_action("Sincronizar modelos selección vista")
        return self._run_sync_models_action(
            brands,
            categorias_alcance,
            source_label=_("selección vista"),
        )

    def action_sync_models_marked(self):
        """Sincroniza modelos para marcas marcadas en lote.

        Si hay categorías marcadas en lote filtra por ellas; si no, sincroniza
        todos los modelos de las marcas marcadas sin filtro de categoría.
        """
        categorias_alcance = self._get_selected_categories()

        brands = self._require_marked_brands("Sincronizar modelos marcados en lote")
        return self._run_sync_models_action(
            brands,
            categorias_alcance,
            source_label=_("marcados en lote"),
        )

    def _etiqueta_marcas(self, brands, limite=3):
        """Lista de marcas por ID SYSCOM, recortada para que quepa en el nombre del log."""
        ids = brands.mapped("syscom_id")
        if len(ids) <= limite:
            return ", ".join(ids)
        return _("%(lista)s y %(resto)s más") % {
            "lista": ", ".join(ids[:limite]),
            "resto": len(ids) - limite,
        }

    def _etiquetas_categorias(self, categorias, limite=None):
        """Nombra categorías como 'Nombre (id)'.

        El ID no es decoración: hay categorías distintas con el mismo nombre —369 y
        65674 se llaman las dos "Control de Acceso"—, así que sin él la lista parece
        traer un duplicado.
        """
        etiquetas = [
            "%s (%s)" % (categoria.name or _("Sin nombre"), categoria.syscom_id)
            for categoria in categorias
        ]
        if limite is None or len(etiquetas) <= limite:
            return ", ".join(etiquetas)
        return _("%(lista)s y %(resto)s más") % {
            "lista": ", ".join(etiquetas[:limite]),
            "resto": len(etiquetas) - limite,
        }

    def _mensaje_log_sync_modelos(self, stats, categorias_alcance, source_label):
        """Arma el detalle que va al log: totales, suma de control y desglose por marca."""
        lineas = [_("Origen: %s.") % source_label]
        if categorias_alcance:
            lineas.append(
                _("Alcance: solo categorías marcadas — %s.")
                % self._etiquetas_categorias(categorias_alcance)
            )
        else:
            lineas.append(
                _("Alcance: todas las categorías de la marca (ninguna categoría marcada en lote).")
            )
        lineas.append(
            _(
                "Totales: la API devolvió %(traidos)s; sincronizados %(kept)s (creados %(created)s, "
                "actualizados %(updated)s); descartados por filtro de categorías %(descartados)s; "
                "sin ID utilizable %(sin_id)s."
            )
            % {
                "traidos": stats["traidos"],
                "kept": stats["kept"],
                "created": stats["created"],
                "updated": stats["updated"],
                "descartados": stats["descartados_filtro"],
                "sin_id": stats["sin_id"],
            }
        )
        # Se imprime para que un humano pueda sumarla a ojo. No se valida con un assert:
        # cualquier excepción aquí revertiría el sync completo, que es justo el modo de
        # falla del 429 que queremos dejar de repetir.
        lineas.append(
            _(
                "Suma de control: %(traidos)s = %(kept)s sincronizados + %(descartados)s descartados "
                "+ %(sin_id)s sin ID."
            )
            % {
                "traidos": stats["traidos"],
                "kept": stats["kept"],
                "descartados": stats["descartados_filtro"],
                "sin_id": stats["sin_id"],
            }
        )
        if stats["categorias_no_locales"]:
            lineas.append(
                _(
                    "Aviso: %s productos traían categorías que no existen en la base local; esas "
                    "categorías no pudieron participar en el filtro."
                )
                % stats["categorias_no_locales"]
            )
        lineas.append(_("Por marca:"))
        for marca in stats["por_marca"][:MAX_MARCAS_DETALLE_LOG]:
            lineas.append(
                _(
                    "  - %(marca)s: API %(traidos)s (páginas %(hechas)s/%(paginas)s, total reportado "
                    "%(reportado)s) → sincronizados %(kept)s (creados %(created)s, actualizados "
                    "%(updated)s), descartados por filtro %(descartados)s, sin ID %(sin_id)s."
                )
                % {
                    "marca": marca["marca"],
                    "traidos": marca["traidos"],
                    "hechas": marca["paginas_hechas"],
                    "paginas": marca["paginas_total"] or "¿?",
                    "reportado": marca["total_reportado"] or "¿?",
                    "kept": marca["kept"],
                    "created": marca["created"],
                    "updated": marca["updated"],
                    "descartados": marca["descartados_filtro"],
                    "sin_id": marca["sin_id"],
                }
            )
        resto = len(stats["por_marca"]) - MAX_MARCAS_DETALLE_LOG
        if resto > 0:
            lineas.append(_("  … y %s marcas más; ver Totales arriba.") % resto)
        return "\n".join(lineas)

    def _notificacion_sync_modelos(self, stats, categorias_alcance, source_label, marcas_label):
        """Redacta la notificación y decide su color.

        Con ``traidos`` en la mano, un resultado en cero por fin puede decir cuál de
        las tres causas fue: la API no trajo nada, el filtro lo descartó todo, o los
        productos venían sin ID. Antes las tres salían como el mismo verde.
        """
        traidos = stats["traidos"]
        kept = stats["kept"]
        descartados = stats["descartados_filtro"]
        sin_id = stats["sin_id"]
        categorias = self._etiquetas_categorias(categorias_alcance, limite=2)
        detalle = _("Detalle en %s.") % MENU_LOGS
        detalle_por_marca = _("Detalle por marca en %s.") % MENU_LOGS

        sincronizados = _(
            "Modelos sincronizados desde %(source)s: %(kept)s de %(traidos)s que devolvió la API "
            "(creados %(created)s, actualizados %(updated)s)."
        ) % {
            "source": source_label,
            "kept": kept,
            "traidos": traidos,
            "created": stats["created"],
            "updated": stats["updated"],
        }

        if kept and not descartados:
            tipo = "success"
            partes = [sincronizados]
        elif kept:
            tipo = "warning"
            partes = [
                sincronizados,
                _("El filtro por categorías marcadas descartó %s.") % descartados,
                _("Categorías activas: %s.") % categorias,
                detalle_por_marca,
            ]
        elif not traidos:
            tipo = "warning"
            partes = [
                _("La API no devolvió productos para las marcas indicadas (%s). No se sincronizó nada.")
                % marcas_label,
                detalle,
            ]
        elif descartados == traidos:
            tipo = "warning"
            partes = [
                _(
                    "La API devolvió %(traidos)s productos de %(marcas)s y el filtro por categorías "
                    "marcadas descartó los %(descartados)s. No se sincronizó nada."
                )
                % {"traidos": traidos, "marcas": marcas_label, "descartados": descartados},
                _("Categorías activas: %s.") % categorias,
                _("Si querías traer la marca completa, desmarca esas categorías."),
                detalle,
            ]
        elif sin_id == traidos:
            tipo = "warning"
            partes = [
                _(
                    "La API devolvió %(traidos)s productos de %(marcas)s, pero ninguno traía un ID "
                    "utilizable. No se sincronizó nada."
                )
                % {"traidos": traidos, "marcas": marcas_label},
                detalle,
            ]
        else:
            tipo = "warning"
            partes = [
                _(
                    "No se sincronizó ningún modelo de %(marcas)s. La API devolvió %(traidos)s: "
                    "%(descartados)s descartados por el filtro de categorías marcadas, %(sin_id)s "
                    "sin ID utilizable."
                )
                % {
                    "marcas": marcas_label,
                    "traidos": traidos,
                    "descartados": descartados,
                    "sin_id": sin_id,
                },
                detalle,
            ]

        if stats["categorias_no_locales"]:
            partes.append(
                _(
                    "Además, %s productos traían categorías que no existen en la base local y no "
                    "pudieron evaluarse contra el filtro."
                )
                % stats["categorias_no_locales"]
            )

        return " ".join(partes), tipo

    def _run_sync_models_action(self, brands, categorias_alcance, source_label):
        allowed_cat_ids = set(categorias_alcance.mapped("syscom_id")) or None
        stats = self._sync_models_for_brands(brands, allowed_category_syscom_ids=allowed_cat_ids)
        marcas_label = self._etiqueta_marcas(brands)
        hubo_descartes = bool(stats["descartados_filtro"] or stats["categorias_no_locales"])

        self.env["sync.syscom.log"].create({
            # Nombre distintivo y con las marcas dentro: en la lista de Logs esta línea
            # compite con cientos de entradas de los crons de fondo.
            "name": _("Traer productos de marcas: %s") % marcas_label,
            "kind": "warn" if (not stats["kept"] or hubo_descartes) else "info",
            "message": self._mensaje_log_sync_modelos(stats, categorias_alcance, source_label),
        })

        mensaje, tipo = self._notificacion_sync_modelos(
            stats,
            categorias_alcance,
            source_label,
            marcas_label,
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": mensaje,
                "type": tipo,
                # Un resultado que hay que investigar no puede desvanecerse solo.
                "sticky": tipo == "warning",
            },
        }

    def _sync_models_for_brands(self, brands, allowed_category_syscom_ids=None):
        """Sync staging products for brands, optionally filtering by allowed SYSCOM categories.

        Además de los contadores de escritura devuelve las cifras que permiten saber
        por qué un producto no llegó a la base: cuántos trajo la API, cuántos descartó
        el filtro de categorías y cuántos venían sin ID usable. Sin ellas un resultado
        en cero es indistinguible de una marca vacía, que fue exactamente lo que pasó
        con 3m el 18/08/2026.
        """
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("Configura el token en Ajustes antes de sincronizar."))

        base_url = params.get_param("sync_syscom.syscom_base_url") or "https://developers.syscom.mx/api/v1"
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or 30)
        client = SyscomClient(base_url=base_url, token=token, timeout=timeout)

        allowed_set = set(allowed_category_syscom_ids or [])
        created = updated = kept = 0
        traidos = descartados_filtro = sin_id = categorias_no_locales = 0
        por_marca = []
        Product = self.env["sync.syscom.product"]
        products_synced = Product.browse([])

        for brand in brands:
            products, paginas_hechas, paginas_total, total_reportado = self._fetch_all_brand_products(
                client,
                brand.syscom_id,
                stock=params.get_param("sync_syscom.brand_products_stock"),
            )
            marca = {
                "marca": brand.syscom_id,
                "traidos": len(products),
                "created": 0,
                "updated": 0,
                "kept": 0,
                "descartados_filtro": 0,
                "sin_id": 0,
                "categorias_no_locales": 0,
                "paginas_hechas": paginas_hechas,
                "paginas_total": paginas_total,
                "total_reportado": total_reportado,
            }
            por_marca.append(marca)
            traidos += marca["traidos"]

            for product in products:
                prod_syscom_id = str(product.get("producto_id") or product.get("id") or "").strip()
                if not prod_syscom_id:
                    sin_id += 1
                    marca["sin_id"] += 1
                    continue

                categories = product.get("categorías") or product.get("categorias") or []
                cat_ids = []
                match_scope = not allowed_set
                falta_categoria_local = False
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
                    else:
                        # La categoría existe en SYSCOM pero no en la base local, así que
                        # no puede participar en el filtro: si este producto se descarta,
                        # el descarte podría ser un falso negativo.
                        falta_categoria_local = True
                if falta_categoria_local:
                    categorias_no_locales += 1
                    marca["categorias_no_locales"] += 1
                if not match_scope:
                    descartados_filtro += 1
                    marca["descartados_filtro"] += 1
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
                    marca["updated"] += 1
                else:
                    prod_record = Product.create(vals)
                    created += 1
                    marca["created"] += 1

                if cat_ids:
                    prod_record.category_ids = [(6, 0, cat_ids)]

                products_synced |= prod_record
                kept += 1
                marca["kept"] += 1

        return {
            "created": created,
            "updated": updated,
            "kept": kept,
            "traidos": traidos,
            "descartados_filtro": descartados_filtro,
            "sin_id": sin_id,
            "categorias_no_locales": categorias_no_locales,
            "por_marca": por_marca,
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
        encolado = self.env["sync.syscom.product"].queue_products_for_background_publish(
            stats["products"],
            source_label="Marcas %s (%s)" % (source_label, ", ".join(brands.mapped("syscom_id"))),
        )
        marcas_label = self._etiqueta_marcas(brands)
        omitidos = encolado["omitidos_abandonados"]
        encolados = encolado["encolados"]

        self.env["sync.syscom.log"].sudo().create({
            "name": _("Publicación por marcas: %s") % marcas_label,
            "kind": "warn" if (not encolados or omitidos) else "info",
            "message": _(
                "Origen: %(source)s. Marcas: %(brands)s. La API devolvió %(traidos)s; modelos sync: "
                "%(kept)s (creados %(created)s, actualizados %(updated)s). En cola: %(queued)s. "
                "Omitidos por abandonados: %(omitidos)s."
            )
            % {
                "source": source_label,
                "brands": ", ".join(brands.mapped("syscom_id")),
                "traidos": stats["traidos"],
                "kept": stats["kept"],
                "created": stats["created"],
                "updated": stats["updated"],
                "queued": encolados,
                "omitidos": omitidos,
            },
        })

        # Antes esto era un `raise UserError` cuando no se encolaba nada. La excepción
        # revertía el sync que se acababa de hacer —cuota gastada, cero escrito, ni el
        # log— que es el mismo modo de falla del 429. Ahora avisa sin tirar el trabajo.
        if encolados:
            mensaje = _("Publicación por marca iniciada en segundo plano desde %(source)s. En cola: %(queued)s.") % {
                "source": source_label,
                "queued": encolados,
            }
        elif stats["traidos"]:
            mensaje = _(
                "No se encoló nada para %(marcas)s. La API devolvió %(traidos)s productos y se "
                "sincronizaron %(kept)s, pero ninguno quedó publicable."
            ) % {"marcas": marcas_label, "traidos": stats["traidos"], "kept": stats["kept"]}
        else:
            mensaje = _("La API no devolvió productos para %(marcas)s. No se encoló nada.") % {
                "marcas": marcas_label,
            }
        if omitidos:
            mensaje += _(
                " Se omitieron %s productos abandonados: agotaron sus reintentos y no se resucitan"
                " desde aquí. Para reintentarlos, selecciónalos en Modelos y usa 'Reiniciar estado"
                " de publicación'."
            ) % omitidos
        alerta = not encolados or bool(omitidos)

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": mensaje,
                "type": "warning" if alerta else "success",
                "sticky": alerta,
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
