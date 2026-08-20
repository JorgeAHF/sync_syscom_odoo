"""Formateo compartido de los mensajes que devuelven los botones asíncronos.

El módulo encola trabajo y devuelve el control de inmediato, así que el mensaje del
clic es lo único que el usuario llega a ver.  Este mixin existe para que Categorías,
Marcas y Modelos digan las mismas cosas de la misma forma: un ID real en vez del
``display_name`` que el propio código acaba de escribir, y una ruta de menú donde
seguir el trabajo.

Va como ``AbstractModel`` y no como funciones de módulo por una razón concreta: ``_()``
deduce el idioma inspeccionando el frame de quien llama y busca un local llamado
``self`` (``odoo/tools/translate.py``, ``_get_lang``).  Una función suelta que recibe
``env`` no lo tiene, así que cae al último recurso y registra un WARNING con traza
completa en cada llamada.  Desde un clic se salvaría por ``odoo.http.request``, pero
desde un cron o ``odoo shell`` no.  Como método, ``self.env.lang`` resuelve siempre.
"""

from datetime import timedelta

from odoo import _, fields, models
from odoo.tools import format_datetime

from .constants import (
    DEFAULT_RATE_LIMIT_BACKOFF,
    MAX_RATE_LIMIT_BACKOFF,
    MAX_RATE_LIMIT_POSTPONES,
)


def segundos_de_espera_efectivos(env, retry_after=None, aplazamientos=0):
    """Cuánto esperar tras un 429, en segundos.

    Va como función suelta y no como método del mixin, al contrario que todo lo
    demás de este archivo: no llama a ``_()``, así que la trampa de ``_get_lang``
    descrita arriba no aplica. Solo lee un parámetro y hace aritmética.

    Si la respuesta trajo ``Retry-After``, manda esa cifra: es la única fuente
    que sabe de verdad cuándo se abre la ventana. Si no vino, se usa el respaldo
    configurado duplicándolo por cada aplazamiento consecutivo, con tope.
    """
    if retry_after is not None and retry_after > 0:
        return min(int(retry_after), MAX_RATE_LIMIT_BACKOFF)

    params = env["ir.config_parameter"].sudo()
    try:
        base = int(params.get_param("sync_syscom.rate_limit_backoff_seconds") or DEFAULT_RATE_LIMIT_BACKOFF)
    except (TypeError, ValueError):
        base = DEFAULT_RATE_LIMIT_BACKOFF
    base = max(base, 1)
    return min(base * (2 ** max(int(aplazamientos or 0), 0)), MAX_RATE_LIMIT_BACKOFF)


# ── Pausa de ruta tras un 429 ─────────────────────────────────────────────────
# El rate limit es una propiedad de la API, no de un job concreto, asi que se pausa
# la RUTA entera y no el registro. Va en ir.config_parameter y no en campos de los
# modelos para no obligar a un -u; el dato es transitorio y no merece una columna.
#
# Ojo con por que hace falta la pausa ademas del _trigger: los crons 67 y 69 corren
# CADA MINUTO. Reprogramar con _trigger(at=...) solo anade un disparo extra, no
# retrasa el horario normal, asi que sin esta pausa el cron volveria a la API 60 s
# despues del 429 igualmente.

def _clave_pausa(ruta):
    return "sync_syscom.rate_limit_pausa_hasta.%s" % ruta


def _clave_aplazamientos(ruta):
    return "sync_syscom.rate_limit_aplazamientos.%s" % ruta


def segundos_de_pausa_restantes(env, ruta):
    """Segundos que faltan para que la ruta pueda volver a salir a la API. 0 si ya puede."""
    valor = env["ir.config_parameter"].sudo().get_param(_clave_pausa(ruta))
    if not valor:
        return 0
    try:
        hasta = fields.Datetime.to_datetime(valor)
    except (TypeError, ValueError):
        return 0
    if not hasta:
        return 0
    restan = (hasta - fields.Datetime.now()).total_seconds()
    return int(restan) if restan > 0 else 0


def contar_aplazamientos(env, ruta):
    valor = env["ir.config_parameter"].sudo().get_param(_clave_aplazamientos(ruta))
    try:
        return max(int(valor or 0), 0)
    except (TypeError, ValueError):
        return 0


def reiniciar_aplazamientos(env, ruta):
    """Tras una pasada buena, la ruta vuelve a empezar de cero."""
    params = env["ir.config_parameter"].sudo()
    if params.get_param(_clave_aplazamientos(ruta)):
        params.set_param(_clave_aplazamientos(ruta), "0")
    if params.get_param(_clave_pausa(ruta)):
        params.set_param(_clave_pausa(ruta), "")


def aplazar_por_rate_limit(env, ruta, exc, etiqueta, cron_xmlid=None):
    """Pausa la ruta tras un 429 en vez de dormir el hilo del cron.

    Dormir seria lo peor que se puede hacer aqui: solo hay 2 hilos de cron
    (``max_cron_threads``), el hilo dormido mantiene el bloqueo de su fila de
    ir_cron y una transaccion abierta --que es justo lo que fabrica los
    SerializationFailure entre los crons 60 y 66-- y un Retry-After puede pedir
    media hora.

    Devuelve ``(aplazado, segundos)``. ``aplazado=False`` significa que se agoto el
    tope de aplazamientos consecutivos y quien llama debe dar el trabajo por perdido.
    """
    previos = contar_aplazamientos(env, ruta)
    if previos >= MAX_RATE_LIMIT_POSTPONES:
        return False, 0

    espera = segundos_de_espera_efectivos(env, getattr(exc, "retry_after", None), previos)
    ahora = fields.Datetime.now()
    params = env["ir.config_parameter"].sudo()
    params.set_param(_clave_pausa(ruta), fields.Datetime.to_string(ahora + timedelta(seconds=espera)))
    params.set_param(_clave_aplazamientos(ruta), str(previos + 1))

    # El _trigger es para cuando el cron tenga un intervalo largo: asegura que
    # vuelva justo al abrirse la ventana en vez de esperar a su siguiente horario.
    if cron_xmlid:
        cron = env.ref(cron_xmlid, raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger(at=ahora + timedelta(seconds=espera + 5))

    tiene_cabecera = getattr(exc, "retry_after", None) is not None
    env["sync.syscom.log"].sudo().create({
        "name": "Rate limit de SYSCOM (ruta pausada)",
        "kind": "warn",
        "message": (
            "%(etiqueta)s se detuvo por HTTP 429. La ruta queda en pausa %(espera)s s "
            "(aplazamiento %(n)s de %(max)s). Origen de la espera: %(origen)s. "
            "No se pierde el avance: se retoma donde quedo. Error: %(err)s"
            % {
                "etiqueta": etiqueta,
                "espera": espera,
                "n": previos + 1,
                "max": MAX_RATE_LIMIT_POSTPONES,
                "origen": "cabecera Retry-After" if tiene_cabecera else "respaldo configurado (SYSCOM no mando Retry-After)",
                "err": exc,
            }
        ),
    })
    return True, espera

# Rutas de menú que se citan en los mensajes, para que el usuario sepa dónde mirar.
MENU_TRABAJOS_SYNC = "SyncSyscom › Sincronizar › Trabajos sync catálogo"
MENU_TRABAJOS_CATEGORIAS = "SyncSyscom › Sincronizar › Trabajos categorías"
MENU_TRABAJOS_PUBLICACION = "SyncSyscom › Sincronizar › Ver trabajos publicación"
MENU_TRABAJOS_COSTOS = "SyncSyscom › Sincronizar › Trabajos costos"
MENU_TRABAJOS_DATOS = "SyncSyscom › Sincronizar › Trabajos datos extendidos"
MENU_TRABAJOS_DROPSHIP = "SyncSyscom › Sincronizar › Trabajos dropshipping"
MENU_LOGS = "SyncSyscom › Logs"

# Nombre del cron que publica lo que estos botones dejan en cola. Se cita en los
# mensajes de los botones que NO crean job: ahí no hay nada que mirar salvo la lista,
# así que el cron responsable es el único dato que explica por qué "no pasa nada".
CRON_PUBLICAR = "publicar seleccionados"


class SyncSyscomJobFeedback(models.AbstractModel):
    _name = "sync.syscom.job.feedback"
    _description = "Formateo de mensajes de trabajos SYSCOM"

    def _etiqueta_seleccion(self, registro, nombre_campo):
        """Etiqueta que muestra la interfaz para un campo de selección.

        Usa ``_description_selection``, que aplica la traducción del idioma del
        contexto, en vez de ``.selection``, que devuelve el valor fuente del código.
        """
        opciones = dict(registro._fields[nombre_campo]._description_selection(self.env))
        valor = registro[nombre_campo]
        return opciones.get(valor, valor)

    def _descripcion_job_existente(self, job, con_etapa=False):
        """Describe un job que ya estaba en curso: id, estado, etapa y cuándo se creó."""
        partes = [
            "#%s" % job.id,
            _("estado %s") % self._etiqueta_seleccion(job, "state"),
        ]
        if con_etapa:
            partes.append(_("etapa %s") % self._etiqueta_seleccion(job, "stage"))
        if job.create_date:
            partes.append(_("creado el %s") % format_datetime(self.env, job.create_date))
        return ", ".join(partes)
