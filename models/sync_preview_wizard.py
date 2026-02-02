from odoo import fields, models, _
from odoo.exceptions import UserError


class SyncSyscomPreviewWizard(models.TransientModel):
    _name = "sync.syscom.preview.wizard"
    _description = "Previsualización de productos a sincronizar"

    line_ids = fields.One2many("sync.syscom.preview.wizard.line", "wizard_id", string="Productos")
    total_candidates = fields.Integer(string="Total candidatos", compute="_compute_totals")
    total_selected = fields.Integer(string="Total seleccionados", compute="_compute_totals")

    def _compute_totals(self):
        for wiz in self:
            wiz.total_candidates = len(wiz.line_ids)
            wiz.total_selected = len(wiz.line_ids.filtered("selected"))

    @staticmethod
    def _gather_candidates(env):
        """
        Devuelve recordset de productos staging según prioridad:
        1) modelos selected=True
        2) marcas selected=True
        3) categorías selected=True
        Si no hay nada marcado, levanta UserError.
        """
        Product = env["sync.syscom.product"]
        Brand = env["sync.syscom.brand"]
        Category = env["sync.syscom.category"]

        # prioridad 1: modelos marcados
        products = Product.search([("selected", "=", True)])
        if products:
            return products

        # prioridad 2: marcas marcadas
        brands = Brand.search([("selected", "=", True)])
        if brands:
            return Product.search([("brand_id", "in", brands.ids)])

        # prioridad 3: categorías marcadas
        categories = Category.search([("selected", "=", True)])
        if categories:
            return Product.search([("category_ids", "in", categories.ids)])

        raise UserError(_("Marca al menos un modelo, marca o categoría para sincronizar."))

    @classmethod
    def create_from_selection(cls):
        env = cls._get_env()
        products = cls._gather_candidates(env)
        wiz = env["sync.syscom.preview.wizard"].create({})
        lines = []
        for prod in products:
            lines.append({
                "wizard_id": wiz.id,
                "product_id": prod.id,
                "selected": True,
                "name": prod.name,
                "model": prod.model,
                "brand_name": prod.brand_id.name,
            })
        env["sync.syscom.preview.wizard.line"].create(lines)
        return wiz

    def action_reload_candidates(self):
        self.ensure_one()
        self.line_ids.unlink()
        products = self._gather_candidates(self.env)
        lines = []
        for prod in products:
            lines.append({
                "wizard_id": self.id,
                "product_id": prod.id,
                "selected": True,
                "name": prod.name,
                "model": prod.model,
                "brand_name": prod.brand_id.name,
            })
        self.env["sync.syscom.preview.wizard.line"].create(lines)
        return {
            "type": "ir.actions.act_window",
            "res_model": "sync.syscom.preview.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_sync_confirm(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_("No hay productos para sincronizar."))
        products = self.line_ids.mapped("product_id")
        products.action_publish_selected()
        return {"type": "ir.actions.act_window_close"}


class SyncSyscomPreviewWizardLine(models.TransientModel):
    _name = "sync.syscom.preview.wizard.line"
    _description = "Línea de previsualización de sync"

    wizard_id = fields.Many2one("sync.syscom.preview.wizard", required=True, ondelete="cascade")
    selected = fields.Boolean(string="Sincronizar", default=True)
    product_id = fields.Many2one("sync.syscom.product", string="Producto", required=True)
    name = fields.Char(string="Nombre")
    model = fields.Char(string="Modelo")
    brand_name = fields.Char(string="Marca")
    price_list = fields.Float(string="Precio lista (USD)", related="product_id.price_list", readonly=True)
    price_list_mxn = fields.Float(string="Precio lista (MXN)", related="product_id.price_list_mxn", readonly=True)
