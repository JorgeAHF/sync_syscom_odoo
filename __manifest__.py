{
    "name": "Sync Syscom",
    "version": "0.0.1",
    "summary": "Integración SYSCOM – Odoo",
    "category": "Sales",
    "depends": ["base", "product", "stock", "sale"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "views/menu.xml",
    ],
    "installable": True,
    "application": False,
}
