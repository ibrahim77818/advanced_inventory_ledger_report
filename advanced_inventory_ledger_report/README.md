# Advanced Inventory Ledger Report (Odoo 16 · Community & Enterprise)

Inventory → Reporting → **Inventory Ledger**

Opening stock, inbound, outbound and closing stock (quantity and value) per
product and warehouse, computed by **one PostgreSQL statement per run**.
Output to an interactive list / pivot / graph, a branded XLSX, or a landscape PDF.

## Directory layout

```
advanced_inventory_ledger_report/
├── __init__.py                     uninstall hook (drops helper indexes)
├── __manifest__.py
├── controllers/main.py             /advanced_inventory_ledger_report/xlsx/<id> (streamed download)
├── models/
│   ├── inventory_ledger_engine.py  SQL engine: CTEs + window functions, INSERT ... SELECT
│   ├── inventory_ledger_line.py    result rows (transient), drill-down, helper indexes
│   └── inventory_ledger_xlsx.py    xlsxwriter generator (summary / detailed)
├── wizard/
│   ├── inventory_ledger_wizard.py  filters, security checks, output actions, report helpers
│   └── inventory_ledger_wizard_views.xml
├── report/
│   ├── inventory_ledger_report.xml     paper format + ir.actions.report
│   └── inventory_ledger_templates.xml  QWeb PDF
├── i18n/                           ar.po (Arabic) + .pot template
├── static/description/             apps.odoo.com page (index.html), icon, banner, screenshots
├── security/                       ACL + record rules (own runs, multi-company)
├── views/                          list / pivot / graph / search views, menu
└── tests/test_inventory_ledger.py  TransactionCase suite
```

## How the numbers are built

| Concept | Rule |
|---|---|
| Warehouse scope | every `internal` location under the warehouse's view location |
| IN leg | done move whose destination is in the warehouse and source is not |
| OUT leg | done move whose source is in the warehouse and destination is not |
| Moves inside one warehouse | ignored (put-away, pick, pack: net zero) |
| WH1 → WH2 transfer | Internal OUT for WH1, Internal IN for WH2 |
| Classification | by the other location's usage: supplier, customer, internal/transit, other (inventory loss, production, scrap) |
| Quantities | done quantity `stock_move.quantity`, converted to the product's UoM (in Odoo 17 `product_qty` keeps the original demand after an over-receipt) |
| Dates | period boundaries are whole days in the user's timezone, converted to UTC |

### Opening stock method

* **Fast (default)**: opening = current quants − everything moved since the start date.
  Reads only moves from the start date onwards.
* **Audit**: opening = sum of every done move before the start date. Independent of
  quants; run both and compare to detect quant / move inconsistencies.

### Valuation

Taken from `stock_valuation_layer` (company level):

* Move value = quantity × that move's layer unit cost; unvalued moves (internal
  transfers) use the average cost at the end date.
* Opening / closing value = quantity × average unit cost (Σvalue / Σqty of layers)
  at the start / end date.

Run company-wide, total closing value equals the Stock Valuation report. Per
warehouse it is an allocation at company average cost, because Odoo values
stock per company, not per location.

## Languages and layout

* Arabic translation included (`i18n/ar.po`): wizard, views, PDF and Excel.
* Right-to-left languages: the PDF mirrors automatically, dates stay readable
  (isolated left-to-right), and the Excel sheet opens right-to-left.
* PDF paper format (A4 landscape) reserves 52 mm at the top so the company header
  (logo + 4-line address) never overlaps the table on any page. If your report
  layout has a taller header, raise *Top Margin* and *Header spacing* on the
  "Inventory Ledger (A4 Landscape)" paper format.

## Performance notes

* The module adds a partial index `stock_move (product_id, date) WHERE state = 'done'`
  and `stock_valuation_layer (product_id, company_id, create_date)`; both are dropped
  on uninstall.
* Results live in a transient table, isolated per user by record rules and purged
  after 24 hours.

## Tests

```bash
odoo-bin -d <db> -i advanced_inventory_ledger_report \
         --test-tags /advanced_inventory_ledger_report --stop-after-init
```

The suite checks bucket classification, closing quantity vs `qty_available` (current
and `to_date`), closing value vs `value_svl`, agreement between the two opening
methods over several periods, filters, the detailed running balance, XLSX and QWeb
rendering, drill-down actions and the multi-company / own-run security rules.
