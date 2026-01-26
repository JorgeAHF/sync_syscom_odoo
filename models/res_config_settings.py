from odoo import _, fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    syscom_api_token = fields.Char(string="Token SYSCOM")
    syscom_base_url = fields.Char(
        string="Base URL API",
        default="https://api.syscom.mx",
    )
    syscom_timeout = fields.Integer(
        string="Timeout (s)",
        default=30,
    )

    def get_values(self):
        res = super().get_values()
        params = self.env["ir.config_parameter"].sudo()
        res.update(
            syscom_api_token=params.get_param("sync_syscom.syscom_api_token", default=""),
            syscom_base_url=params.get_param(
                "sync_syscom.syscom_base_url",
                default="https://api.syscom.mx",
            ),
            syscom_timeout=int(params.get_param("sync_syscom.syscom_timeout", default="30")),
        )
        return res

    def set_values(self):
        super().set_values()
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("sync_syscom.syscom_api_token", self.syscom_api_token or "")
        params.set_param(
            "sync_syscom.syscom_base_url",
            (self.syscom_base_url or "https://api.syscom.mx").strip(),
        )
        params.set_param("sync_syscom.syscom_timeout", self.syscom_timeout or 30)

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
