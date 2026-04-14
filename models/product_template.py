from html import escape

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
    brand_id = fields.Many2one(
        "sync.syscom.brand",
        string="Marca",
        help="Marca SYSCOM asociada al producto.",
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
        help="Porcentaje de descuento aplicado sobre precio con descuento SYSCOM para calcular el costo.",
    )
    syscom_warranty = fields.Char(
        string="Garantía",
        help="Garantía devuelta por SYSCOM.",
    )
    syscom_height_cm = fields.Float(
        string="Alto SYSCOM (cm)",
    )
    syscom_length_cm = fields.Float(
        string="Largo SYSCOM (cm)",
    )
    syscom_width_cm = fields.Float(
        string="Ancho SYSCOM (cm)",
    )
    syscom_features_json = fields.Json(
        string="Características SYSCOM",
    )

    def _has_syscom_vendor(self):
        """Return True if the template has at least one vendor marked as SYSCOM."""
        self.ensure_one()
        for seller in self.seller_ids:
            partner = seller.partner_id or seller.name
            if partner and getattr(partner, "syscom_is_vendor", False):
                return True
        return False

    def _get_ecommerce_description_field_name(self):
        self.ensure_one()
        candidate_names = [
            "description_ecommerce",
            "description_ecommerce_html",
            "sale_ecommerce_description",
            "description_sale_ecommerce",
        ]
        for name in candidate_names:
            if name in self._fields:
                return name

        for name, info in self.fields_get().items():
            if name == "website_description":
                continue
            label = (info.get("string") or "").strip().lower()
            if (
                "comercio electrónico" in label
                or "comercio electronico" in label
                or "ecommerce" in label
                or "electronic commerce" in label
            ):
                return name
        return False

    def _set_syscom_ecommerce_description(self, feature_lines):
        self.ensure_one()
        field_name = self._get_ecommerce_description_field_name()
        if not field_name:
            return False

        field = self._fields[field_name]
        lines = [str(line).strip() for line in (feature_lines or []) if str(line).strip()]
        if not lines:
            value = False
        elif field.type == "html":
            value = "<ul>%s</ul>" % "".join("<li>%s</li>" % escape(line) for line in lines)
        else:
            value = "\n".join("- %s" % line for line in lines)
        self.sudo().write({field_name: value})
        return True
