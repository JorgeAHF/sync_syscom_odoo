from odoo.addons.website_sale.controllers.main import WebsiteSale
from odoo.exceptions import UserError
from odoo.http import request


class WebsiteSaleSyscom(WebsiteSale):
    """WebsiteSale hooks to block checkout when SYSCOM stock cannot be validated."""

    def _checkout_redirection(self, order, **post):
        # Keep original redirections first (empty cart, etc.)
        res = super()._checkout_redirection(order, **post)
        if res:
            return res

        if not order:
            return res

        try:
            order.sudo()._syscom_validate_stock_or_raise(stage="checkout")
        except UserError as exc:
            # Show a friendly message on cart page and block continuing to checkout.
            request.session["syscom_error"] = str(exc)
            return request.redirect("/shop/cart")
        # Clear any previous error once validation passes.
        request.session.pop("syscom_error", None)
        return res
