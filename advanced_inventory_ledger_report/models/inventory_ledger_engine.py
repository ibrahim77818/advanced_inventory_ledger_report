# -*- coding: utf-8 -*-
"""Set-based inventory ledger engine.

Design
------
All quantities of a report run come from ONE SQL statement
(``WITH ... INSERT INTO ... SELECT``) executed server-side in PostgreSQL.
No product loop, no per-move Python work - cost grows with the number of
*moves in scope*, not with products x warehouses x days. Valuation is a
second, batched step that reuses Odoo 19's own valuation engine (see below).

Scope model
-----------
A warehouse = every ``internal`` location under its ``view_location_id``
(matched through ``parent_path``). A done stock move produces a *leg* for
each warehouse it enters or leaves:

* destination inside warehouse W and source outside W  ->  IN  leg for W
* source inside warehouse W and destination outside W   ->  OUT leg for W
* source and destination both inside W                  ->  ignored (net 0)

So a transfer WH1 -> WH2 is an Internal OUT for WH1 and an Internal IN for
WH2, while put-away / picking moves inside one warehouse do not inflate
the totals.

Each leg is classified by the usage of the *other* location:

=================  ==========================  ==========================
other usage        IN leg                      OUT leg
=================  ==========================  ==========================
supplier           in_purchase                 out_return (vendor return)
customer           in_return (customer ret.)   out_sale
internal/transit   in_internal                 out_internal
inventory/prod...  in_other                    out_other
=================  ==========================  ==========================

Quantities
----------
Since Odoo 17 the done quantity is stored in ``stock_move.quantity`` (move UoM); ``product_qty``
keeps the original demand when more than the demand is processed. Legs therefore use
``quantity`` converted to the product UoM. Odoo 19 redefined ``uom_uom.factor`` as the
*absolute quantity* of the reference unit (Dozen = 12), so the conversion is
qty * move_uom.factor / product_uom.factor (the inverse of Odoo 16-18).

Opening stock strategies
------------------------
``moves``   opening = SUM(legs before date_from). Reads the whole move
            history up to date_to. Audit-exact, independent of quants.
``quants``  opening = current quants - SUM(legs since date_from). Reads
            only moves from date_from onwards - typically 10-100x fewer
            rows for recent periods. Relies on quants being consistent
            with moves (true in a healthy database).

Valuation (Odoo 19)
-------------------
Odoo 19 removed ``stock.valuation.layer``; value now lives on ``stock.move``
(``value``, ``is_in``, ``is_out``) and ``product.product.avg_cost`` /
``total_value`` are computed by the stock_account engine for any date.
* move value = qty x the move's own unit cost (|stock_move.value| / qty) for
  moves Odoo values (entering or leaving the company); unvalued moves, e.g.
  inter-warehouse transfers, use the average cost at date_to. Unvalued
  quantities are parked in ``in_qty_unvalued`` / ``out_qty_unvalued`` by the
  SQL step and priced in the valuation step.
* opening / closing value = qty x ``avg_cost`` computed by Odoo with
  ``to_date`` = just before date_from / date_to, per company.
Run company-wide, total closing value equals ``product.total_value``. Odoo
19 values a warehouse as company value x warehouse qty share, which is the
same allocation this report uses.
"""
from collections import defaultdict
from datetime import timedelta

from odoo import api, models

COMMON_CTES = """
WITH wh AS (
    SELECT w.id AS warehouse_id, w.company_id, vl.parent_path
      FROM stock_warehouse w
      JOIN stock_location vl ON vl.id = w.view_location_id
     WHERE w.company_id = ANY(%(company_ids)s)
       AND (%(warehouse_ids)s::int[] IS NULL OR w.id = ANY(%(warehouse_ids)s::int[]))
),
loc AS (
    SELECT l.id AS location_id, wh.warehouse_id, wh.company_id
      FROM stock_location l
      JOIN wh ON l.parent_path LIKE wh.parent_path || '%%'
     WHERE l.usage = 'internal'
),
prod AS (
    SELECT pp.id AS product_id, pt.categ_id, pt.uom_id
      FROM product_product pp
      JOIN product_template pt ON pt.id = pp.product_tmpl_id
     WHERE pt.is_storable
       AND (%(product_ids)s::int[] IS NULL OR pp.id = ANY(%(product_ids)s::int[]))
       AND (%(categ_ids)s::int[] IS NULL OR pt.categ_id = ANY(%(categ_ids)s::int[]))
),
legs AS (
    -- IN legs: destination inside the warehouse, source outside it
    SELECT m.id AS move_id, m.date, m.product_id, d.warehouse_id, d.company_id,
           1 AS direction, m.quantity * mu.factor / pu.factor AS qty, src.usage AS other_usage,
           CASE WHEN m.is_in OR m.is_out THEN m.value END AS move_value
      FROM stock_move m
      JOIN prod ON prod.product_id = m.product_id
      JOIN uom_uom mu ON mu.id = m.product_uom
      JOIN uom_uom pu ON pu.id = prod.uom_id
      JOIN loc d ON d.location_id = m.location_dest_id
      JOIN stock_location src ON src.id = m.location_id
 LEFT JOIN loc s ON s.location_id = m.location_id AND s.warehouse_id = d.warehouse_id
     WHERE m.state = 'done'
       AND s.location_id IS NULL
       AND (%(use_quants)s OR m.date <= %(date_to)s)
       AND (NOT %(use_quants)s OR m.date >= %(date_from)s)
    UNION ALL
    -- OUT legs: source inside the warehouse, destination outside it
    SELECT m.id, m.date, m.product_id, s.warehouse_id, s.company_id,
           -1, m.quantity * mu.factor / pu.factor, dst.usage,
           CASE WHEN m.is_in OR m.is_out THEN m.value END
      FROM stock_move m
      JOIN prod ON prod.product_id = m.product_id
      JOIN uom_uom mu ON mu.id = m.product_uom
      JOIN uom_uom pu ON pu.id = prod.uom_id
      JOIN loc s ON s.location_id = m.location_id
      JOIN stock_location dst ON dst.id = m.location_dest_id
 LEFT JOIN loc d ON d.location_id = m.location_dest_id AND d.warehouse_id = s.warehouse_id
     WHERE m.state = 'done'
       AND d.location_id IS NULL
       AND (%(use_quants)s OR m.date <= %(date_to)s)
       AND (NOT %(use_quants)s OR m.date >= %(date_from)s)
),
classified AS (
    SELECT legs.*,
           CASE
               WHEN legs.date < %(date_from)s THEN 'opening'
               WHEN legs.date > %(date_to)s THEN 'future'
               WHEN legs.direction = 1 THEN
                   CASE legs.other_usage
                       WHEN 'supplier' THEN 'in_purchase'
                       WHEN 'customer' THEN 'in_return'
                       WHEN 'internal' THEN 'in_internal'
                       WHEN 'transit'  THEN 'in_internal'
                       ELSE 'in_other'
                   END
               ELSE
                   CASE legs.other_usage
                       WHEN 'customer' THEN 'out_sale'
                       WHEN 'supplier' THEN 'out_return'
                       WHEN 'internal' THEN 'out_internal'
                       WHEN 'transit'  THEN 'out_internal'
                       ELSE 'out_other'
                   END
           END AS bucket
      FROM legs
),
period_moves AS (
    SELECT c.*,
           CASE WHEN c.move_value IS NOT NULL AND c.qty <> 0
                THEN ABS(c.move_value) / c.qty END AS unit_cost
      FROM classified c
     WHERE c.bucket NOT IN ('opening', 'future')
),
opening AS (
    SELECT company_id, warehouse_id, product_id, SUM(qty) AS opening_qty
      FROM (
            -- strategy "moves": history before the period
            SELECT company_id, warehouse_id, product_id, direction * qty AS qty
              FROM classified
             WHERE bucket = 'opening'
            UNION ALL
            -- strategy "quants": stock on hand now ...
            SELECT loc.company_id, loc.warehouse_id, q.product_id, q.quantity
              FROM stock_quant q
              JOIN loc ON loc.location_id = q.location_id
              JOIN prod ON prod.product_id = q.product_id
             WHERE %(use_quants)s
            UNION ALL
            -- ... rolled back by everything that happened since date_from
            SELECT company_id, warehouse_id, product_id, -direction * qty
              FROM classified
             WHERE %(use_quants)s AND bucket <> 'opening'
           ) x
  GROUP BY company_id, warehouse_id, product_id
)
"""

SUMMARY_INSERT = """
INSERT INTO inventory_ledger_line (
    wizard_id, report_type, line_type, company_id, warehouse_id, product_id, categ_id, uom_id,
    opening_qty, in_purchase_qty, in_return_qty, in_internal_qty, in_other_qty, in_qty,
    out_sale_qty, out_return_qty, out_internal_qty, out_other_qty, out_qty, closing_qty,
    unit_cost, opening_value, in_value, out_value, closing_value,
    in_qty_unvalued, out_qty_unvalued,
    create_uid, create_date, write_uid, write_date)
SELECT %(wizard_id)s, 'summary', 'summary',
       a.company_id, a.warehouse_id, a.product_id, prod.categ_id, prod.uom_id,
       a.opening_qty, a.in_purchase_qty, a.in_return_qty, a.in_internal_qty, a.in_other_qty, a.in_qty,
       a.out_sale_qty, a.out_return_qty, a.out_internal_qty, a.out_other_qty, a.out_qty,
       a.opening_qty + a.in_qty - a.out_qty,
       0, 0, a.in_value, a.out_value, 0,
       a.in_qty_unvalued, a.out_qty_unvalued,
       %(uid)s, (now() AT TIME ZONE 'UTC'), %(uid)s, (now() AT TIME ZONE 'UTC')
  FROM (
        SELECT COALESCE(o.company_id, p.company_id) AS company_id,
               COALESCE(o.warehouse_id, p.warehouse_id) AS warehouse_id,
               COALESCE(o.product_id, p.product_id) AS product_id,
               COALESCE(o.opening_qty, 0) AS opening_qty,
               COALESCE(p.in_purchase_qty, 0) AS in_purchase_qty,
               COALESCE(p.in_return_qty, 0) AS in_return_qty,
               COALESCE(p.in_internal_qty, 0) AS in_internal_qty,
               COALESCE(p.in_other_qty, 0) AS in_other_qty,
               COALESCE(p.in_qty, 0) AS in_qty,
               COALESCE(p.out_sale_qty, 0) AS out_sale_qty,
               COALESCE(p.out_return_qty, 0) AS out_return_qty,
               COALESCE(p.out_internal_qty, 0) AS out_internal_qty,
               COALESCE(p.out_other_qty, 0) AS out_other_qty,
               COALESCE(p.out_qty, 0) AS out_qty,
               COALESCE(p.in_value, 0) AS in_value,
               COALESCE(p.out_value, 0) AS out_value,
               COALESCE(p.in_qty_unvalued, 0) AS in_qty_unvalued,
               COALESCE(p.out_qty_unvalued, 0) AS out_qty_unvalued
          FROM opening o
     FULL JOIN (
                SELECT company_id, warehouse_id, product_id,
                       SUM(CASE WHEN bucket = 'in_purchase'  THEN qty ELSE 0 END) AS in_purchase_qty,
                       SUM(CASE WHEN bucket = 'in_return'    THEN qty ELSE 0 END) AS in_return_qty,
                       SUM(CASE WHEN bucket = 'in_internal'  THEN qty ELSE 0 END) AS in_internal_qty,
                       SUM(CASE WHEN bucket = 'in_other'     THEN qty ELSE 0 END) AS in_other_qty,
                       SUM(CASE WHEN direction = 1  THEN qty ELSE 0 END) AS in_qty,
                       SUM(CASE WHEN bucket = 'out_sale'     THEN qty ELSE 0 END) AS out_sale_qty,
                       SUM(CASE WHEN bucket = 'out_return'   THEN qty ELSE 0 END) AS out_return_qty,
                       SUM(CASE WHEN bucket = 'out_internal' THEN qty ELSE 0 END) AS out_internal_qty,
                       SUM(CASE WHEN bucket = 'out_other'    THEN qty ELSE 0 END) AS out_other_qty,
                       SUM(CASE WHEN direction = -1 THEN qty ELSE 0 END) AS out_qty,
                       SUM(CASE WHEN direction = 1  THEN COALESCE(qty * unit_cost, 0) ELSE 0 END) AS in_value,
                       SUM(CASE WHEN direction = -1 THEN COALESCE(qty * unit_cost, 0) ELSE 0 END) AS out_value,
                       SUM(CASE WHEN direction = 1  AND unit_cost IS NULL THEN qty ELSE 0 END) AS in_qty_unvalued,
                       SUM(CASE WHEN direction = -1 AND unit_cost IS NULL THEN qty ELSE 0 END) AS out_qty_unvalued
                  FROM period_moves
              GROUP BY company_id, warehouse_id, product_id
               ) p
            ON p.company_id = o.company_id
           AND p.warehouse_id = o.warehouse_id
           AND p.product_id = o.product_id
       ) a
  JOIN prod ON prod.product_id = a.product_id
 WHERE NOT %(hide_zero)s
    OR ABS(a.opening_qty) > %(epsilon)s
    OR a.in_qty <> 0
    OR a.out_qty <> 0
"""

DETAILED_INSERT = """
INSERT INTO inventory_ledger_line (
    wizard_id, report_type, line_type, sequence, company_id, warehouse_id, product_id, categ_id, uom_id,
    date, move_id, picking_id, reference, partner_id, movement_type,
    opening_qty, in_qty, out_qty, balance_qty, unit_cost, opening_value, in_value, out_value,
    in_qty_unvalued, out_qty_unvalued,
    create_uid, create_date, write_uid, write_date)
SELECT %(wizard_id)s, 'detailed', r.line_type,
       ROW_NUMBER() OVER w_seq,
       r.company_id, r.warehouse_id, r.product_id, prod.categ_id, prod.uom_id,
       r.date, r.move_id, m.picking_id,
       CASE WHEN r.move_id IS NULL THEN NULL ELSE COALESCE(m.reference, m.origin) END,  -- Odoo 19: stock_move.name removed
       COALESCE(sp.partner_id, m.partner_id),
       r.movement_type,
       r.opening_qty, r.in_qty, r.out_qty,
       SUM(r.opening_qty + r.in_qty - r.out_qty) OVER w_bal,
       COALESCE(r.unit_cost, 0),
       0,
       COALESCE(r.in_qty * r.unit_cost, 0),
       COALESCE(r.out_qty * r.unit_cost, 0),
       CASE WHEN r.move_id IS NOT NULL AND r.unit_cost IS NULL THEN r.in_qty ELSE 0 END,
       CASE WHEN r.move_id IS NOT NULL AND r.unit_cost IS NULL THEN r.out_qty ELSE 0 END,
       %(uid)s, (now() AT TIME ZONE 'UTC'), %(uid)s, (now() AT TIME ZONE 'UTC')
  FROM (
        -- one opening row per (company, warehouse, product) in scope
        SELECT k.company_id, k.warehouse_id, k.product_id,
               'opening' AS line_type, 0 AS ord,
               %(date_from)s::timestamp AS date, NULL::integer AS move_id,
               'opening' AS movement_type,
               COALESCE(o.opening_qty, 0) AS opening_qty,
               0::numeric AS in_qty, 0::numeric AS out_qty, 0::numeric AS unit_cost
          FROM (
                SELECT company_id, warehouse_id, product_id FROM opening
                UNION
                SELECT company_id, warehouse_id, product_id FROM period_moves
               ) k
     LEFT JOIN opening o
            ON o.company_id = k.company_id
           AND o.warehouse_id = k.warehouse_id
           AND o.product_id = k.product_id
         WHERE NOT %(hide_zero)s
            OR ABS(COALESCE(o.opening_qty, 0)) > %(epsilon)s
            OR EXISTS (SELECT 1 FROM period_moves pm
                        WHERE pm.company_id = k.company_id
                          AND pm.warehouse_id = k.warehouse_id
                          AND pm.product_id = k.product_id)
        UNION ALL
        -- one row per warehouse leg of each move in the period
        SELECT pm.company_id, pm.warehouse_id, pm.product_id,
               'move', 1, pm.date, pm.move_id, pm.bucket,
               0,
               CASE WHEN pm.direction = 1  THEN pm.qty ELSE 0 END,
               CASE WHEN pm.direction = -1 THEN pm.qty ELSE 0 END,
               pm.unit_cost
          FROM period_moves pm
       ) r
  JOIN prod ON prod.product_id = r.product_id
LEFT JOIN stock_move m ON m.id = r.move_id
LEFT JOIN stock_picking sp ON sp.id = m.picking_id
WINDOW w_seq AS (PARTITION BY r.company_id, r.warehouse_id, r.product_id
                 ORDER BY r.ord, r.date, r.move_id),
       w_bal AS (PARTITION BY r.company_id, r.warehouse_id, r.product_id
                 ORDER BY r.ord, r.date, r.move_id
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
"""

VALUATION_UPDATE = """
UPDATE inventory_ledger_line l
   SET opening_value = l.opening_qty * c.open_cost,
       closing_value = CASE WHEN l.line_type = 'summary' THEN l.closing_qty * c.close_cost ELSE 0 END,
       unit_cost = CASE
                       WHEN l.line_type = 'summary' THEN c.close_cost
                       WHEN l.line_type = 'opening' THEN c.open_cost
                       WHEN l.in_qty_unvalued <> 0 OR l.out_qty_unvalued <> 0 THEN c.close_cost
                       ELSE l.unit_cost
                   END,
       in_value = l.in_value + l.in_qty_unvalued * c.close_cost,
       out_value = l.out_value + l.out_qty_unvalued * c.close_cost
  FROM unnest(%(company_ids)s::int[], %(product_ids)s::int[],
              %(open_costs)s::float8[], %(close_costs)s::float8[])
       AS c(company_id, product_id, open_cost, close_cost)
 WHERE l.wizard_id = %(wizard_id)s
   AND l.company_id = c.company_id
   AND l.product_id = c.product_id
"""


class InventoryLedgerEngine(models.AbstractModel):
    _name = 'inventory.ledger.engine'
    _description = 'Inventory Ledger SQL Engine'

    @api.model
    def compute(self, wizard):
        """(Re)build the ledger lines of ``wizard``. Returns the number of rows."""
        wizard.ensure_one()
        params = wizard._get_query_params()
        # The engine reads tables directly: push pending ORM writes first.
        self.env.flush_all()
        cr = self.env.cr
        cr.execute('DELETE FROM inventory_ledger_line WHERE wizard_id = %s', [wizard.id])
        body = SUMMARY_INSERT if wizard.report_type == 'summary' else DETAILED_INSERT
        cr.execute(COMMON_CTES + body, params)
        count = cr.rowcount
        if count:
            self._apply_valuation(wizard, params)
        self.env['inventory.ledger.line'].invalidate_model()
        wizard.invalidate_recordset(['line_ids'])
        return count

    @api.model
    def _apply_valuation(self, wizard, params):
        """Price the lines with Odoo 19's own valuation engine.

        ``avg_cost`` is computed by stock_account for a given ``to_date`` and
        company (standard, average and FIFO cost methods, manual value changes,
        landed costs). It is batched over all products of the run.
        """
        cr = self.env.cr
        cr.execute("""SELECT DISTINCT company_id, product_id FROM inventory_ledger_line
                       WHERE wizard_id = %s""", [wizard.id])
        products_by_company = defaultdict(list)
        for company_id, product_id in cr.fetchall():
            products_by_company[company_id].append(product_id)

        open_at = params['date_from'] - timedelta(seconds=1)
        close_at = params['date_to']
        rows = {'company_ids': [], 'product_ids': [], 'open_costs': [], 'close_costs': []}
        Product = self.env['product.product'].sudo()
        for company_id, product_ids in products_by_company.items():
            products = Product.with_company(company_id).with_context(
                allowed_company_ids=[company_id]).browse(product_ids)
            open_costs = {p.id: p.avg_cost for p in products.with_context(to_date=open_at)}
            close_costs = {p.id: p.avg_cost for p in products.with_context(to_date=close_at)}
            for product_id in product_ids:
                rows['company_ids'].append(company_id)
                rows['product_ids'].append(product_id)
                rows['open_costs'].append(open_costs.get(product_id) or 0.0)
                rows['close_costs'].append(close_costs.get(product_id) or 0.0)
        self.env.flush_all()
        cr.execute(VALUATION_UPDATE, dict(rows, wizard_id=wizard.id))
