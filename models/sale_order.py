import requests

from odoo import _, fields, models
from odoo.exceptions import UserError

from .syscom_client import SyscomClient
from .constants import SYSCOM_DEFAULT_BASE_URL, SYSCOM_DEFAULT_TIMEOUT


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def _syscom_validate_stock_or_raise(self, stage="confirm"):
        """Validate SYSCOM stock for dropship products.

        Rules:
        - Uses existencia.nuevo from SYSCOM.
        - If API fails: block purchase (raise).
        - If insufficient stock: block confirmation/checkout (raise).
        """
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("No se puede validar SYSCOM: falta token en Ajustes."))

        base_url = params.get_param("sync_syscom.syscom_base_url") or SYSCOM_DEFAULT_BASE_URL
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or SYSCOM_DEFAULT_TIMEOUT)
        client = SyscomClient(base_url=base_url, token=token, timeout=timeout)

        for order in self:
            # Aggregate quantities per SYSCOM id to reduce API calls.
            qty_by_syscom = {}
            tmpl_by_syscom = {}
            for line in order.order_line.filtered(lambda l: not l.display_type and l.product_id):
                tmpl = line.product_id.product_tmpl_id
                syscom_id = (tmpl.syscom_product_id or "").strip()
                if not syscom_id:
                    continue
                if not tmpl.syscom_is_product:
                    continue
                if not tmpl._has_syscom_vendor():
                    continue
                qty_by_syscom[syscom_id] = qty_by_syscom.get(syscom_id, 0.0) + line.product_uom_qty
                tmpl_by_syscom[syscom_id] = tmpl

            if not qty_by_syscom:
                continue

            for syscom_id, qty in qty_by_syscom.items():
                tmpl = tmpl_by_syscom.get(syscom_id)
                try:
                    detail = client.get_product_detail(syscom_id) or {}
                except (UserError, requests.exceptions.RequestException) as exc:
                    if tmpl:
                        tmpl.sudo().write({"syscom_api_ok": False})
                    raise UserError(
                        _("No se pudo validar existencias con SYSCOM (%(stage)s). Intenta más tarde. (%(err)s)")
                        % {"stage": stage, "err": exc}
                    )

                existencia = detail.get("existencia") or {}
                try:
                    stock_new = int(existencia.get("nuevo") or 0)
                except (TypeError, ValueError):
                    stock_new = 0

                if tmpl:
                    tmpl.sudo().write(
                        {
                            "syscom_stock_new": stock_new,
                            "syscom_stock_synced_at": fields.Datetime.now(),
                            "syscom_api_ok": True,
                        }
                    )

                if stock_new <= 0 or qty > stock_new:
                    raise UserError(
                        _(
                            "Stock insuficiente en SYSCOM para '%(name)s'. Disponible (nuevo): %(s)s. Solicitado: %(q)s."
                        )
                        % {
                            "name": (tmpl.name if tmpl else syscom_id),
                            "s": stock_new,
                            "q": qty,
                        }
                    )

    def action_confirm(self):
        # Hard validation before confirming SO (keep as quotation if blocked).
        self._syscom_validate_stock_or_raise(stage="confirm")
        return super().action_confirm()
