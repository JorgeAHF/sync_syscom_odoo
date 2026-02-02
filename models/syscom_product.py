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
    price_list = fields.Float(string="Precio lista (USD)")
    price_special = fields.Float(string="Precio especial (USD)")
    price_discounts = fields.Float(string="Precio con descuentos (USD)")
    price_list_mxn = fields.Float(string="Precio lista (MXN)")
    price_special_mxn = fields.Float(string="Precio especial (MXN)")
    price_discounts_mxn = fields.Float(string="Precio con descuentos (MXN)")
    exchange_rate = fields.Float(string="Tipo de cambio aplicado")
    exchange_rate_date = fields.Date(string="Fecha tipo de cambio")
    currency = fields.Char(string="Moneda origen", default="USD")
    total_existencia = fields.Integer(string="Existencia total")
    sat_key = fields.Char(string="Clave SAT")
    image_url = fields.Char(string="Imagen portada")
    brand_logo_url = fields.Char(string="Logo de marca")
    link = fields.Char(string="Link")
    existence_json = fields.Json(string="Existencias (JSON)")
    icons_json = fields.Json(string="Iconos (JSON)")
    features_json = fields.Json(string="Características (JSON)")
    images_json = fields.Json(string="Imágenes (JSON)")
    resources_json = fields.Json(string="Recursos (JSON)")
    description = fields.Text(string="Descripción")
    payload = fields.Json(string="Payload SYSCOM")
    synced_at = fields.Datetime(string="Sincronizado en")
    sync_error = fields.Text(string="Último error de sync")

    _syscom_id_unique = models.Constraint(
        "unique(syscom_id)",
        "El ID SYSCOM debe ser único.",
    )

    def action_publish_selected(self):
        """Enriquece productos seleccionados con detalle, convierte MXN y publica en product.template."""
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError("Configura el token en Ajustes antes de publicar productos.")
        base_url = params.get_param("sync_syscom.syscom_base_url") or "https://developers.syscom.mx/api/v1"
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or 30)
        from .syscom_client import SyscomClient
        client = SyscomClient(base_url=base_url, token=token, timeout=timeout)

        selected_products = self.search([("selected", "=", True)])
        created = updated = failed = 0

        # Tipo de cambio (una semana) obtenido una vez por lote
        rate_payload = client.get_exchange_rate() or {}
        try:
            exchange_rate = float(rate_payload.get("una_semana") or rate_payload.get("normal") or 1.0)
        except Exception:
            exchange_rate = 1.0
        exchange_rate_date = fields.Date.context_today(self)

        for product in selected_products:
            try:
                detail = client.get_product_detail(product.syscom_id) or {}

                precios = detail.get("precios") or {}
                price_list = precios.get("precio_lista") or 0.0
                price_special = precios.get("precio_especial") or 0.0
                price_discounts = precios.get("precio_descuentos") or 0.0

                price_list_mxn = price_list * exchange_rate
                price_special_mxn = price_special * exchange_rate
                price_discounts_mxn = price_discounts * exchange_rate

                name = detail.get("titulo") or product.name
                default_code = detail.get("modelo") or product.model
                description = detail.get("descripcion") or product.description or ""
                link = detail.get("link") or product.link
                total_existencia = detail.get("total_existencia") or 0
                sat_key = detail.get("sat_key") or detail.get("sat") or ""
                image_url = detail.get("img_portada") or product.image_url or ""
                brand_logo_url = detail.get("marca_logo") or ""
                existence_json = detail.get("existencia") or {}
                icons_json = detail.get("iconos") or {}
                features_json = detail.get("características") or detail.get("caracteristicas") or []
                images_json = detail.get("imágenes") or detail.get("imagenes") or []
                resources_json = detail.get("recursos") or []

                product_vals = {
                    "name": name,
                    "model": default_code,
                    "price_list": price_list,
                    "price_special": price_special,
                    "price_discounts": price_discounts,
                    "price_list_mxn": price_list_mxn,
                    "price_special_mxn": price_special_mxn,
                    "price_discounts_mxn": price_discounts_mxn,
                    "exchange_rate": exchange_rate,
                    "exchange_rate_date": exchange_rate_date,
                    "total_existencia": total_existencia,
                    "sat_key": sat_key,
                    "image_url": image_url,
                    "brand_logo_url": brand_logo_url,
                    "link": link,
                    "existence_json": existence_json,
                    "icons_json": icons_json,
                    "features_json": features_json,
                    "images_json": images_json,
                    "resources_json": resources_json,
                    "description": description,
                    "payload": detail,
                    "synced_at": fields.Datetime.now(),
                    "sync_error": False,
                }

                # Categorías del detalle
                cat_ids = []
                for cat in detail.get("categorías") or detail.get("categorias") or []:
                    cat_syscom_id = str(cat.get("id") or "").strip()
                    if not cat_syscom_id:
                        continue
                    cat_rec = self.env["sync.syscom.category"].search(
                        [("syscom_id", "=", cat_syscom_id)],
                        limit=1,
                    )
                    if cat_rec:
                        cat_ids.append(cat_rec.id)
                if cat_ids:
                    product_vals["category_ids"] = [(6, 0, cat_ids)]

                product.write(product_vals)

                # Crear/actualizar plantilla de producto Odoo
                template = self.env["product.template"].search([("default_code", "=", default_code)], limit=1)
                template_vals = {
                    "name": name,
                    "default_code": default_code,
                    "list_price": price_list_mxn,
                    "description_sale": description + (f"\\nLink: {link}" if link else ""),
                }
                if template:
                    template.write(template_vals)
                    updated += 1
                else:
                    template = self.env["product.template"].create(template_vals)
                    created += 1

            except Exception as exc:
                failed += 1
                product.write({
                    "sync_error": str(exc),
                    "synced_at": fields.Datetime.now(),
                })
                continue

        self.env["sync.syscom.log"].create({
            "name": "Publicación de productos SYSCOM",
            "kind": "info",
            "message": "Productos publicados: creados %(c)s, actualizados %(u)s, fallidos %(f)s" % {"c": created, "u": updated, "f": failed},
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Sync SYSCOM",
                "message": "Productos publicados. Creados: %(c)s, actualizados: %(u)s, fallidos: %(f)s." % {"c": created, "u": updated, "f": failed},
                "type": "success",
                "sticky": False,
            },
        }
