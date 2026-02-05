from odoo import fields, models
from odoo.exceptions import UserError
from urllib.parse import urlparse, urlunparse


class SyscomProduct(models.Model):
    _name = "sync.syscom.product"
    _description = "Producto SYSCOM (staging)"
    _order = "model"

    @staticmethod
    def _to_float(value):
        """Coerce API values to float; fallback to 0.0 on any error."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _get_deepest_category(self, cat_ids):
        """Return the category (sync.syscom.category) with highest level; fallback first."""
        if not cat_ids:
            return None
        categories = self.env["sync.syscom.category"].browse(cat_ids)
        categories = categories.sorted(key=lambda c: c.level or 0, reverse=True)
        return categories[0] if categories else None

    def _ensure_product_category(self, syscom_category):
        """Create/link a product.category matching the SYSCOM category tree."""
        if not syscom_category:
            return None
        syscom_category = syscom_category.sudo()
        if syscom_category.product_category_id:
            return syscom_category.product_category_id

        parent_product_category = None
        if syscom_category.parent_id:
            parent_product_category = self._ensure_product_category(syscom_category.parent_id)

        ProductCategory = self.env["product.category"].sudo()
        domain = [("name", "=", syscom_category.name)]
        domain.append(("parent_id", "=", parent_product_category.id if parent_product_category else False))
        product_category = ProductCategory.search(domain, limit=1)
        if not product_category:
            vals = {"name": syscom_category.name}
            if parent_product_category:
                vals["parent_id"] = parent_product_category.id
            product_category = ProductCategory.create(vals)

        syscom_category.write({"product_category_id": product_category.id})
        return product_category

    def _update_template_pricelists_and_cost(self, template, prices_mxn, params):
        """Update pricelists (list, special, discount) and standard_price."""

        def _config_pricelist_id(param_key, fallback_xmlid):
            """Return configured pricelist id, falling back to the module's XMLID if unset.

            This makes pricelist updates work even if the user hasn't opened/saved Settings yet.
            """
            val = params.get_param(param_key)
            try:
                if val:
                    return int(val)
            except Exception:
                pass
            ref = self.env.ref(fallback_xmlid, raise_if_not_found=False)
            return int(ref.id) if ref else 0

        pricelist_list_id = _config_pricelist_id(
            "sync_syscom.pricelist_list_id",
            "sync_syscom.pricelist_syscom_list",
        )
        pricelist_special_id = _config_pricelist_id(
            "sync_syscom.pricelist_special_id",
            "sync_syscom.pricelist_syscom_special",
        )
        pricelist_discount_id = _config_pricelist_id(
            "sync_syscom.pricelist_discount_id",
            "sync_syscom.pricelist_syscom_discount",
        )
        cost_pct = float(params.get_param("sync_syscom.cost_discount_pct") or 4.0)
        PricelistItem = self.env["product.pricelist.item"].sudo()

        def upsert(pricelist_id, price):
            if not pricelist_id:
                return
            item = PricelistItem.search([
                ("pricelist_id", "=", pricelist_id),
                ("product_tmpl_id", "=", template.id),
                ("applied_on", "=", "1_product"),
            ], limit=1)
            vals_item = {
                "pricelist_id": pricelist_id,
                "applied_on": "1_product",
                "product_tmpl_id": template.id,
                "compute_price": "fixed",
                "fixed_price": price,
            }
            if item:
                item.write({"fixed_price": price})
            else:
                PricelistItem.create(vals_item)

        upsert(pricelist_list_id, prices_mxn.get("list_price_mxn", 0.0))
        upsert(pricelist_special_id, prices_mxn.get("special_price_mxn", 0.0))
        upsert(pricelist_discount_id, prices_mxn.get("discount_price_mxn", 0.0))

        # costo (standard_price)
        cost = prices_mxn.get("special_price_mxn", 0.0) * (1 - cost_pct / 100.0)
        vals_cost = {"standard_price": cost}
        if template._fields.get("syscom_cost_margin_pct"):
            vals_cost["syscom_cost_margin_pct"] = cost_pct
        template.sudo().write(vals_cost)

    def _sync_template_unspsc_from_sat(self, template, sat_key, sat_description=None):
        """Set UNSPSC Category (Many2one) on product.template using SYSCOM sat_key.

        If the UNSPSC field/model isn't available in this DB, do nothing (per requirements).
        """
        sat_key = (sat_key or "").strip()
        if not sat_key:
            return

        # Find the UNSPSC M2O field on product.template (it exists only when UNSPSC feature/module is present).
        unspsc_field_name = None
        for fname, field in template._fields.items():
            if field.type == "many2one" and getattr(field, "comodel_name", None) == "product.unspsc.code":
                unspsc_field_name = fname
                break
        if not unspsc_field_name:
            return

        Unspsc = self.env["product.unspsc.code"].sudo()
        if "code" not in Unspsc._fields:
            # Unexpected model shape; safer to do nothing.
            return

        unspsc = Unspsc.search([("code", "=", sat_key)], limit=1)
        if not unspsc:
            vals = {"code": sat_key}
            if sat_description and "name" in Unspsc._fields:
                vals["name"] = sat_description
            unspsc = Unspsc.create(vals)
        elif sat_description and "name" in Unspsc._fields and not (unspsc.name or "").strip():
            # Enrich existing record name if missing.
            unspsc.write({"name": sat_description})

        template.sudo().write({unspsc_field_name: unspsc.id})

    def _sync_template_media_and_resources(self, template, detail):
        """Sync images and resource links from SYSCOM detail into product.template."""
        Image = self.env["product.image"].sudo()
        Attachment = self.env["ir.attachment"].sudo()
        ProductDocument = self.env["product.document"].sudo() if "product.document" in self.env.registry.models else None

        images = detail.get("imágenes") or detail.get("imagenes") or []
        resources = detail.get("recursos") or []

        # Imágenes: primera a image_1920, todas a product.image
        if images:
            first_url = images[0]
            if isinstance(first_url, dict):
                first_url = first_url.get("url") or first_url.get("imagen")
            if first_url:
                try:
                    import base64, requests
                    resp = requests.get(first_url, timeout=10)
                    if resp.ok:
                        template.image_1920 = base64.b64encode(resp.content)
                except Exception:
                    pass

        existing_images = Image.search([("product_tmpl_id", "=", template.id), ("name", "like", "SYSCOM %")])
        existing_images.unlink()

        seq = 1
        for img in images:
            url = img
            if isinstance(img, dict):
                url = img.get("url") or img.get("imagen")
            if not url:
                continue
            try:
                import base64, requests
                resp = requests.get(url, timeout=10)
                if not resp.ok:
                    continue
                Image.create({
                    "product_tmpl_id": template.id,
                    "name": f"SYSCOM {seq}",
                    "sequence": seq,
                    "image_1920": base64.b64encode(resp.content),
                })
                seq += 1
            except Exception:
                continue

        # Recursos:
        # Prefer Odoo "Documentos del producto" so links appear on the website product page.
        # Fall back to ir.attachment(url) if product.document is not available in this DB.
        for res in resources:
            if not isinstance(res, dict):
                continue
            # SYSCOM returns {"recurso": "...", "path": "http://...pdf"} in /productos/{id}
            url = (res.get("path") or res.get("url") or "").strip()
            # Browsers may block "insecure downloads" from an https website when the file URL is http.
            # SYSCOM often returns http://ftp*.syscom.mx/...; if https is available, prefer it.
            try:
                parsed = urlparse(url)
                if parsed.scheme == "http" and parsed.netloc.endswith("syscom.mx") and parsed.netloc.startswith("ftp"):
                    url = urlunparse(parsed._replace(scheme="https"))
            except Exception:
                pass
            name = (res.get("recurso") or res.get("nombre") or res.get("titulo") or res.get("name") or "").strip()
            if not url:
                continue
            if ProductDocument:
                doc = ProductDocument.search([
                    ("res_model", "=", "product.template"),
                    ("res_id", "=", template.id),
                    ("type", "=", "url"),
                    ("url", "=", url),
                ], limit=1)
                if doc:
                    update_vals = {}
                    if name and (doc.name or "").strip() != name:
                        update_vals["name"] = name
                    if not doc.shown_on_product_page:
                        update_vals["shown_on_product_page"] = True
                    if not doc.public:
                        update_vals["public"] = True
                    # If multi-website is enabled, pin it to a website when empty so it can be published.
                    if "website_id" in doc._fields and not doc.website_id:
                        website = self.env["website"].sudo().search([], limit=1) if "website" in self.env.registry.models else None
                        if website:
                            update_vals["website_id"] = website.id
                    if update_vals:
                        doc.write(update_vals)
                else:
                    vals_doc = {
                        "name": name or "Recurso SYSCOM",
                        "type": "url",
                        "url": url,
                        "res_model": "product.template",
                        "res_id": template.id,
                        "shown_on_product_page": True,
                        "public": True,
                        "description": "SYSCOM",
                    }
                    if "website_id" in ProductDocument._fields:
                        website = self.env["website"].sudo().search([], limit=1) if "website" in self.env.registry.models else None
                        if website:
                            vals_doc["website_id"] = website.id
                    ProductDocument.create(vals_doc)
            else:
                exists = Attachment.search([
                    ("res_model", "=", "product.template"),
                    ("res_id", "=", template.id),
                    ("type", "=", "url"),
                    ("url", "=", url),
                ], limit=1)
                if exists:
                    continue
                Attachment.create({
                    "name": name or "Recurso SYSCOM",
                    "type": "url",
                    "url": url,
                    "res_model": "product.template",
                    "res_id": template.id,
                })

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


    def _get_client(self):
        """Helper para instanciar SyscomClient con parámetros configurados."""
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError("Configura el token en Ajustes antes de sincronizar.")
        base_url = params.get_param("sync_syscom.syscom_base_url") or "https://developers.syscom.mx/api/v1"
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or 30)
        from .syscom_client import SyscomClient
        return SyscomClient(base_url=base_url, token=token, timeout=timeout)

    def cron_update_exchange_rate(self):
        """Cron semanal: recalcula precios MXN en staging y plantillas publicadas."""
        client = self._get_client()
        rate_payload = client.get_exchange_rate() or {}
        try:
            exchange_rate = float(rate_payload.get("una_semana") or rate_payload.get("normal") or 1.0)
        except Exception:
            exchange_rate = 1.0
        exchange_rate_date = fields.Date.context_today(self)

        # Actualizar staging
        products = self.search([])
        for prod in products:
            if prod.price_list is None:
                continue
            price_list = self._to_float(prod.price_list)
            price_special = self._to_float(prod.price_special)
            price_discounts = self._to_float(prod.price_discounts)
            prod.write({
                "price_list_mxn": price_list * exchange_rate,
                "price_special_mxn": price_special * exchange_rate,
                "price_discounts_mxn": price_discounts * exchange_rate,
                "exchange_rate": exchange_rate,
                "exchange_rate_date": exchange_rate_date,
            })

        # Actualizar plantillas existentes por default_code
        templates = self.env["product.template"].search([])
        updated_templates = 0
        for tmpl in templates:
            if not tmpl.default_code:
                continue
            prod = products.filtered(lambda p: p.model == tmpl.default_code or p.syscom_id == tmpl.default_code)
            if not prod:
                continue
            price_mxn = self._to_float(prod[0].price_list) * exchange_rate
            tmpl.write({"list_price": price_mxn})
            updated_templates += 1

        self.env["sync.syscom.log"].create({
            "name": "Actualización tipo de cambio SYSCOM",
            "kind": "info",
            "message": "Tasa aplicada: %(rate)s. Productos staging: %(p)s. Plantillas actualizadas: %(t)s" % {
                "rate": exchange_rate,
                "p": len(products),
                "t": updated_templates,
            },
        })

    def cron_update_stock_selected(self):
        """Cron diario 1am MX: actualiza existencias de productos seleccionados."""
        client = self._get_client()
        selected = self.search([("selected", "=", True)])
        updated = failed = 0
        params = self.env["ir.config_parameter"].sudo()
        location = self.env.ref("sync_syscom.stock_location_syscom", raise_if_not_found=False)
        quant_model = self.env["stock.quant"]
        rate_payload = client.get_exchange_rate() or {}
        try:
            exchange_rate = float(rate_payload.get("una_semana") or rate_payload.get("normal") or 1.0)
        except Exception:
            exchange_rate = 1.0
        price_currency = params.get_param("sync_syscom.price_currency") or "usd"
        for prod in selected:
            try:
                detail = client.get_product_detail(prod.syscom_id) or {}
                total_existencia = detail.get("total_existencia") or 0
                existence_json = detail.get("existencia") or {}
                sat_key = detail.get("sat_key") or detail.get("sat") or ""
                sat_description = detail.get("sat_description") or ""
                precios = detail.get("precios") or {}
                price_list = self._to_float(precios.get("precio_lista"))
                price_special = self._to_float(precios.get("precio_especial"))
                # SYSCOM returns "precio_descuento" (singular) in /productos/{id}.
                # Keep backward compatibility with a potential plural key.
                price_discounts = self._to_float(precios.get("precio_descuento") or precios.get("precio_descuentos"))
                if price_currency == "usd":
                    price_list_mxn = price_list * exchange_rate
                    price_special_mxn = price_special * exchange_rate
                    price_discounts_mxn = price_discounts * exchange_rate
                else:
                    price_list_mxn = price_list
                    price_special_mxn = price_special
                    price_discounts_mxn = price_discounts

                prod.write({
                    "total_existencia": total_existencia,
                    "sat_key": sat_key,
                    "price_list": price_list,
                    "price_special": price_special,
                    "price_discounts": price_discounts,
                    "price_list_mxn": price_list_mxn,
                    "price_special_mxn": price_special_mxn,
                    "price_discounts_mxn": price_discounts_mxn,
                    "exchange_rate": exchange_rate,
                    "exchange_rate_date": fields.Date.context_today(self),
                    "existence_json": existence_json,
                    "synced_at": fields.Datetime.now(),
                    "sync_error": False,
                })
                # Actualizar stock virtual SYSCOM si la ubicación existe
                if location:
                    product_template = self.env["product.template"].search(
                        [("default_code", "=", prod.model or prod.syscom_id)],
                        limit=1,
                    )
                    if product_template:
                        product_variant = product_template.product_variant_id
                        # Precios y costo también en cron
                        self._update_template_pricelists_and_cost(product_template, {
                            "list_price_mxn": price_list_mxn,
                            "special_price_mxn": price_special_mxn,
                            "discount_price_mxn": price_discounts_mxn,
                        }, params)
                        # UNSPSC from SAT key (if available)
                        self._sync_template_unspsc_from_sat(product_template, sat_key, sat_description)
                        quant = quant_model.sudo().search([
                            ("product_id", "=", product_variant.id),
                            ("location_id", "=", location.id),
                        ], limit=1)
                        if quant:
                            quant.sudo().write({"quantity": total_existencia})
                        else:
                            quant_model.sudo().create({
                                "product_id": product_variant.id,
                                "location_id": location.id,
                                "quantity": total_existencia,
                            })
                updated += 1
            except Exception as exc:
                prod.write({
                    "sync_error": str(exc),
                    "synced_at": fields.Datetime.now(),
                })
                failed += 1

        self.env["sync.syscom.log"].create({
            "name": "Actualización diaria de existencias SYSCOM",
            "kind": "info",
            "message": "Existencias actualizadas: %(u)s, fallidas: %(f)s" % {"u": updated, "f": failed},
        })

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
        min_stock = int(params.get_param("sync_syscom.min_stock") or 1)
        if min_stock < 1:
            min_stock = 1
        price_currency = params.get_param("sync_syscom.price_currency") or "usd"
        pricelist_list_id = int(params.get_param("sync_syscom.pricelist_list_id") or 0)
        pricelist_special_id = int(params.get_param("sync_syscom.pricelist_special_id") or 0)

        for product in selected_products:
            try:
                detail = client.get_product_detail(product.syscom_id) or {}

                precios = detail.get("precios") or {}
                price_list = self._to_float(precios.get("precio_lista"))
                price_special = self._to_float(precios.get("precio_especial"))
                # SYSCOM returns "precio_descuento" (singular) in /productos/{id}.
                # Keep backward compatibility with a potential plural key.
                price_discounts = self._to_float(precios.get("precio_descuento") or precios.get("precio_descuentos"))

                if price_currency == "usd":
                    price_list_mxn = price_list * exchange_rate
                    price_special_mxn = price_special * exchange_rate
                    price_discounts_mxn = price_discounts * exchange_rate
                else:
                    price_list_mxn = price_list
                    price_special_mxn = price_special
                    price_discounts_mxn = price_discounts

                name = detail.get("titulo") or product.name
                default_code = detail.get("modelo") or product.model
                description = detail.get("descripcion") or product.description or ""
                link = detail.get("link") or product.link
                total_existencia = detail.get("total_existencia") or 0
                sat_key = detail.get("sat_key") or detail.get("sat") or ""
                sat_description = detail.get("sat_description") or ""
                image_url = detail.get("img_portada") or product.image_url or ""
                brand_logo_url = detail.get("marca_logo") or ""
                existence_json = detail.get("existencia") or {}
                icons_json = detail.get("iconos") or {}
                features_json = detail.get("características") or detail.get("caracteristicas") or []
                images_json = detail.get("imágenes") or detail.get("imagenes") or []
                resources_json = detail.get("recursos") or []

                # Validación de stock mínimo antes de dar de alta/publicar
                if total_existencia < min_stock:
                    raise UserError(
                        "Stock insuficiente en SYSCOM (%s). Mínimo requerido: %s." % (total_existencia, min_stock)
                    )

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
                    "website_description": description,
                }
                if template:
                    template.write(template_vals)
                    updated += 1
                else:
                    template = self.env["product.template"].create(template_vals)
                    created += 1

                # Categoría Odoo (más profunda)
                deepest_cat = self._get_deepest_category(cat_ids)
                if deepest_cat:
                    product_category = self._ensure_product_category(deepest_cat)
                    if product_category:
                        template.categ_id = product_category.id

                # UNSPSC Category (for CFDI) from SYSCOM sat_key; do nothing if field isn't present.
                self._sync_template_unspsc_from_sat(template, sat_key, sat_description)

                # Actualizar listas de precios SYSCOM + costo
                self._update_template_pricelists_and_cost(template, {
                    "list_price_mxn": price_list_mxn,
                    "special_price_mxn": price_special_mxn,
                    "discount_price_mxn": price_discounts_mxn,
                }, params)

                # Imágenes y recursos
                self._sync_template_media_and_resources(template, detail)

            except Exception as exc:
                failed += 1
                product.write({
                    "sync_error": str(exc),
                    "synced_at": fields.Datetime.now(),
                })
                self.env["sync.syscom.log"].sudo().create({
                    "name": "Error publicación producto",
                    "kind": "error",
                    "message": "%s (%s)" % (product.name or product.syscom_id, exc),
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


class ProductTemplate(models.Model):
    _inherit = "product.template"

    syscom_cost_margin_pct = fields.Float(
        string="Margen costo SYSCOM (%)",
        help="Porcentaje de descuento aplicado sobre precio especial SYSCOM para calcular el costo.",
    )
