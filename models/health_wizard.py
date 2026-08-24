import ast
from collections import Counter
from datetime import timedelta

from odoo import _, api, fields, models

from .job_feedback import _clave_pausa, contar_aplazamientos, segundos_de_pausa_restantes
from .product_data_job import RUTA_PRODUCT_DATA
from .sync_job import RUTA_SYNC_JOBS
from .syscom_product import RUTA_PUBLICAR, RUTA_STOCK

# Minutos por unidad de intervalo de ir.cron. Duplicado deliberado del que ya usa
# ResConfigSettings._minutos_entre_lotes_del_cron: aquí aplica a los 12 crons del
# módulo, no solo al 58, y es un mapeo estático de Odoo que no vale la pena compartir
# entre dos módulos por 5 líneas.
_FACTOR_MINUTOS = {"minutes": 1, "hours": 60, "days": 1440, "weeks": 10080, "months": 43200}

# (ruta, etiqueta) de las cuatro rutas que pueden quedar en pausa por un 429. Los
# nombres de ruta se importan de donde ya viven -no se repiten a mano- para que un
# cambio de nombre de ruta rompa la carga del módulo en vez de desalinear el panel
# en silencio.
#
# Sin `_()`: son literales de módulo, evaluados una sola vez al importar. `_()` ahí
# no tiene un `self` en el frame de quien llama -_get_lang lo busca así- y cae al
# último recurso: WARNING con traza completa en el arranque y sin traducir de
# todas formas. Mismo motivo por el que MENU_TRABAJOS_* en job_feedback.py son
# strings planos.
_RUTAS_RATE_LIMIT = [
    (RUTA_STOCK, "Refresco de stock/precios (cron 58)"),
    (RUTA_PUBLICAR, "Publicar seleccionados (cron 60)"),
    (RUTA_SYNC_JOBS, "Catálogo: marcas y modelos (cron 67)"),
    (RUTA_PRODUCT_DATA, "Datos extendidos (cron 69)"),
]

# (modelo técnico, etiqueta, si agrupa también por job_type). Los tres últimos no
# tienen campo job_type -solo sync.syscom.sync.job lo tiene-, así que ahí se agrupa
# solo por estado.
_MODELOS_JOB = [
    ("sync.syscom.sync.job", "Sync catálogo (marcas/modelos)", True),
    ("sync.syscom.cost.job", "Recálculo de costos", False),
    ("sync.syscom.dropship.job", "Dropshipping", False),
    ("sync.syscom.product.data.job", "Datos extendidos", False),
]

# (clave, etiqueta, domain extra) de los motivos de abandono que ya se han visto.
# El texto de sync_error trae datos variables (el stock exacto, el id de marca...)
# así que agrupar por el texto crudo fragmenta un solo motivo en varias filas; se
# agrupa por prefijo en su lugar. "Otro" es el resto -lo no capturado por ninguno
# de los prefijos de arriba-, para que la suma de las filas cuadre siempre con el
# total de abandonados, incluso cuando aparezca un motivo nuevo que nadie anticipó.
_BUCKETS_ABANDONO = [
    ("stock_insuficiente", "Stock insuficiente en SYSCOM",
     [("sync_error", "like", "Stock insuficiente%")]),
    ("http_404", "Retirado de SYSCOM (HTTP 404)",
     [("sync_error", "like", "HTTP 404%")]),
    ("http_429", "Rate limit al momento de abandonar (HTTP 429)",
     [("sync_error", "like", "HTTP 429%")]),
    ("sin_motivo", "Sin motivo registrado",
     [("sync_error", "in", [False, ""])]),
]
_DOMAIN_OTRO_ABANDONO = [
    ("sync_error", "not like", "Stock insuficiente%"),
    ("sync_error", "not like", "HTTP 404%"),
    ("sync_error", "not like", "HTTP 429%"),
    ("sync_error", "not in", [False, ""]),
]

# (clave, etiqueta) de los motivos de "publish_state=done pero is_published=False"
# -el proceso terminó sin fallo, pero el producto no se ve en la tienda-. Medido el
# 24/08/2026 contra el catálogo real: 765 casos de 4,368 en 'done' (17.5%), repartidos
# en los tres primeros, con el cuarto en 0 -pero comprobado en la propia consulta, no
# asumido: ver el motivo en _build_publish_gap_lines.
#
# OJO, "stock_con_mensaje" usa un texto DISTINTO al de _BUCKETS_ABANDONO: "Sin stock
# suficiente" (este) es el mensaje que escribe la ruta de publicación cuando decide no
# publicar; "Stock insuficiente en SYSCOM" (el de abandono) es el que escribe el
# abandono por reintentos agotados. Se parecen y no son el mismo mensaje -confundirlos
# rompe el filtro en silencio, ya pasó una vez al escribir esta consulta.
_BUCKET_RETIRADO = "retirado"
_BUCKET_STOCK_CON_MENSAJE = "stock_con_mensaje"
_BUCKET_STOCK_SIN_MENSAJE = "stock_sin_mensaje"
_BUCKET_SIN_EXPLICACION = "sin_explicacion"
_BUCKETS_PUBLISH_GAP = [
    (_BUCKET_RETIRADO, "Retirado de SYSCOM (HTTP 404) — auto-despublicado"),
    (_BUCKET_STOCK_CON_MENSAJE, "Stock insuficiente (con mensaje registrado)"),
    (_BUCKET_STOCK_SIN_MENSAJE, "Stock insuficiente (confirmado ahora, sin mensaje)"),
    (_BUCKET_SIN_EXPLICACION, "Sin explicación (ni retirado, ni stock bajo, ni mensaje)"),
]


class SyncSyscomHealthOpenableLine(models.AbstractModel):
    """Mixin para líneas que abren, con un botón, la lista real que cuentan.

    `domain` se guarda como `repr()` de una lista de tuplas -no como dominio de
    Odoo serializado-, así que se relee con `ast.literal_eval`, no con `safe_eval`:
    lo escribe el propio wizard, nunca el usuario, y no hay expresiones que evaluar.
    """
    _name = "sync.syscom.health.openable.line"
    _description = "Línea de salud con acción de apertura"

    res_model = fields.Char(string="Modelo", required=True)
    domain = fields.Char(string="Dominio", required=True)
    # Vacío -> la lista por defecto de `res_model`. Se necesita explícito cuando esa
    # lista por defecto no muestra el dato que justifica la fila: p. ej. la lista de
    # product.template de serie no trae columnas SYSCOM, así que la fila "stock
    # insuficiente sin mensaje" sería un callejón sin salida -se abre, y ahí tampoco
    # se ve por qué- sin una vista propia que sí muestre el stock.
    list_view_xmlid = fields.Char(string="Vista de lista")

    def action_open_records(self):
        self.ensure_one()
        action = {
            "type": "ir.actions.act_window",
            "name": self.display_name,
            "res_model": self.res_model,
            "view_mode": "list,form",
            "domain": ast.literal_eval(self.domain),
            "target": "current",
        }
        if self.list_view_xmlid:
            vista = self.env.ref(self.list_view_xmlid, raise_if_not_found=False)
            if vista:
                action["views"] = [(vista.id, "list"), (False, "form")]
        return action


class SyncSyscomHealthWizard(models.TransientModel):
    _name = "sync.syscom.health.wizard"
    _description = "Salud de sincronización SYSCOM"

    computed_at = fields.Datetime(string="Calculado el", readonly=True)
    cron_line_ids = fields.One2many(
        "sync.syscom.health.cron.line", "wizard_id", string="Crons", readonly=True)
    job_line_ids = fields.One2many(
        "sync.syscom.health.job.line", "wizard_id", string="Trabajos", readonly=True)
    abandoned_line_ids = fields.One2many(
        "sync.syscom.health.abandoned.line", "wizard_id", string="Abandonados", readonly=True)
    publish_gap_line_ids = fields.One2many(
        "sync.syscom.health.publish.gap.line", "wizard_id",
        string="Terminados sin publicar", readonly=True)
    rate_limit_line_ids = fields.One2many(
        "sync.syscom.health.rate.limit.line", "wizard_id", string="Pausas por rate limit",
        readonly=True)

    @api.model
    def default_get(self, fields_list):
        """Puebla las 4 tablas ya en el `default_get`, no solo en `create`.

        Abrir la acción desde el menú renderiza un registro "Nuevo" (NewId) en el
        cliente, que se arma con `default_get` y **no** pasa por `create` hasta que
        algo fuerza un guardado -por eso "Actualizar" parecía ser el único botón que
        poblaba algo: es un botón de tipo objeto, y esos fuerzan el guardado antes de
        llamar al método-. Sin este override, abrir el panel mostraba las 4 tablas
        vacías hasta el primer clic.
        """
        res = super().default_get(fields_list)
        if "cron_line_ids" in fields_list:
            res["cron_line_ids"] = [(0, 0, vals) for vals in self._build_cron_lines()]
        if "job_line_ids" in fields_list:
            res["job_line_ids"] = [(0, 0, vals) for vals in self._build_job_lines()]
        if "abandoned_line_ids" in fields_list:
            res["abandoned_line_ids"] = [(0, 0, vals) for vals in self._build_abandoned_lines()]
        if "publish_gap_line_ids" in fields_list:
            res["publish_gap_line_ids"] = [
                (0, 0, vals) for vals in self._build_publish_gap_lines()]
        if "rate_limit_line_ids" in fields_list:
            res["rate_limit_line_ids"] = [(0, 0, vals) for vals in self._build_rate_limit_lines()]
        if "computed_at" in fields_list:
            res["computed_at"] = fields.Datetime.now()
        return res

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._refresh_lines()
        return records

    def action_refresh(self):
        self._refresh_lines()
        return True

    def _refresh_lines(self):
        for wizard in self:
            wizard.cron_line_ids.unlink()
            wizard.job_line_ids.unlink()
            wizard.abandoned_line_ids.unlink()
            wizard.publish_gap_line_ids.unlink()
            wizard.rate_limit_line_ids.unlink()
            Cron = self.env["sync.syscom.health.cron.line"]
            Job = self.env["sync.syscom.health.job.line"]
            Abandoned = self.env["sync.syscom.health.abandoned.line"]
            PublishGap = self.env["sync.syscom.health.publish.gap.line"]
            RateLimit = self.env["sync.syscom.health.rate.limit.line"]
            Cron.create([dict(vals, wizard_id=wizard.id) for vals in wizard._build_cron_lines()])
            Job.create([dict(vals, wizard_id=wizard.id) for vals in wizard._build_job_lines()])
            Abandoned.create(
                [dict(vals, wizard_id=wizard.id) for vals in wizard._build_abandoned_lines()])
            PublishGap.create(
                [dict(vals, wizard_id=wizard.id) for vals in wizard._build_publish_gap_lines()])
            RateLimit.create(
                [dict(vals, wizard_id=wizard.id) for vals in wizard._build_rate_limit_lines()])
            wizard.computed_at = fields.Datetime.now()

    def _crons_del_modulo(self):
        """Los ir.cron declarados por este módulo, vía ir.model.data -no por nombre,
        que ya cambió de texto más de una vez en la historia de este código."""
        Data = self.env["ir.model.data"].sudo()
        filas = Data.search([("module", "=", "sync_syscom"), ("model", "=", "ir.cron")])
        return self.env["ir.cron"].sudo().browse(filas.mapped("res_id")).exists().sorted("id")

    def _build_cron_lines(self):
        ahora = fields.Datetime.now()
        vals_list = []
        for cron in self._crons_del_modulo():
            factor = _FACTOR_MINUTOS.get(cron.interval_type, 0)
            intervalo_minutos = (cron.interval_number or 0) * factor or 1
            umbral_minutos = max(3 * intervalo_minutos, 10)

            retraso_minutos = False
            estado = "inactivo"
            if cron.active:
                estado = "ok"
                if cron.nextcall:
                    retraso = (ahora - cron.nextcall).total_seconds() / 60
                    if retraso > 0:
                        retraso_minutos = retraso
                        if retraso > umbral_minutos:
                            estado = "vencido"

            vals_list.append({
                "cron_id": cron.id,
                "cron_active": cron.active,
                "intervalo": "%s %s" % (cron.interval_number, cron.interval_type),
                "nextcall": cron.nextcall,
                "lastcall": cron.lastcall,
                "retraso_minutos": retraso_minutos,
                "estado": estado,
            })
        return vals_list

    def _build_job_lines(self):
        vals_list = []
        for modelo, etiqueta, con_tipo in _MODELOS_JOB:
            Modelo = self.env[modelo]
            campos = ["job_type", "state"] if con_tipo else ["state"]
            filas = Modelo.search_read([], campos)
            # Tablas de decenas de filas como mucho: se cuenta en Python con Counter
            # en vez de read_group, para no depender de que la clave de conteo
            # ('__count' en Odoo 19) no cambie de nombre entre versiones.
            contador = Counter(
                (fila.get("job_type") or "", fila["state"]) for fila in filas
            )
            for (job_type, state), cantidad in sorted(contador.items()):
                domain = [("state", "=", state)]
                if job_type:
                    domain.append(("job_type", "=", job_type))
                vals_list.append({
                    "res_model": modelo,
                    "modelo_label": etiqueta,
                    "job_type": job_type,
                    "state": state,
                    "cantidad": cantidad,
                    "domain": repr(domain),
                })
        return vals_list

    def _build_abandoned_lines(self):
        Product = self.env["sync.syscom.product"]
        base = [("publish_state", "=", "abandoned")]
        vals_list = []
        for clave, etiqueta, extra in _BUCKETS_ABANDONO:
            domain = base + extra
            vals_list.append({
                "res_model": "sync.syscom.product",
                "motivo": etiqueta,
                "bucket_key": clave,
                "cantidad": Product.search_count(domain),
                "domain": repr(domain),
            })
        domain_otro = base + _DOMAIN_OTRO_ABANDONO
        vals_list.append({
            "res_model": "sync.syscom.product",
            "motivo": _("Otro"),
            "bucket_key": "otro",
            "cantidad": Product.search_count(domain_otro),
            "domain": repr(domain_otro),
        })
        return vals_list

    def _build_publish_gap_lines(self):
        """publish_state='done' pero is_published=False: el proceso dice terminado,
        el cliente no lo ve. Sin relación formal (Many2one) entre los dos modelos
        -se emparejan por valor, syscom_id contra syscom_product_id-, así que no hay
        dominio de Odoo que lo exprese directo; se resuelve con una sola consulta SQL
        que ya trae los ids de product.template agrupados por motivo, no fila por
        fila. Medido con EXPLAIN ANALYZE el 24/08/2026: ~56 ms sobre 41,734 filas de
        staging y 4,369 plantillas SYSCOM.

        El tercer bucket compara el stock actual contra el mínimo de verdad -no es un
        cajón de sastre "todo lo que sobra"-, con la misma fórmula que usa el refresco
        de stock para decidir si republica: stock SYSCOM alcanza el mínimo, O hay
        stock propio de HERGON que blinda al producto (`syscom_product.py:1463`). Lo
        que no encaja en ningún bucket anterior cae en "sin explicación", no se
        confunde con "stock insuficiente": hoy da 0, pero un producto despublicado a
        mano con stock sano -la corrección manual es el mecanismo normal de esta
        tienda, no una excepción- caería ahí, marcado como lo que es, no disfrazado
        de problema de stock que no tiene.
        """
        min_stock = int(
            self.env["ir.config_parameter"].sudo().get_param("sync_syscom.min_stock") or 1)
        self.env.cr.execute("""
            SELECT
              CASE
                WHEN pt.syscom_sync_error LIKE %(retirado)s THEN %(bucket_retirado)s
                WHEN sp.sync_error LIKE %(con_mensaje)s THEN %(bucket_con_mensaje)s
                WHEN pt.syscom_stock_new < %(min_stock)s
                     AND coalesce(pt.syscom_stock_propio, 0) = 0 THEN %(bucket_sin_mensaje)s
                ELSE %(bucket_sin_explicacion)s
              END AS bucket,
              array_agg(pt.id) AS ids
            FROM sync_syscom_product sp
            JOIN product_template pt ON pt.syscom_product_id = sp.syscom_id
            WHERE sp.publish_state = 'done' AND pt.is_published = false
            GROUP BY 1
        """, {
            "retirado": "HTTP 404%",
            "con_mensaje": "Sin stock suficiente%",
            "min_stock": min_stock,
            "bucket_retirado": _BUCKET_RETIRADO,
            "bucket_con_mensaje": _BUCKET_STOCK_CON_MENSAJE,
            "bucket_sin_mensaje": _BUCKET_STOCK_SIN_MENSAJE,
            "bucket_sin_explicacion": _BUCKET_SIN_EXPLICACION,
        })
        ids_por_bucket = dict(self.env.cr.fetchall())

        vals_list = []
        for clave, etiqueta in _BUCKETS_PUBLISH_GAP:
            ids = ids_por_bucket.get(clave) or []
            domain = [("id", "in", ids)]
            vals_list.append({
                "res_model": "product.template",
                "list_view_xmlid": "sync_syscom.view_product_template_health_list",
                "motivo": etiqueta,
                "bucket_key": clave,
                "cantidad": len(ids),
                "domain": repr(domain),
            })
        return vals_list

    def _build_rate_limit_lines(self):
        params = self.env["ir.config_parameter"].sudo()
        vals_list = []
        for ruta, etiqueta in _RUTAS_RATE_LIMIT:
            restantes = segundos_de_pausa_restantes(self.env, ruta)
            pausa_hasta_raw = params.get_param(_clave_pausa(ruta))
            pausa_hasta = fields.Datetime.to_datetime(pausa_hasta_raw) if pausa_hasta_raw else False
            vals_list.append({
                "ruta": ruta,
                "ruta_label": etiqueta,
                "pausado": restantes > 0,
                "pausa_hasta": pausa_hasta,
                "aplazamientos": contar_aplazamientos(self.env, ruta),
            })
        return vals_list


class SyncSyscomHealthCronLine(models.TransientModel):
    _name = "sync.syscom.health.cron.line"
    _description = "Salud de sincronización — línea de cron"
    _order = "estado desc, cron_id"

    wizard_id = fields.Many2one("sync.syscom.health.wizard", required=True, ondelete="cascade")
    cron_id = fields.Many2one("ir.cron", string="Cron", required=True)
    # Deliberadamente NO se llama "active": ese nombre es mágico en Odoo -cualquier
    # modelo con un campo así se apunta solo al archivado, y search()/los O2M lo
    # filtran a active=True salvo que se pida lo contrario por contexto. Con ese
    # nombre, este wizard escondía en silencio justo los crons apagados: los cuatro
    # que son el motivo de que exista el panel. Encontrado al verificar por ORM
    # después del -u, no antes.
    cron_active = fields.Boolean(string="Activo")
    intervalo = fields.Char(string="Intervalo")
    nextcall = fields.Datetime(string="Próxima corrida")
    lastcall = fields.Datetime(string="Última corrida")
    retraso_minutos = fields.Float(string="Retraso (min)")
    estado = fields.Selection(
        [("ok", "Al día"), ("vencido", "Vencido"), ("inactivo", "Inactivo")],
        string="Estado",
    )

    def action_open_cron(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.cron_id.display_name,
            "res_model": "ir.cron",
            "res_id": self.cron_id.id,
            "view_mode": "form",
            "target": "current",
        }


class SyncSyscomHealthJobLine(models.TransientModel):
    _name = "sync.syscom.health.job.line"
    _inherit = ["sync.syscom.health.openable.line"]
    _description = "Salud de sincronización — línea de trabajos"
    _order = "modelo_label, state"

    wizard_id = fields.Many2one("sync.syscom.health.wizard", required=True, ondelete="cascade")
    modelo_label = fields.Char(string="Trabajo")
    job_type = fields.Char(string="Tipo")
    state = fields.Char(string="Estado")
    cantidad = fields.Integer(string="Cantidad")


class SyncSyscomHealthAbandonedLine(models.TransientModel):
    _name = "sync.syscom.health.abandoned.line"
    _inherit = ["sync.syscom.health.openable.line"]
    _description = "Salud de sincronización — línea de abandonados"
    _order = "cantidad desc"

    wizard_id = fields.Many2one("sync.syscom.health.wizard", required=True, ondelete="cascade")
    motivo = fields.Char(string="Motivo")
    bucket_key = fields.Char(string="Clave interna")
    cantidad = fields.Integer(string="Cantidad")


class SyncSyscomHealthPublishGapLine(models.TransientModel):
    _name = "sync.syscom.health.publish.gap.line"
    _inherit = ["sync.syscom.health.openable.line"]
    _description = "Salud de sincronización — línea de terminados sin publicar"
    _order = "cantidad desc"

    wizard_id = fields.Many2one("sync.syscom.health.wizard", required=True, ondelete="cascade")
    motivo = fields.Char(string="Motivo")
    bucket_key = fields.Char(string="Clave interna")
    cantidad = fields.Integer(string="Cantidad")


class SyncSyscomHealthRateLimitLine(models.TransientModel):
    _name = "sync.syscom.health.rate.limit.line"
    _description = "Salud de sincronización — línea de rate limit"
    _order = "pausado desc, ruta_label"

    wizard_id = fields.Many2one("sync.syscom.health.wizard", required=True, ondelete="cascade")
    ruta = fields.Char(string="Clave de ruta")
    ruta_label = fields.Char(string="Ruta")
    pausado = fields.Boolean(string="Pausado ahora")
    pausa_hasta = fields.Datetime(string="Pausa hasta")
    aplazamientos = fields.Integer(string="Aplazamientos consecutivos")
