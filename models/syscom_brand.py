import time

from odoo import _, fields, models
from odoo.exceptions import UserError

from .syscom_client import SyscomClient


class SyscomBrand(models.Model):
    _name = "sync.syscom.brand"
    _description = "Marca SYSCOM"
    _order = "name"

    name = fields.Char(string="Nombre", required=True)
    syscom_id = fields.Char(string="ID SYSCOM", required=True, index=True)
    title = fields.Char(string="Título")
    description = fields.Text(string="Descripción")
    logo_url = fields.Char(string="Logo URL")
    active = fields.Boolean(string="Activo", default=True)
    category_ids = fields.Many2many(
        "sync.syscom.category",
        "sync_syscom_brand_category_rel",
        "brand_id",
        "category_id",
        string="Categorías",
    )

    _syscom_id_unique = models.Constraint(
        "UNIQUE(syscom_id)",
        "El ID SYSCOM debe ser único.",
    )

    def action_sync_syscom(self):
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("Configura el token en Ajustes antes de sincronizar."))

        base_url = params.get_param("sync_syscom.syscom_base_url") or "https://developers.syscom.mx/api/v1"
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or 30)
        client = SyscomClient(base_url=base_url, token=token, timeout=timeout)

        start_time = time.monotonic()
        brands = client.get_brands() or []
        created = 0
        updated = 0
        category_links = {}

        for brand in brands:
            syscom_id = str(brand.get("id") or "").strip()
            if not syscom_id:
                continue
            values = {
                "syscom_id": syscom_id,
                "name": brand.get("nombre") or syscom_id,
                "active": True,
            }
            detail = client.get_brand_detail(syscom_id) or {}
            if isinstance(detail, dict):
                values.update({
                    "title": detail.get("titulo") or values["name"],
                    "description": detail.get("descripcion") or "",
                    "logo_url": detail.get("logo") or "",
                })
                categories = detail.get("categorías") or detail.get("categorias") or []
                if categories:
                    category_links[syscom_id] = categories

            record = self.search([("syscom_id", "=", syscom_id)], limit=1)
            if record:
                record.write(values)
                updated += 1
            else:
                record = self.create(values)
                created += 1

            if syscom_id in category_links:
                category_ids = []
                for category in category_links[syscom_id]:
                    category_syscom_id = str(category.get("id") or "").strip()
                    if not category_syscom_id:
                        continue
                    category_record = self.env["sync.syscom.category"].search(
                        [("syscom_id", "=", category_syscom_id)],
                        limit=1,
                    )
                    if category_record:
                        category_ids.append(category_record.id)
                if category_ids:
                    record.category_ids = [(6, 0, category_ids)]

        duration = time.monotonic() - start_time
        self.env["sync.syscom.log"].create({
            "name": _("Sincronización de marcas"),
            "kind": "info",
            "message": _("Marcas creadas: %(created)s, actualizadas: %(updated)s. Duración: %(duration).2fs")
            % {
                "created": created,
                "updated": updated,
                "duration": duration,
            },
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sync SYSCOM"),
                "message": _("Sincronización completada. Creadas: %(created)s, actualizadas: %(updated)s.")
                % {"created": created, "updated": updated},
                "type": "success",
                "sticky": False,
            },
        }
