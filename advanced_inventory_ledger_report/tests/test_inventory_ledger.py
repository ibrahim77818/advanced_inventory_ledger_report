# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase, new_test_user
from odoo.tools import float_compare


@tagged('post_install', '-at_install', 'inventory_ledger')
class TestInventoryLedger(TransactionCase):
    """Scenario (dates relative to today, UTC):

        D-10  receive 100 @ 10.00 into WH1            (supplier  -> WH1)
        D-5   deliver  30 from WH1                    (WH1       -> customer)
        D-2   transfer 40 from WH1 to WH2             (WH1       -> WH2)
        D-0   receive  20 @ 10.00 into WH1            (supplier  -> WH1)

    Period under test: D-7 .. D-0
        WH1: opening 100 | in 20 (purchase) | out 30 (sale) + 40 (internal) | closing 50
        WH2: opening   0 | in 40 (internal) | out 0                        | closing 40
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tz='UTC', tracking_disable=True))
        cls.company = cls.env.company
        cls.wh1 = cls.env['stock.warehouse'].search([('company_id', '=', cls.company.id)], limit=1)
        cls.wh2 = cls.env['stock.warehouse'].create({
            'name': 'Ledger Test WH2', 'code': 'LTW2', 'company_id': cls.company.id})
        cls.supplier_loc = cls.env.ref('stock.stock_location_suppliers')
        cls.customer_loc = cls.env.ref('stock.stock_location_customers')
        cls.categ = cls.env['product.category'].create({
            'name': 'Ledger Test Category',
            'property_cost_method': 'standard',
            'property_valuation': 'manual_periodic',
        })
        cls.product = cls.env['product.product'].create({
            'name': 'Ledger Test Product',
            'default_code': 'LTP-001',
            'type': 'consu',
            'is_storable': True,
            'categ_id': cls.categ.id,
            'standard_price': 10.0,
        })
        cls.product_idle = cls.env['product.product'].create({
            'name': 'Ledger Idle Product',
            'type': 'consu',
            'is_storable': True,
            'categ_id': cls.categ.id,
            'standard_price': 5.0,
        })

        now = fields.Datetime.now()
        cls.today = now.date()
        cls._do_move(cls.supplier_loc, cls.wh1.lot_stock_id, 100, now - timedelta(days=10))
        cls._do_move(cls.wh1.lot_stock_id, cls.customer_loc, 30, now - timedelta(days=5))
        cls._do_move(cls.wh1.lot_stock_id, cls.wh2.lot_stock_id, 40, now - timedelta(days=2))
        cls._do_move(cls.supplier_loc, cls.wh1.lot_stock_id, 20, now)

    @classmethod
    def _do_move(cls, src, dst, qty, date):
        move = cls.env['stock.move'].create({
            'name': 'Ledger test move',
            'product_id': cls.product.id,
            'product_uom_qty': qty,
            'product_uom': cls.product.uom_id.id,
            'location_id': src.id,
            'location_dest_id': dst.id,
            'company_id': cls.company.id,
        })
        move._action_confirm()
        move._action_assign()
        move.quantity = qty
        move.picked = True
        move._action_done()
        # back-date the move, its lines and its valuation layers
        move.write({'date': date})
        move.move_line_ids.write({'date': date})
        cls.env.flush_all()
        cls.env.cr.execute(
            'UPDATE stock_valuation_layer SET create_date = %s WHERE stock_move_id = %s',
            [date, move.id])
        cls.env['stock.valuation.layer'].invalidate_model(['create_date'])
        return move

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _run(self, report_type='summary', days_back=7, date_to=None, **extra):
        vals = {
            'report_type': report_type,
            'date_from': self.today - timedelta(days=days_back),
            'date_to': date_to or self.today,
            'company_ids': [(6, 0, self.company.ids)],
            'product_ids': [(6, 0, self.product.ids)],
        }
        vals.update(extra)
        wizard = self.env['inventory.ledger.wizard'].create(vals)
        wizard._compute_ledger()
        return wizard

    def _line(self, wizard, warehouse):
        lines = wizard.line_ids.filtered(lambda l: l.warehouse_id == warehouse)
        self.assertEqual(len(lines), 1, 'exactly one summary line per warehouse expected')
        return lines

    def assertQty(self, first, second, msg=None):
        self.assertEqual(float_compare(first, second, precision_digits=4), 0,
                         msg or '%s != %s' % (first, second))

    # ------------------------------------------------------------------
    # Summary figures
    # ------------------------------------------------------------------
    def test_01_summary_buckets(self):
        for method in ('quants', 'moves'):
            with self.subTest(opening_method=method):
                wizard = self._run(opening_method=method)
                wh1 = self._line(wizard, self.wh1)
                self.assertQty(wh1.opening_qty, 100)
                self.assertQty(wh1.in_purchase_qty, 20)
                self.assertQty(wh1.in_qty, 20)
                self.assertQty(wh1.out_sale_qty, 30)
                self.assertQty(wh1.out_internal_qty, 40)
                self.assertQty(wh1.out_qty, 70)
                self.assertQty(wh1.closing_qty, 50)
                wh2 = self._line(wizard, self.wh2)
                self.assertQty(wh2.opening_qty, 0)
                self.assertQty(wh2.in_internal_qty, 40)
                self.assertQty(wh2.out_qty, 0)
                self.assertQty(wh2.closing_qty, 40)

    def test_02_closing_matches_on_hand(self):
        """Closing per warehouse == product.qty_available in that warehouse."""
        wizard = self._run()
        for warehouse in (self.wh1, self.wh2):
            on_hand = self.product.with_context(warehouse_id=warehouse.id).qty_available
            self.assertQty(self._line(wizard, warehouse).closing_qty, on_hand)
        self.assertQty(sum(wizard.line_ids.mapped('closing_qty')), self.product.qty_available)

    def test_03_closing_value_matches_valuation(self):
        """Company-wide closing value == live stock valuation (sum of SVL)."""
        wizard = self._run()
        product = self.product.with_company(self.company)
        self.assertQty(sum(wizard.line_ids.mapped('closing_value')), product.value_svl)
        self.assertQty(product.value_svl, 900.0)
        self.assertQty(self._line(wizard, self.wh1).unit_cost, 10.0)

    def test_04_historical_period_matches_to_date(self):
        """A period ending in the past matches qty_available(to_date=...)."""
        wizard = self._run(days_back=20, date_to=self.today - timedelta(days=6))
        _start, end = wizard._get_utc_bounds()
        wh1 = self._line(wizard, self.wh1)
        expected = self.product.with_context(warehouse_id=self.wh1.id, to_date=end).qty_available
        self.assertQty(wh1.opening_qty, 0)
        self.assertQty(wh1.in_purchase_qty, 100)
        self.assertQty(wh1.closing_qty, expected)
        self.assertQty(wh1.closing_qty, 100)

    def test_05_opening_methods_agree(self):
        """Fast (quant roll-back) and audit (full history) give identical figures."""
        fields_to_compare = ('opening_qty', 'in_qty', 'out_qty', 'closing_qty', 'closing_value')
        for days_back, date_to in ((7, None), (3, self.today - timedelta(days=1)), (30, None)):
            fast = self._run(days_back=days_back, date_to=date_to, opening_method='quants')
            audit = self._run(days_back=days_back, date_to=date_to, opening_method='moves')
            key = lambda l: (l.warehouse_id.id, l.product_id.id)
            fast_rows = {key(l): l for l in fast.line_ids}
            audit_rows = {key(l): l for l in audit.line_ids}
            self.assertEqual(set(fast_rows), set(audit_rows))
            for k, line in fast_rows.items():
                for fname in fields_to_compare:
                    self.assertQty(line[fname], audit_rows[k][fname],
                                   '%s differs for %s (period -%s days)' % (fname, k, days_back))

    def test_06_warehouse_filter(self):
        wizard = self._run(warehouse_ids=[(6, 0, self.wh2.ids)])
        self.assertEqual(wizard.line_ids.warehouse_id, self.wh2)
        self.assertQty(wizard.line_ids.in_internal_qty, 40)

    def test_07_hide_zero(self):
        wizard = self._run(product_ids=[(6, 0, (self.product | self.product_idle).ids)])
        self.assertNotIn(self.product_idle, wizard.line_ids.product_id)
        wizard = self._run(product_ids=[(6, 0, (self.product | self.product_idle).ids)],
                           categ_ids=[(6, 0, self.categ.ids)], hide_zero=False)
        self.assertIn(self.product, wizard.line_ids.product_id)

    def test_08_category_filter_includes_children(self):
        child = self.env['product.category'].create({'name': 'Child', 'parent_id': self.categ.id})
        self.product.categ_id = child
        wizard = self._run(product_ids=[(5, 0, 0)], categ_ids=[(6, 0, self.categ.ids)])
        self.assertIn(self.product, wizard.line_ids.product_id)

    def test_09_over_receipt_counts_done_quantity(self):
        """Odoo 17 keeps the done quantity in `quantity`; receiving more than the
        demand must still be counted in full by the ledger."""
        move = self.env['stock.move'].create({
            'name': 'Over receipt',
            'product_id': self.product.id,
            'product_uom_qty': 10,
            'product_uom': self.product.uom_id.id,
            'location_id': self.supplier_loc.id,
            'location_dest_id': self.wh2.lot_stock_id.id,
            'company_id': self.company.id,
        })
        move._action_confirm()
        move._action_assign()
        move.quantity = 12
        move.picked = True
        move._action_done()
        wizard = self._run(days_back=1, warehouse_ids=[(6, 0, self.wh2.ids)])
        line = self._line(wizard, self.wh2)
        self.assertQty(line.in_purchase_qty, 12)
        self.assertQty(line.closing_qty, self.product.with_context(warehouse_id=self.wh2.id).qty_available)

    def test_09b_receipt_in_other_uom(self):
        """A receipt in Dozens of a product stocked in Units is counted in Units."""
        dozen = self.env.ref('uom.product_uom_dozen')
        move = self.env['stock.move'].create({
            'name': 'Receipt in dozens',
            'product_id': self.product.id,
            'product_uom_qty': 2,
            'product_uom': dozen.id,
            'location_id': self.supplier_loc.id,
            'location_dest_id': self.wh2.lot_stock_id.id,
            'company_id': self.company.id,
        })
        move._action_confirm()
        move._action_assign()
        move.quantity = 2
        move.picked = True
        move._action_done()
        wizard = self._run(days_back=1, warehouse_ids=[(6, 0, self.wh2.ids)])
        line = self._line(wizard, self.wh2)
        self.assertQty(line.in_purchase_qty, 24)
        self.assertQty(line.closing_qty, self.product.with_context(warehouse_id=self.wh2.id).qty_available)

    # ------------------------------------------------------------------
    # Detailed ledger
    # ------------------------------------------------------------------
    def test_10_detailed_running_balance(self):
        wizard = self._run('detailed', warehouse_ids=[(6, 0, self.wh1.ids)])
        lines = wizard.line_ids.sorted('sequence')
        self.assertEqual(lines.mapped('line_type'), ['opening', 'move', 'move', 'move'])
        self.assertEqual(lines.mapped('movement_type'),
                         ['opening', 'out_sale', 'out_internal', 'in_purchase'])
        self.assertEqual([round(b, 4) for b in lines.mapped('balance_qty')], [100, 70, 30, 50])
        summary = self._run('summary', warehouse_ids=[(6, 0, self.wh1.ids)])
        self.assertQty(lines[-1].balance_qty, summary.line_ids.closing_qty)
        self.assertTrue(all(lines[1:].mapped('move_id')))
        self.assertQty(lines[1].out_value, 300.0)

    def test_11_report_groups(self):
        wizard = self._run('detailed')
        groups = wizard.get_report_groups()
        self.assertEqual(len(groups), 2)
        by_wh = {g['warehouse']: g for g in groups}
        self.assertQty(by_wh[self.wh1]['totals']['closing_qty'], 50)
        self.assertQty(by_wh[self.wh2]['totals']['closing_qty'], 40)

    # ------------------------------------------------------------------
    # Outputs
    # ------------------------------------------------------------------
    def test_20_xlsx_export(self):
        for report_type in ('summary', 'detailed'):
            wizard = self._run(report_type)
            content, filename = wizard._generate_xlsx()
            self.assertTrue(content.startswith(b'PK'), 'not a valid xlsx (zip) payload')
            self.assertTrue(filename.endswith('.xlsx'))
            action = wizard.action_export_xlsx()
            self.assertEqual(action['type'], 'ir.actions.act_url')

    def test_21_qweb_report_renders(self):
        report = self.env.ref('advanced_inventory_ledger_report.action_report_inventory_ledger')
        for report_type in ('summary', 'detailed'):
            wizard = self._run(report_type)
            html, _fmt = self.env['ir.actions.report']._render_qweb_html(report, wizard.ids)
            self.assertIn(b'Ledger Test Product', html)
            self.assertIn(b'Inventory Ledger', html)

    def test_22_browser_action(self):
        wizard = self._run()
        action = wizard.action_view_report()
        self.assertEqual(action['res_model'], 'inventory.ledger.line')
        self.assertEqual(action['domain'], [('wizard_id', '=', wizard.id)])
        line = wizard.line_ids[:1]
        drill = line.action_open_moves()
        self.assertEqual(drill['res_model'], 'stock.move.line')
        self.assertTrue(self.env['stock.move.line'].search(drill['domain']))

    def test_23_rtl_follows_report_language(self):
        """Direction comes from the language the report is rendered in, not the user's default."""
        self.env['res.lang']._activate_lang('ar_001')
        self.env['res.lang']._activate_lang('en_US')
        wizard = self._run()
        self.assertFalse(wizard.with_context(lang='en_US').get_filter_summary()['rtl'])
        self.assertTrue(wizard.with_context(lang='ar_001').get_filter_summary()['rtl'])

    # ------------------------------------------------------------------
    # Security
    # ------------------------------------------------------------------
    def test_30_company_access_enforced(self):
        other = self.env['res.company'].create({'name': 'Ledger Other Co'})
        user = new_test_user(self.env, login='ledger_stock_user', groups='stock.group_stock_user',
                             company_id=self.company.id, company_ids=[(6, 0, self.company.ids)])
        wizard = self.env['inventory.ledger.wizard'].with_user(user).create({
            'date_from': self.today - timedelta(days=7),
            'date_to': self.today,
            'company_ids': [(6, 0, (self.company | other).ids)],
        })
        with self.assertRaises(AccessError):
            wizard._compute_ledger()

    def test_31_user_sees_only_own_runs(self):
        user = new_test_user(self.env, login='ledger_user_b', groups='stock.group_stock_user')
        mine = self._run()
        self.assertFalse(self.env['inventory.ledger.line'].with_user(user).search(
            [('wizard_id', '=', mine.id)]))

    def test_32_date_constraint(self):
        with self.assertRaises(ValidationError):
            self.env['inventory.ledger.wizard'].create({
                'date_from': self.today, 'date_to': self.today - timedelta(days=1)})
