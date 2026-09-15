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
        - Si la API falla (sin token, timeout, error de red): sigue bloqueando
          la confirmación -- eso significa que no se pudo validar nada, no que
          se validó y falta stock.
        - Si el stock es insuficiente: YA NO bloquea. Decisión de Jorge
          (12/09/2026): el 0 de SYSCOM puede no ser el 0 real -- HERGON puede
          tener el producto por otra vía (stock propio, otro proveedor) que
          este chequeo no ve. Ahora solo se junta como aviso preventivo y
          quien confirma decide si de verdad se puede surtir.

        Devuelve una lista de dicts (order, name, disponible, solicitado) con
        los avisos de stock insuficiente encontrados, para que action_confirm
        los muestre sin frenar la confirmación.
        """
        params = self.env["ir.config_parameter"].sudo()
        token = (params.get_param("sync_syscom.syscom_api_token") or "").strip()
        if not token:
            raise UserError(_("No se puede validar SYSCOM: falta token en Ajustes."))

        base_url = params.get_param("sync_syscom.syscom_base_url") or SYSCOM_DEFAULT_BASE_URL
        timeout = int(params.get_param("sync_syscom.syscom_timeout") or SYSCOM_DEFAULT_TIMEOUT)
        client = SyscomClient(base_url=base_url, token=token, timeout=timeout)

        avisos = []
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
                    avisos.append({
                        "order": order,
                        "name": (tmpl.name if tmpl else syscom_id),
                        "disponible": stock_new,
                        "solicitado": qty,
                    })
        return avisos

    def _syscom_post_stock_warnings(self, avisos):
        """Deja constancia en el chatter de cada orden con avisos, uno por orden."""
        for order in self:
            avisos_orden = [a for a in avisos if a["order"] == order]
            if not avisos_orden:
                continue
            lineas = "\n".join(
                _("- %(name)s: disponible en SYSCOM %(s)s, solicitado %(q)s")
                % {"name": a["name"], "s": a["disponible"], "q": a["solicitado"]}
                for a in avisos_orden
            )
            order.message_post(
                body=_(
                    "⚠ Aviso de stock SYSCOM al confirmar (no bloqueó la venta):\n%s"
                ) % lineas
            )

    def action_confirm(self):
        # Validación de SYSCOM: bloquea solo si no se pudo consultar (sin token,
        # error de red). Si se pudo consultar y el stock no alcanza, ya no
        # bloquea -- se confirma la orden y queda el aviso en el chatter y en
        # una notificación al momento del clic.
        avisos = self._syscom_validate_stock_or_raise(stage="confirm")
        result = super().action_confirm()
        if avisos:
            self._syscom_post_stock_warnings(avisos)
            # Si el confirm de Odoo no devolvió su propia acción de cliente
            # (wizard, redirección, etc.), aprovechamos el hueco para mostrar
            # el aviso como notificación. Si sí devolvió algo, se respeta tal
            # cual -- no lo pisamos.
            if not isinstance(result, dict):
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": _("Aviso de stock SYSCOM"),
                        "message": _(
                            "Se confirmó la orden, pero SYSCOM reportó stock insuficiente "
                            "para %(n)s producto(s). Revisa el detalle en el chatter de la orden."
                        ) % {"n": len(avisos)},
                        "type": "warning",
                        "sticky": True,
                    },
                }
        return result
