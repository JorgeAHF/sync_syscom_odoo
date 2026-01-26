from odoo import _, fields, models
from odoo.exceptions import UserError


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
        if not (self.syscom_api_token or "").strip():
            raise UserError(_("Debe configurar el Token SYSCOM antes de probar la conexión."))

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Conexión SYSCOM"),
                "message": _("Se invocó la prueba de conexión."),
                "type": "success",
                "sticky": False,
            },
        }
