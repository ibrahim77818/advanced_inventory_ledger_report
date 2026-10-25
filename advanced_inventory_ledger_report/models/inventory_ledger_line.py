# -*- coding: utf-8 -*-
from odoo import _, api, fields, models

MOVEMENT_TYPES = [
    ('opening', 'Opening Balance'),
    ('in_purchase', 'Purchases / Receipts'),
    ('in_return', 'Customer Returns'),
    ('in_internal', 'Internal IN'),
    ('in_other', 'Adjustment / Production IN'),
    ('out_sale', 'Sales / Deliveries'),
    ('out_return', 'Vendor Returns'),
    ('out_internal', 'Internal OUT'),
    ('out_other', 'Adjustment / Production OUT'),
]

QTY_DIGITS = 'Product Unit of Measure'
VALUE_DIGITS = 'Product Price'


class InventoryLedgerLine(models.TransientModel):
    """Result rows of one ledger run.

    Rows are bulk-inserted by ``inventory.ledger.engine`` with a single
    INSERT ... SELECT statement; the ORM is only used to read them back in
    list / pivot / graph views, the XLSX exporter and the QWeb report.
    The model is transient: rows disappear with their wizard (cascade) and
    are garbage-collected by the standard transient vacuum.
    """
    _name = 'inventory.ledger.line'
    _description = 'Inventory Ledger Line'
    _order = 'company_id, warehouse_id, product_id, sequence, id'
    _rec_name = 'product_id'
    _transient_max_hours = 24.0

    wizard_id = fields.Many2one(
        'inventory.ledger.wizard', required=True, ondelete='cascade', index=True)
    report_type = fields.Selection(
        [('summary', 'Summary'), ('detailed', 'Detailed')], readonly=True)
    line_type = fields.Selection(
        [('summary', 'Summary'), ('opening', 'Opening Balance'), ('move', 'Movement')],
        readonly=True)
    sequence = fields.Integer(readonly=True)

    company_id = fields.Many2one('res.company', readonly=True, index=True)
    warehouse_id = fields.Many2one('stock.warehouse', readonly=True)
    product_id = fields.Many2one('product.product', readonly=True)
    default_code = fields.Char(related='product_id.default_code', string='Internal Reference')
    categ_id = fields.Many2one('product.category', string='Product Category', readonly=True)
    uom_id = fields.Many2one('uom.uom', string='UoM', readonly=True)

    # ---- detailed-mode columns -------------------------------------------
    date = fields.Datetime(readonly=True)
    move_id = fields.Many2one('stock.move', string='Stock Move', readonly=True)
    picking_id = fields.Many2one('stock.picking', string='Transfer', readonly=True)
    reference = fields.Char(readonly=True)
    partner_id = fields.Many2one('res.partner', string='Partner', readonly=True)
    movement_type = fields.Selection(MOVEMENT_TYPES, readonly=True)
    balance_qty = fields.Float(
        'Running Balance', digits=QTY_DIGITS, readonly=True, aggregator=False)

    # ---- quantities ------------------------------------------------------
    opening_qty = fields.Float('Opening Qty', digits=QTY_DIGITS, readonly=True)
    in_purchase_qty = fields.Float('Purchases / Receipts', digits=QTY_DIGITS, readonly=True)
    in_return_qty = fields.Float('Customer Returns', digits=QTY_DIGITS, readonly=True)
    in_internal_qty = fields.Float('Internal IN', digits=QTY_DIGITS, readonly=True)
    in_other_qty = fields.Float('Adjust. / Prod. IN', digits=QTY_DIGITS, readonly=True)
    in_qty = fields.Float('Total IN', digits=QTY_DIGITS, readonly=True)
    out_sale_qty = fields.Float('Sales / Deliveries', digits=QTY_DIGITS, readonly=True)
    out_return_qty = fields.Float('Vendor Returns', digits=QTY_DIGITS, readonly=True)
    out_internal_qty = fields.Float('Internal OUT', digits=QTY_DIGITS, readonly=True)
    out_other_qty = fields.Float('Adjust. / Prod. OUT', digits=QTY_DIGITS, readonly=True)
    out_qty = fields.Float('Total OUT', digits=QTY_DIGITS, readonly=True)
    closing_qty = fields.Float('Closing Qty', digits=QTY_DIGITS, readonly=True)

    # ---- valuation -------------------------------------------------------
    unit_cost = fields.Float('Unit Cost', digits=VALUE_DIGITS, readonly=True, aggregator='avg')
    opening_value = fields.Float('Opening Value', digits=VALUE_DIGITS, readonly=True)
    in_value = fields.Float('IN Value', digits=VALUE_DIGITS, readonly=True)
    out_value = fields.Float('OUT Value', digits=VALUE_DIGITS, readonly=True)
    closing_value = fields.Float('Closing Value', digits=VALUE_DIGITS, readonly=True)
    # technical: quantities of moves Odoo does not value (e.g. inter-warehouse
    # transfers); priced at the closing average cost by the engine
    in_qty_unvalued = fields.Float(digits=QTY_DIGITS, readonly=True)
    out_qty_unvalued = fields.Float(digits=QTY_DIGITS, readonly=True)

    def init(self):
        """Partial index that makes the ledger CTEs index-driven.

        Odoo 19 has no stock_valuation_layer table any more; valuation reads
        stock_move directly, so the move index covers both steps.
        """
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS stock_move_ledger_done_product_date_idx
                ON stock_move (product_id, date)
             WHERE state = 'done'
        """)

    # ---------------------------------------------------------------------
    # Drill-down
    # ---------------------------------------------------------------------
    def action_open_moves(self):
        """Open the source documents behind a ledger row."""
        self.ensure_one()
        if self.move_id:
            if self.picking_id:
                return {
                    'type': 'ir.actions.act_window',
                    'res_model': 'stock.picking',
                    'res_id': self.picking_id.id,
                    'view_mode': 'form',
                    'target': 'current',
                }
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'stock.move',
                'res_id': self.move_id.id,
                'view_mode': 'form',
                'target': 'current',
            }

        dt_from, dt_to = self.wizard_id._get_utc_bounds()
        view_location = self.warehouse_id.view_location_id
        if self.line_type == 'opening':
            date_domain = [('date', '<', fields.Datetime.to_string(dt_from))]
        else:
            date_domain = [
                ('date', '>=', fields.Datetime.to_string(dt_from)),
                ('date', '<=', fields.Datetime.to_string(dt_to)),
            ]
        domain = [
            ('product_id', '=', self.product_id.id),
            ('state', '=', 'done'),
            '|',
            ('location_id', 'child_of', view_location.id),
            ('location_dest_id', 'child_of', view_location.id),
        ] + date_domain
        action = self.env['ir.actions.act_window']._for_xml_id('stock.stock_move_line_action')
        action.update({
            'name': _('Moves: %(product)s @ %(warehouse)s',
                      product=self.product_id.display_name,
                      warehouse=self.warehouse_id.name),
            'domain': domain,
            'context': {'create': False},
            'target': 'current',
        })
        return action

    @api.model
    def movement_type_labels(self):
        return dict(self._fields['movement_type']._description_selection(self.env))
