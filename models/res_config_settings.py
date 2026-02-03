from odoo import _, fields, models
from odoo.exceptions import UserError

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
        string="Descuento % sobre precio especial para costo",
        default=4.0,
        config_parameter="sync_syscom.cost_discount_pct",
        help="Porcentaje de descuento aplicado al precio especial para calcular el costo (standard_price).",
    )

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
