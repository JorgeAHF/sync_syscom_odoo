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

from odoo import _, models
from odoo.tools import format_datetime

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
