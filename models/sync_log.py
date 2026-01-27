from odoo import fields, models


class SyncSyscomLog(models.Model):
    _name = "sync.syscom.log"
    _description = "Log de sincronización SYSCOM"
    _order = "create_date desc"

    name = fields.Char(string="Nombre", required=True)
    kind = fields.Selection(
        [
            ("info", "Info"),
            ("warn", "Advertencia"),
            ("error", "Error"),
        ],
        string="Tipo",
        default="info",
        required=True,
    )
    message = fields.Text(string="Mensaje")
