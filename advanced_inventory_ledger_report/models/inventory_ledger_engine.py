# -*- coding: utf-8 -*-
"""Set-based inventory ledger engine.

Design
------
Every report run is ONE SQL statement (``WITH ... INSERT INTO ... SELECT``)
executed server-side in PostgreSQL. No product loop, no ORM recordset
iteration, no per-move Python work - so cost grows with the number of
*moves in scope*, not with products x warehouses x days.

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

Opening stock strategies
------------------------
``moves``   opening = SUM(legs before date_from). Reads the whole move
            history up to date_to. Audit-exact, independent of quants.
``quants``  opening = current quants - SUM(legs since date_from). Reads
            only moves from date_from onwards - typically 10-100x fewer
            rows for recent periods. Relies on quants being consistent
            with moves (true in a healthy database).

Valuation
---------
Values come from ``stock_valuation_layer`` (company level, by date):
* move value  = qty x the move's own layer unit cost (falls back to the
  average cost at date_to for unvalued moves, e.g. internal transfers)
* opening / closing value = qty x average unit cost (SUM(value)/SUM(qty)
  of layers) at date_from / date_to.
Run company-wide, closing value reconciles to the Stock Valuation report.
"""
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
     WHERE pt.type = 'product'
       AND (%(product_ids)s::int[] IS NULL OR pp.id = ANY(%(product_ids)s::int[]))
       AND (%(categ_ids)s::int[] IS NULL OR pt.categ_id = ANY(%(categ_ids)s::int[]))
),
legs AS (
    -- IN legs: destination inside the warehouse, source outside it
    SELECT m.id AS move_id, m.date, m.product_id, d.warehouse_id, d.company_id,
           1 AS direction, m.product_qty AS qty, src.usage AS other_usage
      FROM stock_move m
      JOIN prod ON prod.product_id = m.product_id
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
           -1, m.product_qty, dst.usage
      FROM stock_move m
      JOIN prod ON prod.product_id = m.product_id
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
cost_open AS (
    SELECT svl.company_id, svl.product_id,
           SUM(svl.value) / NULLIF(SUM(svl.quantity), 0) AS unit_cost
      FROM stock_valuation_layer svl
      JOIN prod ON prod.product_id = svl.product_id
     WHERE svl.company_id = ANY(%(company_ids)s)
       AND svl.create_date < %(date_from)s
  GROUP BY svl.company_id, svl.product_id
),
cost_close AS (
    SELECT svl.company_id, svl.product_id,
           SUM(svl.value) / NULLIF(SUM(svl.quantity), 0) AS unit_cost
      FROM stock_valuation_layer svl
      JOIN prod ON prod.product_id = svl.product_id
     WHERE svl.company_id = ANY(%(company_ids)s)
       AND svl.create_date <= %(date_to)s
  GROUP BY svl.company_id, svl.product_id
),
move_cost AS (
    SELECT svl.stock_move_id,
           SUM(svl.value) / NULLIF(SUM(svl.quantity), 0) AS unit_cost
      FROM stock_valuation_layer svl
     WHERE svl.stock_move_id IN (
               SELECT move_id FROM classified WHERE bucket NOT IN ('opening', 'future'))
  GROUP BY svl.stock_move_id
),
period_moves AS (
    SELECT c.*, COALESCE(ABS(mc.unit_cost), cc.unit_cost, 0) AS unit_cost
      FROM classified c
 LEFT JOIN move_cost mc ON mc.stock_move_id = c.move_id
 LEFT JOIN cost_close cc ON cc.company_id = c.company_id AND cc.product_id = c.product_id
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
    create_uid, create_date, write_uid, write_date)
SELECT %(wizard_id)s, 'summary', 'summary',
       a.company_id, a.warehouse_id, a.product_id, prod.categ_id, prod.uom_id,
       a.opening_qty, a.in_purchase_qty, a.in_return_qty, a.in_internal_qty, a.in_other_qty, a.in_qty,
       a.out_sale_qty, a.out_return_qty, a.out_internal_qty, a.out_other_qty, a.out_qty,
       a.opening_qty + a.in_qty - a.out_qty,
       COALESCE(cc.unit_cost, 0),
       a.opening_qty * COALESCE(co.unit_cost, 0),
       a.in_value, a.out_value,
       (a.opening_qty + a.in_qty - a.out_qty) * COALESCE(cc.unit_cost, 0),
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
               COALESCE(p.out_value, 0) AS out_value
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
                       SUM(CASE WHEN direction = 1  THEN qty * unit_cost ELSE 0 END) AS in_value,
                       SUM(CASE WHEN direction = -1 THEN qty * unit_cost ELSE 0 END) AS out_value
                  FROM period_moves
              GROUP BY company_id, warehouse_id, product_id
               ) p
            ON p.company_id = o.company_id
           AND p.warehouse_id = o.warehouse_id
           AND p.product_id = o.product_id
       ) a
  JOIN prod ON prod.product_id = a.product_id
LEFT JOIN cost_open co ON co.company_id = a.company_id AND co.product_id = a.product_id
LEFT JOIN cost_close cc ON cc.company_id = a.company_id AND cc.product_id = a.product_id
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
    create_uid, create_date, write_uid, write_date)
SELECT %(wizard_id)s, 'detailed', r.line_type,
       ROW_NUMBER() OVER w_seq,
       r.company_id, r.warehouse_id, r.product_id, prod.categ_id, prod.uom_id,
       r.date, r.move_id, m.picking_id,
       CASE WHEN r.move_id IS NULL THEN NULL ELSE COALESCE(m.reference, m.name) END,
       COALESCE(sp.partner_id, m.partner_id),
       r.movement_type,
       r.opening_qty, r.in_qty, r.out_qty,
       SUM(r.opening_qty + r.in_qty - r.out_qty) OVER w_bal,
       CASE WHEN r.move_id IS NULL THEN COALESCE(co.unit_cost, 0) ELSE r.unit_cost END,
       r.opening_qty * COALESCE(co.unit_cost, 0),
       r.in_qty * r.unit_cost,
       r.out_qty * r.unit_cost,
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
LEFT JOIN cost_open co ON co.company_id = r.company_id AND co.product_id = r.product_id
WINDOW w_seq AS (PARTITION BY r.company_id, r.warehouse_id, r.product_id
                 ORDER BY r.ord, r.date, r.move_id),
       w_bal AS (PARTITION BY r.company_id, r.warehouse_id, r.product_id
                 ORDER BY r.ord, r.date, r.move_id
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
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
        self.env['inventory.ledger.line'].invalidate_model()
        wizard.invalidate_recordset(['line_ids'])
        return count
