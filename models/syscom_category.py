import time
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import format_date

from .job_feedback import MENU_TRABAJOS_CATEGORIAS, MENU_TRABAJOS_SYNC
from .syscom_client import SyscomClient

# Días sin refrescarse a partir de los cuales una marca se considera rancia en los
# avisos del botón "Ver qué marcas hay en lo seleccionado". Es un umbral elegido a
# ojo y conviene saberlo: no hay ningún cron que programe el barrido de marcas --el
# 54 está apagado-- así que no existe una cadencia esperada contra la que medir.
UMBRAL_MARCA_RANCIA_DIAS = 7


class SyscomCategory(models.Model):
    _name = "sync.syscom.category"
    _inherit = ["sync.syscom.job.feedback"]
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
        """Muestra las marcas de las categorías seleccionadas, leyendo de la base local.

        Antes esto salía a SYSCOM: pedía /marcas entero y luego un /marcas/{id} por
        cada una de las 50 primeras, unas 51 llamadas por clic.  No servía: no había
        puntero de avance, así que cada clic repetía exactamente las mismas 50 marcas
        y las restantes eran inalcanzables por muchos clics que se dieran.  El
        "restantes estimadas: 784" que devolvía era una constante disfrazada de
        progreso.

        Los vínculos marca–categoría ya los mantiene ``_sync_brands_batch``, el job
        que mueve el cron 67, que sí recorre las 834 con un cursor de verdad.  Este
        botón se limita a enseñar lo que ese trabajo dejó hecho: cero llamadas.
        """
        marcas = self._marcas_de_categorias(selected_categories)
        frescura = self._frescura_de_marcas(marcas)

        ids_categorias = ", ".join(selected_categories.mapped("syscom_id"))
        if not marcas:
            titulo = _("Marcas en lo seleccionado — ninguna")
        elif frescura["rancias"]:
            titulo = _("⚠ Marcas en lo seleccionado — %(n)s · %(r)s sin refrescar desde el %(fecha)s") % {
                "n": len(marcas),
                "r": frescura["rancias"],
                "fecha": frescura["mas_antigua"],
            }
        else:
            titulo = _("Marcas en lo seleccionado — %(n)s · dato local del %(fecha)s") % {
                "n": len(marcas),
                "fecha": frescura["mas_reciente"],
            }

        self.env["sync.syscom.log"].create({
            "name": _("Marcas en lo seleccionado (consulta local)"),
            "kind": "info" if (marcas and not frescura["rancias"]) else "warn",
            "message": self._mensaje_marcas_locales(
                source_label, ids_categorias, marcas, frescura
            ),
        })

        return {
            "type": "ir.actions.act_window",
            "name": titulo,
            "res_model": "sync.syscom.brand",
            "view_mode": "list,form",
            "domain": [("id", "in", marcas.ids)],
            "target": "current",
            "context": {"create": False},
            "help": _(
                "<p class=\"o_view_nocontent_smiling_face\">Ninguna marca vinculada a esas categorías</p>"
                "<p>Puede ser que SYSCOM no tenga marcas ahí, o que el catálogo local esté "
                "incompleto. Para refrescarlo usa <b>Actualizar lista de marcas</b> en "
                "Marcas SYSCOM.</p>"
            ),
        }

    def _marcas_de_categorias(self, categories):
        """Marcas vinculadas a esas categorías, por coincidencia exacta.

        Sin descendientes, a propósito: es el alcance que ya tenía el botón cuando
        salía a la API, y cambiarlo en el mismo movimiento mezclaría dos cosas.  La
        columna «Marcas heredadas» de la lista sí incluye el subárbol
        (``brand_ids_tree``), así que las dos lecturas están disponibles.
        """
        if not categories:
            return self.env["sync.syscom.brand"].browse()
        return self.env["sync.syscom.brand"].search([("category_ids", "in", categories.ids)])

    def _frescura_de_marcas(self, marcas):
        """Cuántas marcas del alcance llevan sin refrescarse más de UMBRAL_MARCA_RANCIA_DIAS.

        Se mide sobre ``write_date`` y no sobre el último ``sync.syscom.sync.job``.
        Es deliberado: el barrido del 20/08/2026 se corrió a mano y no dejó job, así
        que el último job dice «error, 510/834» mientras los datos están completos.
        El ``write_date`` es verdad lo escriba quien lo escriba.
        """
        vacio = {"rancias": 0, "mas_antigua": "", "mas_reciente": "", "total": 0}
        if not marcas:
            return vacio

        limite = fields.Datetime.now() - timedelta(days=UMBRAL_MARCA_RANCIA_DIAS)
        fechas = [m.write_date for m in marcas if m.write_date]
        if not fechas:
            return vacio
        rancias = [f for f in fechas if f < limite]
        return {
            "total": len(marcas),
            "rancias": len(rancias),
            "mas_antigua": format_date(self.env, min(rancias or fechas)),
            "mas_reciente": format_date(self.env, max(fechas)),
        }

    def _mensaje_marcas_locales(self, source_label, ids_categorias, marcas, frescura):
        """Texto del log. Aparte para no meter cuatro ramas dentro del create()."""
        Brand = self.env["sync.syscom.brand"]
        total_catalogo = Brand.search_count([])
        limite = fields.Datetime.now() - timedelta(days=UMBRAL_MARCA_RANCIA_DIAS)
        rancias_globales = Brand.search([("write_date", "<", fields.Datetime.to_string(limite))])

        cabecera = _("Origen: %(source)s. Categorías: %(cats)s.") % {
            "source": source_label,
            "cats": ids_categorias,
        }

        if not marcas:
            return "%s\n%s\n%s" % (
                cabecera,
                _("No hay ninguna marca vinculada a esas categorías en el catálogo local."),
                _(
                    "Puede ser que SYSCOM no tenga marcas ahí, o que el catálogo local esté "
                    "incompleto: %(total)s marcas, %(rancias)s sin refrescar desde el %(fecha)s. "
                    "Para refrescar usa \"Actualizar lista de marcas\" en Marcas SYSCOM."
                ) % {
                    "total": total_catalogo,
                    "rancias": len(rancias_globales),
                    "fecha": format_date(self.env, min(rancias_globales.mapped("write_date"))) if rancias_globales else "-",
                },
            )

        lineas = [
            cabecera,
            _("Marcas encontradas: %(n)s.") % {"n": len(marcas)},
            _("Alcance: coincidencia exacta con las categorías seleccionadas, sin descendientes."),
            _("Consultado en el catálogo local, sin llamar a SYSCOM."),
        ]
        if frescura["rancias"]:
            lineas.append(
                _("Frescura del alcance: %(r)s de %(n)s llevan sin refrescarse desde el %(fecha)s.")
                % {"r": frescura["rancias"], "n": len(marcas), "fecha": frescura["mas_antigua"]}
            )
            lineas.append(
                _("Para refrescar el catálogo usa \"Actualizar lista de marcas\" en Marcas SYSCOM.")
            )
        else:
            lineas.append(
                _("Frescura del alcance: las %(n)s se refrescaron el %(fecha)s.")
                % {"n": len(marcas), "fecha": frescura["mas_reciente"]}
            )
        lineas.append(
            _("Catálogo de marcas: %(total)s en total, %(rancias)s sin refrescar desde el %(fecha)s.")
            % {
                "total": total_catalogo,
                "rancias": len(rancias_globales),
                "fecha": format_date(self.env, min(rancias_globales.mapped("write_date"))) if rancias_globales else "-",
            }
        )
        return "\n".join(lineas)

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

        # create_for_categories nunca reusa: cada clic crea un job nuevo.
        n_modelos = job.total_products
        n_cats = len(categories)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": _(
                    "Trabajo de publicación #%(id)s creado: %(modelos)s de %(cats)s, "
                    "%(alcance)s. Síguelo en %(menu)s."
                ) % {
                    "id": job.id,
                    "modelos": _("1 modelo") if n_modelos == 1 else _("%s modelos") % n_modelos,
                    "cats": _("1 categoría") if n_cats == 1 else _("%s categorías") % n_cats,
                    "alcance": _("subcategorías incluidas") if include_children else _("sin subcategorías"),
                    "menu": MENU_TRABAJOS_CATEGORIAS,
                },
                "type": "success",
                "sticky": False,
            },
        }

    def action_sync_categories_and_brands(self):
        """Programa sincronización completa en background."""
        job = self.env["sync.syscom.sync.job"].create_full_catalog_job()
        if job.env.context.get("sync_syscom_job_creado", True):
            mensaje = _(
                "Trabajo #%(id)s creado: catálogo completo (categorías, marcas y "
                "modelos). Síguelo en %(menu)s."
            ) % {"id": job.id, "menu": MENU_TRABAJOS_SYNC}
            tipo, pegajoso = "success", False
        else:
            mensaje = _(
                "Ya había un trabajo de catálogo completo en curso: %(detalle)s. "
                "No se creó otro. Síguelo en %(menu)s."
            ) % {
                "detalle": self._descripcion_job_existente(job, con_etapa=True),
                "menu": MENU_TRABAJOS_SYNC,
            }
            tipo, pegajoso = "warning", True
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": mensaje,
                "type": tipo,
                "sticky": pegajoso,
            },
        }

    def cron_sync_categories(self):
        """Compatibilidad hacia atrás: delega al worker de jobs."""
        self.env["sync.syscom.sync.job"].cron_process_sync_jobs()

    def action_start_category_sync(self):
        job = self.env["sync.syscom.sync.job"].create_categories_only_job()
        if job.env.context.get("sync_syscom_job_creado", True):
            mensaje = _(
                "Trabajo #%(id)s creado: sincronización de categorías. Síguelo en %(menu)s."
            ) % {"id": job.id, "menu": MENU_TRABAJOS_SYNC}
            tipo, pegajoso = "success", False
        else:
            mensaje = _(
                "Ya había un trabajo de categorías en curso: %(detalle)s. "
                "No se creó otro. Síguelo en %(menu)s."
            ) % {
                "detalle": self._descripcion_job_existente(job),
                "menu": MENU_TRABAJOS_SYNC,
            }
            tipo, pegajoso = "warning", True
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": mensaje,
                "type": tipo,
                "sticky": pegajoso,
            },
        }

    def action_start_sync_pipeline(self):
        return self.action_sync_categories_and_brands()

    def action_fix_accounting_accounts(self):
        """Assign SYSCOM accounting accounts to all linked product.category records."""
        categories = self.search([("product_category_id", "!=", False)])
        updated = 0
        for cat in categories:
            self.env["sync.syscom.product"]._assign_syscom_category_accounts(cat.product_category_id)
            updated += 1
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Cuentas contables SYSCOM"),
                "message": _("Cuentas contables asignadas a %d categorías de producto.") % updated,
                "type": "success",
                "sticky": False,
            },
        }
