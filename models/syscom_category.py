import time

from odoo import _, fields, models
from odoo.exceptions import UserError

from .syscom_client import SyscomClient


class SyscomCategory(models.Model):
    _name = "sync.syscom.category"
    _description = "Categoría SYSCOM"
    _order = "name"

    name = fields.Char(string="Nombre", required=True)
    syscom_id = fields.Char(string="ID SYSCOM", required=True, index=True)
    level = fields.Integer(string="Nivel")
    active = fields.Boolean(string="Activo", default=True)
    parent_id = fields.Many2one(
        "sync.syscom.category",
        string="Categoría padre",
        ondelete="set null",
    )
    child_ids = fields.One2many(
        "sync.syscom.category",
        "parent_id",
        string="Subcategorías",
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
        categories = client.get_categories() or []
        data_map = {}
        parent_map = {}

        def add_category(payload, parent_syscom_id=None):
            if not isinstance(payload, dict):
                return
            syscom_id = str(payload.get("id") or "").strip()
            if not syscom_id:
                return
            data_map[syscom_id] = {
                "syscom_id": syscom_id,
                "name": payload.get("nombre") or syscom_id,
                "level": int(payload.get("nivel") or 0) or False,
                "active": True,
            }
            if parent_syscom_id:
                parent_map[syscom_id] = str(parent_syscom_id)

        for category in categories:
            add_category(category)
            category_id = category.get("id")
            if not category_id:
                continue
            detail = client.get_category_detail(category_id) or {}
            origin_entries = detail.get("origen")
            parent_origin_id = None
            if isinstance(origin_entries, list) and origin_entries:
                parent_origin_id = origin_entries[0].get("id")
            elif isinstance(origin_entries, dict):
                parent_origin_id = origin_entries.get("id")
            add_category(detail, parent_syscom_id=parent_origin_id)

            if isinstance(origin_entries, list):
                for origin in origin_entries:
                    add_category(origin)
            elif isinstance(origin_entries, dict):
                add_category(origin_entries)

            for subcategory in detail.get("subcategorías") or detail.get("subcategorias") or []:
                add_category(subcategory, parent_syscom_id=detail.get("id"))

        created = 0
        updated = 0
        for values in data_map.values():
            record = self.search([("syscom_id", "=", values["syscom_id"])], limit=1)
            if record:
                record.write(values)
                updated += 1
            else:
                self.create(values)
                created += 1

        for child_syscom_id, parent_syscom_id in parent_map.items():
            child = self.search([("syscom_id", "=", child_syscom_id)], limit=1)
            parent = self.search([("syscom_id", "=", parent_syscom_id)], limit=1)
            if child and parent:
                child.parent_id = parent.id

        duration = time.monotonic() - start_time
        self.env["sync.syscom.log"].create({
            "name": _("Sincronización de categorías"),
            "kind": "info",
            "message": _("Categorías creadas: %(created)s, actualizadas: %(updated)s. Duración: %(duration).2fs")
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
