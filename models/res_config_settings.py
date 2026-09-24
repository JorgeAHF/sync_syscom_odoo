from odoo import _, fields, models
from odoo.exceptions import UserError

from .constants import (
    DEFAULT_STOCK_REFRESH_BATCH_MINUTES,
    DEFAULT_STOCK_REFRESH_BATCH_MINUTES_MIN,
)
from .syscom_client import SyscomClient


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    syscom_api_token = fields.Char(
        string="Token SYSCOM",
        config_parameter="sync_syscom.syscom_api_token",
    )
    syscom_base_url = fields.Char(
        string="Base URL API",
        default="https://developers.syscom.mx/api/v1",
        config_parameter="sync_syscom.syscom_base_url",
    )
    syscom_timeout = fields.Integer(
        string="Timeout (s)",
        default=30,
        config_parameter="sync_syscom.syscom_timeout",
    )
    syscom_min_stock = fields.Integer(
        string="Stock mínimo de SYSCOM",
        default=1,
        required=True,
        config_parameter="sync_syscom.min_stock",
        help="Cantidad mínima de existencia en SYSCOM para permitir dar de alta/publicar un producto.",
    )
    syscom_pricelist_list = fields.Many2one(
        "product.pricelist",
        string="Pricelist lista SYSCOM",
        config_parameter="sync_syscom.pricelist_list_id",
        default=lambda self: self.env.ref("sync_syscom.pricelist_syscom_list", raise_if_not_found=False),
        help="Lista de precios donde se guardará el precio lista de SYSCOM (MXN).",
    )
    syscom_pricelist_special = fields.Many2one(
        "product.pricelist",
        string="Pricelist especial SYSCOM",
        config_parameter="sync_syscom.pricelist_special_id",
        default=lambda self: self.env.ref("sync_syscom.pricelist_syscom_special", raise_if_not_found=False),
        help="Lista de precios donde se guardará el precio especial de SYSCOM (MXN).",
    )
    syscom_pricelist_discount = fields.Many2one(
        "product.pricelist",
        string="Pricelist descuento SYSCOM",
        config_parameter="sync_syscom.pricelist_discount_id",
        default=lambda self: self.env.ref("sync_syscom.pricelist_syscom_discount", raise_if_not_found=False),
        help="Lista de precios donde se guardará el precio con descuentos de SYSCOM (MXN).",
    )
    syscom_price_currency = fields.Selection(
        [("usd", "USD (convertir a MXN)"), ("mxn", "MXN (no convertir)")],
        string="Moneda origen de precios SYSCOM",
        default="usd",
        config_parameter="sync_syscom.price_currency",
        help="Controla si los precios traídos se convierten con el tipo de cambio o ya vienen en MXN.",
    )
    syscom_cost_discount_pct = fields.Float(
        string="Descuento % sobre precio con descuento para costo",
        default=4.0,
        config_parameter="sync_syscom.cost_discount_pct",
        help="Porcentaje de descuento aplicado al precio con descuento de SYSCOM para calcular el costo (standard_price).",
    )

    syscom_stock_refresh_enabled = fields.Boolean(
        string="Activar refresco de stock/precios SYSCOM",
        default=True,
        config_parameter="sync_syscom.stock_refresh_enabled",
        help="Si está activo, se refresca stock (nuevo), precios y costo de productos SYSCOM publicados con una frecuencia configurable.",
    )
    syscom_stock_refresh_cycle_days = fields.Integer(
        string="Días de espera entre ciclos",
        default=7,
        config_parameter="sync_syscom.stock_refresh_cycle_days",
        help="Al terminar la vuelta completa al catálogo, el refresco descansa estos días "
             "antes de empezar la siguiente. No frena entre lotes: frena entre ciclos.",
    )
    syscom_stock_refresh_batch_minutes = fields.Integer(
        string="Minutos entre lotes",
        # Campo normal, SIN `default=`, `compute=` ni `inverse=`. Se lee en `get_values`
        # y se escribe en `set_values`, que es el par que usa el botón Guardar.
        #
        # Antes llevaba `compute` + `inverse`, y eso lo hacía comportarse distinto del
        # resto de la página: los `inverse` se disparan al guardarse el REGISTRO, y el
        # cliente web guarda el registro antes de invocar cualquier botón de tipo objeto.
        # O sea que pulsar "Probar conexión" con los dos campos cambiados aplicaba los
        # minutos y NO los días, dejando media configuración puesta. Ahora los dos
        # esperan al Guardar.
        help="Cada cuántos minutos se procesa un lote. Es el intervalo real del cron "
             "'Sync Syscom: existencias diarias seleccionados'.",
    )
    syscom_publish_include_children = fields.Boolean(
        string="Publicar categorías con subcategorías",
        default=True,
        config_parameter="sync_syscom.publish_include_subcategories",
        help="Si está activo, las acciones de publicar por categoría incluyen automáticamente sus subcategorías.",
    )

    def _cron_refresco_stock(self):
        return self.env.ref("sync_syscom.cron_sync_syscom_stock_daily",
                            raise_if_not_found=False)

    def _minutos_entre_lotes_del_cron(self):
        """El intervalo real del cron 58, en minutos.

        El intervalo vive en `ir.cron` y ese es el único sitio que manda de verdad.
        Guardar una copia en un `ir.config_parameter` daría dos fuentes que se
        desincronizan en cuanto alguien edite el cron a mano.
        """
        cron = self._cron_refresco_stock()
        if not cron:
            return DEFAULT_STOCK_REFRESH_BATCH_MINUTES
        cron = cron.sudo()
        factor = {"minutes": 1, "hours": 60, "days": 1440,
                  "weeks": 10080, "months": 43200}.get(cron.interval_type, 0)
        return ((cron.interval_number or 0) * factor
                or DEFAULT_STOCK_REFRESH_BATCH_MINUTES)

    def get_values(self):
        valores = super().get_values()
        valores["syscom_stock_refresh_batch_minutes"] = self._minutos_entre_lotes_del_cron()
        return valores

    def set_values(self):
        super().set_values()
        cron = self._cron_refresco_stock()
        if not cron:
            return
        # Suelo duro: por debajo de esto un lote de 200 no cabe entre corridas y el cron
        # se solaparía consigo mismo contra el rate limit de SYSCOM.
        minutos = max(int(self.syscom_stock_refresh_batch_minutes or 0),
                      DEFAULT_STOCK_REFRESH_BATCH_MINUTES_MIN)
        cron = cron.sudo()
        # Solo se escribe si cambia: si no, cada Guardar de Ajustes movería el
        # `write_date` del cron sin motivo.
        if (cron.interval_number, cron.interval_type) != (minutos, "minutes"):
            cron.write({"interval_number": minutos, "interval_type": "minutes"})

    def action_syscom_test_connection(self):
        self.ensure_one()
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("Debe configurar el Token SYSCOM antes de probar la conexión."))

        base_url = params.get_param("sync_syscom.syscom_base_url") or "https://developers.syscom.mx/api/v1"
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or 30)
        client = SyscomClient(base_url=base_url, token=token, timeout=timeout)
        ok, message = client.ping()
        if not ok:
            raise UserError(message)

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Conexión SYSCOM"),
                "message": message,
                "type": "success",
                "sticky": False,
            },
        }
