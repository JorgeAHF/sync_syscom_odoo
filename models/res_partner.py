from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    syscom_is_vendor = fields.Boolean(
        string="Proveedor SYSCOM",
        help="Identifica el partner proveedor SYSCOM para flujos de dropshipping y validaciones.",
    )

