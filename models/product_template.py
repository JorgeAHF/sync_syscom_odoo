from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    syscom_is_product = fields.Boolean(
        string="Producto SYSCOM",
        help="Indica que este producto se integra con SYSCOM (dropship).",
    )
    syscom_product_id = fields.Char(
        string="SYSCOM producto_id",
        help="ID numérico de SYSCOM usado para consultar detalle/stock/precios (ej. 235038).",
        index=True,
    )
    syscom_stock_new = fields.Integer(
        string="Stock SYSCOM (nuevo)",
        help="Existencia 'nuevo' devuelta por SYSCOM (solo informativo para ecommerce).",
    )
    syscom_stock_synced_at = fields.Datetime(
        string="Stock SYSCOM actualizado",
    )
    syscom_api_ok = fields.Boolean(
        string="SYSCOM API OK",
        help="Último estado de validación/refresh contra SYSCOM para este producto.",
        default=True,
    )
    syscom_uom_sat = fields.Char(
        string="Unidad SAT (SYSCOM)",
        help="Clave SAT de unidad devuelta por SYSCOM (ej. H87).",
    )
    syscom_cost_margin_pct = fields.Float(
        string="Margen costo SYSCOM (%)",
        help="Porcentaje de descuento aplicado sobre precio especial SYSCOM para calcular el costo.",
    )

    def _has_syscom_vendor(self):
        """Return True if the template has at least one vendor marked as SYSCOM."""
        self.ensure_one()
        for seller in self.seller_ids:
            partner = seller.partner_id or seller.name
            if partner and getattr(partner, "syscom_is_vendor", False):
                return True
        return False
