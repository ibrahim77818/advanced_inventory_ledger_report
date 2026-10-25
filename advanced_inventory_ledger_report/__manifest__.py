# -*- coding: utf-8 -*-
{
    'name': 'Advanced Inventory Ledger Report',
    'version': '19.0.1.0.0',
    'category': 'Inventory/Inventory',
    'summary': 'High-performance SQL inventory ledger: opening, inbound, outbound '
               'and closing stock & value per warehouse - Browser, Excel and PDF',
    'description': """
Advanced Inventory Ledger Report
================================
* Set-based PostgreSQL engine (CTEs + window functions) - one statement per run
* Two opening-stock strategies: full move history (audit) or current-quant roll-back (fastest)
* Summary (per product / warehouse) and Detailed (move-by-move running balance) modes
* Multi-company and multi-warehouse, inter-warehouse transfers shown as Internal IN / OUT
* Valuation from Odoo 19's own engine (stock move values, average cost at date)
* Interactive list / pivot / graph views with drill-down to stock moves
* Branded XLSX export (xlsxwriter) and landscape QWeb PDF
* Works on Community and Enterprise
""",
    'author': 'Ibrahim Yahia',
    'website': 'https://www.linkedin.com/in/ibrahim-yahia',
    'license': 'LGPL-3',
    'depends': ['stock', 'stock_account'],
    'external_dependencies': {'python': ['xlsxwriter']},
    'data': [
        'security/ir.model.access.csv',
        'security/inventory_ledger_security.xml',
        'report/inventory_ledger_report.xml',
        'report/inventory_ledger_templates.xml',
        'views/inventory_ledger_line_views.xml',
        'wizard/inventory_ledger_wizard_views.xml',
        'views/menus.xml',
    ],
    'images': ['static/description/banner.png'],
    'uninstall_hook': 'uninstall_hook',
    'installable': True,
    'application': False,
}
