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
        default="https://api.syscom.mx",
        config_parameter="sync_syscom.syscom_base_url",
    )
    syscom_timeout = fields.Integer(
        string="Timeout (s)",
        default=30,
        config_parameter="sync_syscom.syscom_timeout",
    )

    def action_syscom_test_connection(self):
        self.ensure_one()
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("Debe configurar el Token SYSCOM antes de probar la conexión."))

        base_url = params.get_param("sync_syscom.syscom_base_url") or "https://api.syscom.mx"
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
