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

    @staticmethod
    def _to_optional_float(value):
        """Return float or False when the API value is blank/unusable."""
        if value in (None, "", False):
            return False
        try:
            return float(value)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _normalize_feature_lines(detail):
        if not isinstance(detail, dict):
            return []
        raw = (
            detail.get("caracteristicas")
            or detail.get("características")
            or detail.get("features")
            or []
        )
        if isinstance(raw, str):
            raw = raw.splitlines()
        if not isinstance(raw, (list, tuple)):
            return []
        lines = []
        for item in raw:
            text = str(item or "").strip()
            if not text:
                continue
            lines.append(text)
        return lines

    def _extract_extended_detail_values(self, detail):
        detail = detail or {}
        return {
            "warranty_text": (detail.get("garantia") or "").strip() or False,
            "weight_value": self._to_optional_float(detail.get("peso")),
            "height_value": self._to_optional_float(detail.get("alto")),
            "length_value": self._to_optional_float(detail.get("largo")),
            "width_value": self._to_optional_float(detail.get("ancho")),
            "features_lines": self._normalize_feature_lines(detail),
        }

    def _detail_has_extended_values(self, detail):
        values = self._extract_extended_detail_values(detail)
        return any(
            values[key]
            for key in ("warranty_text", "weight_value", "height_value", "length_value", "width_value")
        ) or bool(values["features_lines"])

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

    def _ensure_public_category(self, syscom_category):
        """Create/link product.public.category tree to mirror SYSCOM categories (website_id=False)."""
        if not syscom_category:
            return None
        syscom_category = syscom_category.sudo()
        if getattr(syscom_category, "public_category_id", False):
            return syscom_category.public_category_id

        parent_public = None
        if syscom_category.parent_id:
            parent_public = self._ensure_public_category(syscom_category.parent_id)

        PublicCategory = self.env["product.public.category"].sudo()
        domain = [("name", "=", syscom_category.name)]
        if "website_id" in PublicCategory._fields:
            domain.append(("website_id", "=", False))
        domain.append(("parent_id", "=", parent_public.id if parent_public else False))
        public_cat = PublicCategory.search(domain, limit=1)
        if not public_cat:
            vals = {"name": syscom_category.name}
            if parent_public:
                vals["parent_id"] = parent_public.id
            if "website_id" in PublicCategory._fields:
                vals["website_id"] = False
            if "sequence" in PublicCategory._fields:
                try:
                    vals["sequence"] = int(getattr(syscom_category, "syscom_sequence", 10) or 10)
                except Exception:
                    vals["sequence"] = 10
            public_cat = PublicCategory.create(vals)
        syscom_category.write({"public_category_id": public_cat.id})
        return public_cat

    def _raise_if_default_code_conflicts(self, default_code, syscom_product_id, exclude_template=None):
        """Block accidental writes over non-SYSCOM products sharing the same SKU."""
        default_code = (default_code or "").strip()
        syscom_product_id = (syscom_product_id or "").strip()
        if not default_code:
            return

        Template = self.env["product.template"].sudo()
        domain = [("default_code", "=", default_code)]
        if exclude_template:
            domain.append(("id", "!=", exclude_template.id))

        for template in Template.search(domain):
            template_syscom_id = (template.syscom_product_id or "").strip()
            if template.syscom_is_product and template_syscom_id == syscom_product_id:
                continue
            raise UserError(
                "Ya existe un producto Odoo con SKU '%s' y no está vinculado a este producto SYSCOM. "
                "Resuelve la colisión antes de publicar para evitar sobrescribir otro producto."
                % default_code
            )

    def _find_template_for_syscom_product(self, default_code, syscom_product_id):
        """Find an existing SYSCOM-managed template without touching unrelated products."""
        default_code = (default_code or "").strip()
        syscom_product_id = (syscom_product_id or "").strip()
        Template = self.env["product.template"].sudo()

        if syscom_product_id:
            template = Template.search([("syscom_product_id", "=", syscom_product_id)], limit=1)
            if template:
                self._raise_if_default_code_conflicts(default_code, syscom_product_id, exclude_template=template)
                return template

        if default_code:
            candidates = Template.search([
                ("default_code", "=", default_code),
                ("syscom_is_product", "=", True),
            ])
            for template in candidates:
                template_syscom_id = (template.syscom_product_id or "").strip()
                if not template_syscom_id or template_syscom_id == syscom_product_id:
                    self._raise_if_default_code_conflicts(default_code, syscom_product_id, exclude_template=template)
                    return template
            self._raise_if_default_code_conflicts(default_code, syscom_product_id)

        return Template.browse()

    def _compute_syscom_cost(self, prices_mxn, params):
        """Return standard_price using SYSCOM discount price as base."""
        cost_pct = float(params.get_param("sync_syscom.cost_discount_pct") or 4.0)
        discount_price = self._to_float((prices_mxn or {}).get("discount_price_mxn"))
        return discount_price * (1 - cost_pct / 100.0), cost_pct

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
        cost, cost_pct = self._compute_syscom_cost(prices_mxn, params)
        vals_cost = {"standard_price": cost}
        if template._fields.get("syscom_cost_margin_pct"):
            vals_cost["syscom_cost_margin_pct"] = cost_pct
        template.sudo().write(vals_cost)

    def _recompute_syscom_template_cost(self, template, staging_product=None, params=None):
        """Recalculate standard_price for one SYSCOM template from local staging prices."""
        template = template.sudo()
        params = params or self.env["ir.config_parameter"].sudo()
        staging_product = staging_product or self.search(
            [("syscom_id", "=", (template.syscom_product_id or "").strip())],
            limit=1,
        )
        if not staging_product:
            return False

        prices_mxn = {
            "list_price_mxn": staging_product.price_list_mxn,
            "special_price_mxn": staging_product.price_special_mxn,
            "discount_price_mxn": staging_product.price_discounts_mxn,
        }
        self._update_template_pricelists_and_cost(template, prices_mxn, params)
        return True

    def _build_staging_extended_vals(self, detail):
        extended = self._extract_extended_detail_values(detail)
        return {
            "warranty_text": extended["warranty_text"],
            "weight_value": extended["weight_value"] or False,
            "height_value": extended["height_value"] or False,
            "length_value": extended["length_value"] or False,
            "width_value": extended["width_value"] or False,
            "features_json": extended["features_lines"] or [],
        }

    def _apply_extended_values_to_product(self, product, detail):
        product.write(self._build_staging_extended_vals(detail))

    def _apply_extended_values_to_template(self, template, detail, staging_product=None):
        template = template.sudo()
        extended = self._extract_extended_detail_values(detail)
        if staging_product:
            extended = {
                "warranty_text": staging_product.warranty_text or extended["warranty_text"],
                "weight_value": staging_product.weight_value or extended["weight_value"],
                "height_value": staging_product.height_value or extended["height_value"],
                "length_value": staging_product.length_value or extended["length_value"],
                "width_value": staging_product.width_value or extended["width_value"],
                "features_lines": staging_product.features_json or extended["features_lines"],
            }

        vals = {
            "syscom_warranty": extended["warranty_text"] or False,
            "syscom_height_cm": extended["height_value"] or False,
            "syscom_length_cm": extended["length_value"] or False,
            "syscom_width_cm": extended["width_value"] or False,
            "syscom_features_json": extended["features_lines"] or [],
        }
        if "weight" in template._fields:
            vals["weight"] = extended["weight_value"] or False
        template.write(vals)
        template._set_syscom_ecommerce_description(extended["features_lines"])

    def _find_template_for_existing_product(self, product):
        Template = self.env["product.template"].sudo()
        syscom_product_id = (product.syscom_id or "").strip()
        default_code = (product.model or "").strip()

        if syscom_product_id:
            template = Template.search([
                ("syscom_is_product", "=", True),
                ("syscom_product_id", "=", syscom_product_id),
            ], limit=1)
            if template:
                return template

        if default_code:
            return Template.search([
                ("syscom_is_product", "=", True),
                ("default_code", "=", default_code),
            ], limit=1)

        return Template.browse()

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

    def _sync_template_uom_from_sat(self, template, uom_sat_code):
        """Set UoM on product.template from SAT unit code (clave_unidad_sat) if available in this DB."""
        code = (uom_sat_code or "").strip()
        if not code:
            return
        Uom = self.env["uom.uom"].sudo()
        # In Mexican localization, the SAT code is commonly stored in this field.
        field_name = None
        for candidate in ("l10n_mx_edi_code", "l10n_mx_edi_unece_code", "l10n_mx_edi_uom_code"):
            if candidate in Uom._fields:
                field_name = candidate
                break
        if not field_name:
            return
        uom = Uom.search([(field_name, "=", code)], limit=1)
        if not uom:
            return
        vals = {}
        if "uom_id" in template._fields and template.uom_id != uom:
            vals["uom_id"] = uom.id
        if "uom_po_id" in template._fields and template.uom_po_id != uom:
            vals["uom_po_id"] = uom.id
        if vals:
            template.sudo().write(vals)

    def _ensure_syscom_vendor_on_template(self, template):
        """Ensure SYSCOM vendor exists in supplierinfo for dropship procurement."""
        vendor = self.env.ref("sync_syscom.res_partner_syscom_vendor", raise_if_not_found=False)
        if not vendor or not template:
            return
        SupplierInfo = self.env["product.supplierinfo"].sudo()
        partner_field = "partner_id" if "partner_id" in SupplierInfo._fields else "name"
        existing = SupplierInfo.search([
            ("product_tmpl_id", "=", template.id),
            (partner_field, "=", vendor.id),
        ], limit=1)
        if existing:
            return
        vals = {
            "product_tmpl_id": template.id,
            partner_field: vendor.id,
            "min_qty": 1.0,
        }
        if "delay" in SupplierInfo._fields:
            vals["delay"] = 1
        if "currency_id" in SupplierInfo._fields:
            mxn = self.env.ref("base.MXN", raise_if_not_found=False)
            if mxn:
                vals["currency_id"] = mxn.id
        SupplierInfo.create(vals)

    def _ensure_template_published_on_website(self, template):
        """Publish product on website (eCommerce) if the field exists.

        Requirement: SYSCOM products should be published automatically.
        If website/eCommerce isn't installed, this becomes a no-op.
        """
        if not template:
            return
        vals = {}
        # website.published.mixin in modern Odoo
        if "is_published" in template._fields and not template.is_published:
            vals["is_published"] = True
        # Backward compatibility for other editions/customizations
        if "website_published" in template._fields and not getattr(template, "website_published", False):
            vals["website_published"] = True
        # If multi-website is enabled and website_id exists, set a default website.
        if "website_id" in template._fields and not template.website_id:
            try:
                website = self.env["website"].sudo().search([], limit=1)
            except Exception:
                website = None
            if website:
                vals["website_id"] = website.id
        if vals:
            template.sudo().write(vals)

    def _ensure_template_documents_published(self, template):
        """Force product documents (URLs) to be published on the website.

        In some setups, product.document defaults to private/hidden even when created by code.
        We enforce both product.document flags and the delegated ir.attachment flags.
        """
        if not template:
            return
        if "product_document_ids" not in template._fields:
            return
        docs = template.sudo().product_document_ids
        if not docs:
            return
        url_docs = docs.sudo().filtered(lambda d: getattr(d, "type", None) == "url")
        if not url_docs:
            return

        # ORM enforcement first (respects _inherits/related fields)
        for doc in url_docs:
            if getattr(doc, "type", None) != "url":
                continue
            vals = {}
            if "shown_on_product_page" in doc._fields and not doc.shown_on_product_page:
                vals["shown_on_product_page"] = True
            if "public" in doc._fields and not doc.public:
                vals["public"] = True
            # Make it available for all websites (as requested) if the field exists.
            if "website_id" in doc._fields and doc.website_id:
                vals["website_id"] = False
            if vals:
                doc.write(vals)
            if doc.ir_attachment_id:
                att_vals = {}
                if hasattr(doc.ir_attachment_id, "public") and not doc.ir_attachment_id.public:
                    att_vals["public"] = True
                if hasattr(doc.ir_attachment_id, "website_id") and getattr(doc.ir_attachment_id, "website_id", False):
                    att_vals["website_id"] = False
                if att_vals:
                    doc.ir_attachment_id.sudo().write(att_vals)

        # Some installations override create/write and re-privatize documents.
        # As a last resort, enforce via SQL (only if the columns exist).
        doc_model = self.env["product.document"].sudo()
        doc_table = getattr(doc_model, "_table", None)
        doc_ids = tuple(url_docs.ids)
        att_ids = tuple([d.ir_attachment_id.id for d in url_docs if d.ir_attachment_id])
        cr = self.env.cr

        def _column_exists(table, column):
            cr.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s LIMIT 1",
                (table, column),
            )
            return bool(cr.fetchone())

        if doc_table and doc_ids and _column_exists(doc_table, "shown_on_product_page"):
            cr.execute(
                f'UPDATE "{doc_table}" SET shown_on_product_page=TRUE WHERE id IN %s',
                (doc_ids,),
            )
        if doc_table and doc_ids and _column_exists(doc_table, "public"):
            cr.execute(
                f'UPDATE "{doc_table}" SET public=TRUE WHERE id IN %s',
                (doc_ids,),
            )
        if doc_table and doc_ids and _column_exists(doc_table, "website_id"):
            cr.execute(
                f'UPDATE "{doc_table}" SET website_id=NULL WHERE id IN %s',
                (doc_ids,),
            )

        if att_ids:
            # ir_attachment always exists
            cr.execute('UPDATE "ir_attachment" SET public=TRUE, website_id=NULL WHERE id IN %s', (att_ids,))

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
                    if "website_id" in doc._fields and doc.website_id:
                        update_vals["website_id"] = False
                    if update_vals:
                        doc.write(update_vals)
                    # product.document.public may be linked to the delegated ir.attachment.public; enforce both..
                    if doc.ir_attachment_id:
                        att_vals = {}
                        if hasattr(doc.ir_attachment_id, "public") and not doc.ir_attachment_id.public:
                            att_vals["public"] = True
                        if hasattr(doc.ir_attachment_id, "website_id") and getattr(doc.ir_attachment_id, "website_id", False):
                            att_vals["website_id"] = False
                        if att_vals:
                            doc.ir_attachment_id.sudo().write(att_vals)
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
                    # website_id left empty/False => visible on all websites
                    doc = ProductDocument.create(vals_doc)
                    # Some setups override create() and keep documents private by default.
                    # Force the publication flags after create to match the business requirement.
                    force_vals = {}
                    if not doc.shown_on_product_page:
                        force_vals["shown_on_product_page"] = True
                    if not doc.public:
                        force_vals["public"] = True
                    if "website_id" in doc._fields and doc.website_id:
                        force_vals["website_id"] = False
                    if force_vals:
                        doc.write(force_vals)
                    # Enforce delegated attachment flags too (this is what actually drives public access).
                    if doc.ir_attachment_id:
                        att_vals = {}
                        if hasattr(doc.ir_attachment_id, "public") and not doc.ir_attachment_id.public:
                            att_vals["public"] = True
                        if hasattr(doc.ir_attachment_id, "website_id") and getattr(doc.ir_attachment_id, "website_id", False):
                            att_vals["website_id"] = False
                        if att_vals:
                            doc.ir_attachment_id.sudo().write(att_vals)
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

        # Final enforcement (covers cases where create/write hooks re-private the document)
        self._ensure_template_documents_published(template)

    name = fields.Char(string="Nombre", required=True)
    syscom_id = fields.Char(string="ID SYSCOM", required=True, index=True)
    model = fields.Char(string="Modelo", index=True)
    active = fields.Boolean(string="Activo", default=True)
    selected = fields.Boolean(
        string="Lote",
        default=False,
        help="Marca persistente para procesos batch manuales. No equivale a la selección visual de la vista.",
    )
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
    stock_new = fields.Integer(string="Existencia nuevo")
    sat_key = fields.Char(string="Clave SAT")
    image_url = fields.Char(string="Imagen portada")
    brand_logo_url = fields.Char(string="Logo de marca")
    link = fields.Char(string="Link")
    existence_json = fields.Json(string="Existencias (JSON)")
    icons_json = fields.Json(string="Iconos (JSON)")
    features_json = fields.Json(string="Características (JSON)")
    images_json = fields.Json(string="Imágenes (JSON)")
    resources_json = fields.Json(string="Recursos (JSON)")
    warranty_text = fields.Char(string="Garantía")
    weight_value = fields.Float(string="Peso (kg)")
    height_value = fields.Float(string="Alto (cm)")
    length_value = fields.Float(string="Largo (cm)")
    width_value = fields.Float(string="Ancho (cm)")
    description = fields.Text(string="Descripción")
    payload = fields.Json(string="Payload SYSCOM")
    synced_at = fields.Datetime(string="Sincronizado en")
    sync_error = fields.Text(string="Último error de sync")
    publish_state = fields.Selection(
        [
            ("none", "Ninguno"),
            ("pending", "Pendiente"),
            ("processing", "Procesando"),
            ("done", "Publicado"),
            ("error", "Error"),
        ],
        string="Estado publicación",
        default="none",
        index=True,
    )
    publish_enqueued_at = fields.Datetime(string="Encolado publicación")
    publish_started_at = fields.Datetime(string="Inicio publicación")
    publish_done_at = fields.Datetime(string="Fin publicación")

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

    def _get_marked_for_batch(self):
        return self.search([("selected", "=", True)])

    def _require_records_for_view_action(self, label):
        records = self.exists()
        if not records:
            raise UserError("Selecciona al menos un modelo en la vista antes de ejecutar '%s'." % label)
        return records

    def _require_marked_for_batch(self, label):
        records = self._get_marked_for_batch()
        if not records:
            raise UserError("Marca al menos un modelo en la columna Lote antes de ejecutar '%s'." % label)
        return records

    def _describe_products_for_log(self, products, limit=10):
        products = products.exists()
        if not products:
            return "sin productos"
        chunks = []
        for product in products[:limit]:
            label = product.model or product.name or product.syscom_id or "?"
            chunks.append("%s [%s]" % (label, product.syscom_id or "?"))
        if len(products) > limit:
            chunks.append("... +%s más" % (len(products) - limit))
        return ", ".join(chunks)

    def _compute_exchange_rate_batch(self, client):
        """Return (exchange_rate, exchange_rate_date) for a batch."""
        rate_payload = client.get_exchange_rate() or {}
        try:
            exchange_rate = float(rate_payload.get("una_semana") or rate_payload.get("normal") or 1.0)
        except Exception:
            exchange_rate = 1.0
        exchange_rate_date = fields.Date.context_today(self)
        return exchange_rate, exchange_rate_date

    def _publish_one_from_detail(self, product, detail, params, exchange_rate, exchange_rate_date, price_currency):
        """Publish one staging record into product.template using an already fetched SYSCOM detail payload.

        Returns (template, created_bool).
        """
        precios = detail.get("precios") or {}
        price_list = self._to_float(precios.get("precio_lista"))
        price_special = self._to_float(precios.get("precio_especial"))
        # SYSCOM returns "precio_descuento" (singular) in /productos/{id}.
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
        total_existencia = int(detail.get("total_existencia") or 0)
        sat_key = detail.get("sat_key") or detail.get("sat") or ""
        sat_description = detail.get("sat_description") or ""
        image_url = detail.get("img_portada") or product.image_url or ""
        brand_logo_url = detail.get("marca_logo") or ""
        existence_json = detail.get("existencia") or {}
        stock_new = int((existence_json or {}).get("nuevo") or 0)
        unidad = detail.get("unidad_de_medida") or {}
        uom_sat = (unidad.get("clave_unidad_sat") or "").strip()
        icons_json = detail.get("iconos") or {}
        features_json = detail.get("características") or detail.get("caracteristicas") or []
        images_json = detail.get("imágenes") or detail.get("imagenes") or []
        resources_json = detail.get("recursos") or []

        min_stock = int(params.get_param("sync_syscom.min_stock") or 1)
        if min_stock < 1:
            min_stock = 1
        if stock_new < min_stock:
            raise UserError(
                "Stock insuficiente en SYSCOM (nuevo=%s). Mínimo requerido: %s." % (stock_new, min_stock)
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
            "stock_new": stock_new,
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
        product_vals.update(self._build_staging_extended_vals(detail))

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
        template = self._find_template_for_syscom_product(default_code, product.syscom_id)
        created = False
        template_vals = {
            "name": name,
            "default_code": default_code,
            "list_price": price_list_mxn,
            "website_description": description,
            "syscom_is_product": True,
            "syscom_product_id": product.syscom_id,
            "syscom_stock_new": stock_new,
            "syscom_stock_synced_at": fields.Datetime.now(),
            "syscom_api_ok": True,
            "syscom_uom_sat": uom_sat or False,
        }
        if template:
            template.write(template_vals)
        else:
            template = self.env["product.template"].create(template_vals)
            created = True

        self._apply_extended_values_to_template(template, detail, staging_product=product)

        self._ensure_syscom_vendor_on_template(template)

        deepest_cat = self._get_deepest_category(cat_ids)
        if deepest_cat:
            product_category = self._ensure_product_category(deepest_cat)
            if product_category:
                template.categ_id = product_category.id
            try:
                public_cat = self._ensure_public_category(deepest_cat)
                if public_cat and "public_categ_ids" in template._fields:
                    template.public_categ_ids = [(6, 0, [public_cat.id])]
            except Exception:
                pass

        self._sync_template_unspsc_from_sat(template, sat_key, sat_description)
        self._sync_template_uom_from_sat(template, uom_sat)

        self._update_template_pricelists_and_cost(template, {
            "list_price_mxn": price_list_mxn,
            "special_price_mxn": price_special_mxn,
            "discount_price_mxn": price_discounts_mxn,
        }, params)

        # Requerimiento: recursos/imagenes antes de publicar
        self._sync_template_media_and_resources(template, detail)
        self._ensure_template_published_on_website(template)
        # Enforce doc publication after publish too (some hooks depend on is_published).
        self._ensure_template_documents_published(template)

        return template, created

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
        templates = self.env["product.template"].search([
            ("syscom_is_product", "=", True),
            ("syscom_product_id", "!=", False),
        ])
        products_by_syscom_id = {
            (prod.syscom_id or "").strip(): prod
            for prod in products
            if (prod.syscom_id or "").strip()
        }
        products_by_model = {
            (prod.model or "").strip(): prod
            for prod in products
            if (prod.model or "").strip()
        }
        updated_templates = 0
        for tmpl in templates:
            prod = products_by_syscom_id.get((tmpl.syscom_product_id or "").strip())
            if not prod and tmpl.default_code:
                prod = products_by_model.get((tmpl.default_code or "").strip())
            if not prod:
                continue
            price_list_mxn = self._to_float(prod.price_list) * exchange_rate
            price_special_mxn = self._to_float(prod.price_special) * exchange_rate
            price_discounts_mxn = self._to_float(prod.price_discounts) * exchange_rate
            tmpl.write({"list_price": price_list_mxn})
            self._update_template_pricelists_and_cost(tmpl, {
                "list_price_mxn": price_list_mxn,
                "special_price_mxn": price_special_mxn,
                "discount_price_mxn": price_discounts_mxn,
            }, self.env["ir.config_parameter"].sudo())
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
        """Cron background: refresca stock (nuevo), precios y costo de productos SYSCOM publicados.

        El cron puede ejecutarse con frecuencia corta, pero se "salta" si aún no toca según settings.
        """
        params = self.env["ir.config_parameter"].sudo()
        enabled_raw = params.get_param("sync_syscom.stock_refresh_enabled")
        if str(enabled_raw).strip().lower() in ("false", "0", "no", ""):
            return

        try:
            hours = int(params.get_param("sync_syscom.stock_refresh_hours") or 4)
        except Exception:
            hours = 4
        if hours < 1:
            hours = 1

        now = fields.Datetime.now()
        last_run = params.get_param("sync_syscom.stock_refresh_last_run")
        if last_run:
            try:
                last_dt = fields.Datetime.from_string(last_run)
                if last_dt and (now - last_dt).total_seconds() < hours * 3600:
                    return
            except Exception:
                pass

        client = self._get_client()

        # Tipo de cambio (una semana) por lote
        rate_payload = client.get_exchange_rate() or {}
        try:
            exchange_rate = float(rate_payload.get("una_semana") or rate_payload.get("normal") or 1.0)
        except Exception:
            exchange_rate = 1.0
        price_currency = params.get_param("sync_syscom.price_currency") or "usd"

        # 1) Refresh staging "selected" (mantener info interna)
        selected = self._get_marked_for_batch()
        updated = failed = 0
        for prod in selected:
            try:
                detail = client.get_product_detail(prod.syscom_id) or {}
                existencia = detail.get("existencia") or {}
                stock_new = int(existencia.get("nuevo") or 0)
                total_existencia = int(detail.get("total_existencia") or 0)
                precios = detail.get("precios") or {}
                price_list = self._to_float(precios.get("precio_lista"))
                price_special = self._to_float(precios.get("precio_especial"))
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
                    "stock_new": stock_new,
                    "price_list": price_list,
                    "price_special": price_special,
                    "price_discounts": price_discounts,
                    "price_list_mxn": price_list_mxn,
                    "price_special_mxn": price_special_mxn,
                    "price_discounts_mxn": price_discounts_mxn,
                    "exchange_rate": exchange_rate,
                    "exchange_rate_date": fields.Date.context_today(self),
                    "existence_json": existencia,
                    "payload": detail,
                    "synced_at": now,
                    "sync_error": False,
                })
                self._apply_extended_values_to_product(prod, detail)
            except Exception as exc:
                prod.write({"sync_error": str(exc), "synced_at": now})

        # 2) Refresh product.template publicados (eCommerce)
        Template = self.env["product.template"].sudo()
        domain = [
            ("syscom_is_product", "=", True),
            ("syscom_product_id", "!=", False),
            ("is_published", "=", True),
        ]
        templates = Template.search(domain)

        for tmpl in templates:
            if not tmpl._has_syscom_vendor():
                continue
            try:
                detail = client.get_product_detail(tmpl.syscom_product_id) or {}
                existencia = detail.get("existencia") or {}
                stock_new = int(existencia.get("nuevo") or 0)

                unidad = detail.get("unidad_de_medida") or {}
                uom_sat = (unidad.get("clave_unidad_sat") or "").strip()
                sat_key = detail.get("sat_key") or detail.get("sat") or ""
                sat_description = detail.get("sat_description") or ""

                precios = detail.get("precios") or {}
                price_list = self._to_float(precios.get("precio_lista"))
                price_special = self._to_float(precios.get("precio_especial"))
                price_discounts = self._to_float(precios.get("precio_descuento") or precios.get("precio_descuentos"))
                if price_currency == "usd":
                    price_list_mxn = price_list * exchange_rate
                    price_special_mxn = price_special * exchange_rate
                    price_discounts_mxn = price_discounts * exchange_rate
                else:
                    price_list_mxn = price_list
                    price_special_mxn = price_special
                    price_discounts_mxn = price_discounts

                tmpl.write({
                    "list_price": price_list_mxn,
                    "syscom_stock_new": stock_new,
                    "syscom_stock_synced_at": now,
                    "syscom_api_ok": True,
                    "syscom_uom_sat": uom_sat or False,
                })
                self._sync_template_unspsc_from_sat(tmpl, sat_key, sat_description)
                self._sync_template_uom_from_sat(tmpl, uom_sat)
                self._update_template_pricelists_and_cost(tmpl, {
                    "list_price_mxn": price_list_mxn,
                    "special_price_mxn": price_special_mxn,
                    "discount_price_mxn": price_discounts_mxn,
                }, params)
                staging_product = self.search([("syscom_id", "=", tmpl.syscom_product_id)], limit=1)
                if staging_product:
                    self._apply_extended_values_to_product(staging_product, detail)
                self._apply_extended_values_to_template(tmpl, detail, staging_product=staging_product)
                # Enforce documents visibility on the website (URLs from SYSCOM resources)
                self._ensure_template_documents_published(tmpl)
                updated += 1
            except Exception:
                tmpl.write({"syscom_api_ok": False, "syscom_stock_synced_at": now})
                failed += 1

        params.set_param("sync_syscom.stock_refresh_last_run", fields.Datetime.to_string(now))

        self.env["sync.syscom.log"].sudo().create({
            "name": "Refresco stock/precios SYSCOM",
            "kind": "info",
            "message": "Plantillas SYSCOM actualizadas: %(u)s, fallidas: %(f)s. Staging marcados en lote: %(s)s" % {
                "u": updated,
                "f": failed,
                "s": len(selected),
            },
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

        selected_products = self.exists() or self._get_marked_for_batch()
        if not selected_products:
            raise UserError("No hay modelos para publicar.")
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
                total_existencia = int(detail.get("total_existencia") or 0)
                sat_key = detail.get("sat_key") or detail.get("sat") or ""
                sat_description = detail.get("sat_description") or ""
                image_url = detail.get("img_portada") or product.image_url or ""
                brand_logo_url = detail.get("marca_logo") or ""
                existence_json = detail.get("existencia") or {}
                stock_new = int((existence_json or {}).get("nuevo") or 0)
                unidad = detail.get("unidad_de_medida") or {}
                uom_sat = (unidad.get("clave_unidad_sat") or "").strip()
                icons_json = detail.get("iconos") or {}
                features_json = detail.get("características") or detail.get("caracteristicas") or []
                images_json = detail.get("imágenes") or detail.get("imagenes") or []
                resources_json = detail.get("recursos") or []

                # Validación de stock mínimo antes de dar de alta/publicar
                if stock_new < min_stock:
                    raise UserError(
                        "Stock insuficiente en SYSCOM (nuevo=%s). Mínimo requerido: %s." % (stock_new, min_stock)
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
                    "stock_new": stock_new,
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
                product_vals.update(self._build_staging_extended_vals(detail))

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
                template = self._find_template_for_syscom_product(default_code, product.syscom_id)
                template_vals = {
                    "name": name,
                    "default_code": default_code,
                    "list_price": price_list_mxn,
                    "website_description": description,
                    "syscom_is_product": True,
                    "syscom_product_id": product.syscom_id,
                    "syscom_stock_new": stock_new,
                    "syscom_stock_synced_at": fields.Datetime.now(),
                    "syscom_api_ok": True,
                    "syscom_uom_sat": uom_sat or False,
                }
                if template:
                    template.write(template_vals)
                    updated += 1
                else:
                    template = self.env["product.template"].create(template_vals)
                    created += 1

                self._apply_extended_values_to_template(template, detail, staging_product=product)

                # Ensure vendor is set for dropship procurement
                self._ensure_syscom_vendor_on_template(template)

                # Categoría Odoo (más profunda)
                deepest_cat = self._get_deepest_category(cat_ids)
                if deepest_cat:
                    product_category = self._ensure_product_category(deepest_cat)
                    if product_category:
                        template.categ_id = product_category.id
                    # Categoría eCommerce (replica el árbol y el orden de SYSCOM)
                    try:
                        public_cat = self._ensure_public_category(deepest_cat)
                        if public_cat and "public_categ_ids" in template._fields:
                            template.public_categ_ids = [(6, 0, [public_cat.id])]
                    except Exception:
                        pass

                # UNSPSC Category (for CFDI) from SYSCOM sat_key; do nothing if field isn't present.
                self._sync_template_unspsc_from_sat(template, sat_key, sat_description)
                # Unidad SAT -> UoM (si existe la localización)
                self._sync_template_uom_from_sat(template, uom_sat)

                # Actualizar listas de precios SYSCOM + costo
                self._update_template_pricelists_and_cost(template, {
                    "list_price_mxn": price_list_mxn,
                    "special_price_mxn": price_special_mxn,
                    "discount_price_mxn": price_discounts_mxn,
                }, params)

                # Imágenes/recursos primero, luego publicar.
                # (Requerimiento: asegurar que los documentos queden listos antes de que el producto sea visible.)
                self._sync_template_media_and_resources(template, detail)
                self._ensure_template_published_on_website(template)

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

    def queue_products_for_background_publish(self, products, source_label=None):
        """Queue products for background publication."""
        products = products.exists()
        if not products:
            return 0

        queued_at = fields.Datetime.now()
        products.write({
            "publish_state": "pending",
            "publish_enqueued_at": queued_at,
            "publish_started_at": False,
            "publish_done_at": False,
            "sync_error": False,
        })

        source = source_label or "selección manual"
        self.env["sync.syscom.log"].sudo().create({
            "name": "Publicación en background (inicio)",
            "kind": "info",
            "message": "Se programó la publicación de %(count)s productos. Origen: %(source)s. Modelos: %(products)s."
            % {
                "count": len(products),
                "source": source,
                "products": self._describe_products_for_log(products),
            },
        })
        return len(products)

    def action_start_publish_selected_background(self):
        """Compatibilidad: usa el modo explícito de marcados en lote."""
        return self.action_start_publish_marked_background()

    def action_start_publish_records_background(self):
        products = self._require_records_for_view_action("Publicar selección vista")
        queued = self.queue_products_for_background_publish(
            products,
            source_label="Selección vista (%s)" % ", ".join(products.mapped("syscom_id")),
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Sync SYSCOM",
                "message": "Publicación iniciada en segundo plano para la selección vista (%s productos)." % queued,
                "type": "success",
                "sticky": False,
            },
        }

    def action_start_publish_marked_background(self):
        products = self._require_marked_for_batch("Publicar marcados en lote")
        queued = self.queue_products_for_background_publish(
            products,
            source_label="Marcados en lote (%s)" % ", ".join(products.mapped("syscom_id")),
        )

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Sync SYSCOM",
                "message": "Publicación iniciada en segundo plano para marcados en lote (%s productos)." % queued,
                "type": "success",
                "sticky": False,
            },
        }

    def action_start_recompute_syscom_costs(self):
        job = self.env["sync.syscom.cost.job"].create_recompute_all_job()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Sync SYSCOM",
                "message": "Trabajo de recálculo de costos programado: %s." % job.display_name,
                "type": "success",
                "sticky": False,
            },
        }

    def action_start_sync_extended_product_data(self):
        job = self.env["sync.syscom.product.data.job"].create_sync_all_job()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Sync SYSCOM",
                "message": "Trabajo de datos extendidos programado: %s." % job.display_name,
                "type": "success",
                "sticky": False,
            },
        }

    def cron_publish_selected_products(self):
        """Cron: publica productos pendientes en batches para evitar timeouts."""
        params = self.env["ir.config_parameter"].sudo()
        try:
            batch_size = int(params.get_param("sync_syscom.publish_batch_size") or 10)
        except Exception:
            batch_size = 10
        if batch_size < 1:
            batch_size = 10

        pending = self.search(
            [("publish_state", "=", "pending")],
            order="publish_enqueued_at desc, id desc",
            limit=batch_size,
        )
        if not pending:
            return

        now = fields.Datetime.now()
        pending.write({"publish_state": "processing", "publish_started_at": now})

        client = self._get_client()
        exchange_rate, exchange_rate_date = self._compute_exchange_rate_batch(client)
        price_currency = params.get_param("sync_syscom.price_currency") or "usd"

        ok = 0
        err = 0
        for prod in pending:
            try:
                detail = client.get_product_detail(prod.syscom_id) or {}
                self._publish_one_from_detail(prod, detail, params, exchange_rate, exchange_rate_date, price_currency)
                prod.write({
                    "publish_state": "done",
                    "publish_done_at": fields.Datetime.now(),
                    "selected": False,
                })
                ok += 1
            except Exception as exc:
                err += 1
                prod.write({
                    "publish_state": "error",
                    "publish_done_at": fields.Datetime.now(),
                    "sync_error": str(exc),
                })
                self.env["sync.syscom.log"].sudo().create({
                    "name": "Error publicación background",
                    "kind": "error",
                    "message": "%s (%s)" % (prod.name or prod.syscom_id, exc),
                })
                continue

        self.env["sync.syscom.log"].sudo().create({
            "name": "Publicación en background (batch)",
            "kind": "info",
            "message": "Batch publicado. Modelos: %(products)s. OK: %(ok)s, errores: %(err)s."
            % {
                "products": self._describe_products_for_log(pending),
                "ok": ok,
                "err": err,
            },
        })
 
