from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError

from .syscom_client import SyscomClient, SyscomClientError


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    syscom_api_token = fields.Char(
        string="Token SYSCOM",
        config_parameter="sync_syscom.syscom_api_token",
    )
    syscom_base_url = fields.Char(
        string="Base URL API",
        config_parameter="sync_syscom.syscom_base_url",
        default="https://api.syscom.mx",
    )
    syscom_timeout = fields.Integer(
        string="Timeout (s)",
        config_parameter="sync_syscom.syscom_timeout",
        default=30,
    )

    def action_syscom_test_connection(self):
        self.ensure_one()
        if not self.env.user.has_group("sync_syscom.group_sync_syscom_manager"):
            raise AccessError(_("No tiene permisos para probar la conexión SYSCOM."))

        token = (self.syscom_api_token or "").strip()
        if not token:
            raise UserError(_("Debe configurar el Token SYSCOM antes de probar la conexión."))

        base_url = (self.syscom_base_url or "").strip() or "https://api.syscom.mx"
        timeout = self.syscom_timeout or 30

        client = SyscomClient(base_url=base_url, token=token, timeout=timeout)
        try:
            client.test_connection()
        except SyscomClientError as exc:
            raise UserError(_("Error al conectar con SYSCOM: %s") % exc) from exc

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Conexión exitosa"),
                "message": _("Credenciales válidas y SYSCOM respondió correctamente."),
                "type": "success",
                "sticky": False,
            },
        }
