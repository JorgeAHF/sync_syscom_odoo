from odoo import fields, models
from odoo.exceptions import UserError


class SyscomProduct(models.Model):
    _name = "sync.syscom.product"
    _description = "Producto SYSCOM (staging)"
    _order = "model"

    name = fields.Char(string="Nombre", required=True)
    syscom_id = fields.Char(string="ID SYSCOM", required=True, index=True)
    model = fields.Char(string="Modelo", index=True)
    active = fields.Boolean(string="Activo", default=True)
    selected = fields.Boolean(string="Sel", default=False)
    brand_id = fields.Many2one("sync.syscom.brand", string="Marca")
    category_ids = fields.Many2many(
        "sync.syscom.category",
        "sync_syscom_category_product_rel",
        "product_id",
        "category_id",
        string="Categorías",
    )
    price_list = fields.Float(string="Precio lista")
    price_special = fields.Float(string="Precio especial")
    price_discounts = fields.Float(string="Precio con descuentos")
    currency = fields.Char(string="Moneda", default="MXN")
    image_url = fields.Char(string="Imagen portada")
    link = fields.Char(string="Link")
    description = fields.Text(string="Descripción")
    payload = fields.Json(string="Payload SYSCOM")

    _syscom_id_unique = models.Constraint(
        "unique(syscom_id)",
        "El ID SYSCOM debe ser único.",
    )

    def action_publish_selected(self):
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError("Configura el token en Ajustes antes de publicar productos.")
        base_url = params.get_param("sync_syscom.syscom_base_url") or "https://developers.syscom.mx/api/v1"
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or 30)
        from .syscom_client import SyscomClient
        client = SyscomClient(base_url=base_url, token=token, timeout=timeout)

        selected_products = self.search([("selected", "=", True)])
        created = updated = 0
        for product in selected_products:
            # Obtener detalle
            detail = client.get_product_detail(product.syscom_id) or {}
            name = detail.get("titulo") or product.name
            default_code = detail.get("modelo") or product.model
            list_price = (detail.get("precios") or {}).get("precio_lista") or product.price_list or 0.0
            description = detail.get("descripcion") or product.description or ""
            link = detail.get("link") or product.link

            template = self.env["product.template"].search([("default_code", "=", default_code)], limit=1)
            vals = {
                "name": name,
                "default_code": default_code,
                "list_price": list_price,
                "description_sale": description + (f"\\nLink: {link}" if link else ""),
            }
            if template:
                template.write(vals)
                updated += 1
            else:
                template = self.env["product.template"].create(vals)
                created += 1

        self.env["sync.syscom.log"].create({
            "name": "Publicación de productos SYSCOM",
            "kind": "info",
            "message": "Productos publicados: creados %(c)s, actualizados %(u)s" % {"c": created, "u": updated},
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Sync SYSCOM",
                "message": "Productos publicados. Creados: %(c)s, actualizados: %(u)s." % {"c": created, "u": updated},
                "type": "success",
                "sticky": False,
            },
        }
