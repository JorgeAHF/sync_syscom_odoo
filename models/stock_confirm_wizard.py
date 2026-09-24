from odoo import fields, models


class SyncSyscomStockConfirmWizard(models.TransientModel):
    """Aviso previo a confirmar una venta cuando SYSCOM reporta stock insuficiente.

    No bloquea por sí mismo: solo se interpone entre el clic en "Confirmar" y la
    confirmación real. Quien vende decide si continúa. Si cierra el asistente sin
    apretar "Confirmar de todas formas", la orden se queda como cotización, tal
    cual estaba -- no se escribe nada.
    """

    _name = "sync.syscom.stock.confirm.wizard"
    _description = "Aviso de stock insuficiente en SYSCOM al confirmar una venta"

    order_ids = fields.Many2many("sale.order", string="Órdenes")
    message = fields.Text(string="Detalle", readonly=True)

    def action_confirm_anyway(self):
        """El usuario decidió seguir a pesar del aviso: confirma de verdad."""
        self.ensure_one()
        orders = self.order_ids
        for order in orders:
            order.message_post(
                body="⚠ Se confirmó a pesar del aviso de stock insuficiente en SYSCOM "
                "(decisión manual desde el asistente)."
            )
        return orders.with_context(sync_syscom_skip_stock_check=True).action_confirm()

    def action_cancel(self):
        """No se confirma nada: la orden se queda como cotización."""
        return {"type": "ir.actions.act_window_close"}
