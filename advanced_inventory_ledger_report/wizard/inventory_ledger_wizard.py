# -*- coding: utf-8 -*-
from datetime import datetime, time

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.tools import groupby

SUMMARY_TOTAL_FIELDS = (
    'opening_qty', 'in_purchase_qty', 'in_return_qty', 'in_internal_qty', 'in_other_qty',
    'in_qty', 'out_sale_qty', 'out_return_qty', 'out_internal_qty', 'out_other_qty',
    'out_qty', 'closing_qty', 'opening_value', 'in_value', 'out_value', 'closing_value',
)


class InventoryLedgerWizard(models.TransientModel):
    _name = 'inventory.ledger.wizard'
    _description = 'Inventory Ledger Report Wizard'
    _transient_max_hours = 24.0

    def _default_date_from(self):
        return fields.Date.context_today(self).replace(day=1)

    report_type = fields.Selection(
        [('summary', 'Summary'), ('detailed', 'Detailed')],
        string='Report Type', required=True, default='summary',
        help='Summary: one line per product and warehouse.\n'
             'Detailed: every stock movement with a running balance.')
    date_from = fields.Date('From', required=True, default=_default_date_from)
    date_to = fields.Date('To', required=True, default=fields.Date.context_today)
    opening_method = fields.Selection(
        [('quants', 'Fast (roll back from current stock)'),
         ('moves', 'Audit (full move history)')],
        string='Opening Stock Method', required=True, default='quants',
        help='Fast: opening = stock on hand today minus everything that moved since the '
             'start date. Only recent moves are read.\n'
             'Audit: opening = sum of every done move before the start date. Independent '
             'of quants; use it to cross-check data integrity.')
    company_ids = fields.Many2many(
        'res.company', 'inv_ledger_wiz_company_rel', 'wizard_id', 'company_id',
        string='Companies', required=True, default=lambda self: self.env.companies)
    warehouse_ids = fields.Many2many(
        'stock.warehouse', 'inv_ledger_wiz_warehouse_rel', 'wizard_id', 'warehouse_id',
        string='Warehouses', domain="[('company_id', 'in', company_ids)]",
        help='Leave empty for all warehouses of the selected companies.')
    categ_ids = fields.Many2many(
        'product.category', 'inv_ledger_wiz_categ_rel', 'wizard_id', 'categ_id',
        string='Product Categories', help='Sub-categories are included.')
    product_ids = fields.Many2many(
        'product.product', 'inv_ledger_wiz_product_rel', 'wizard_id', 'product_id',
        string='Products', domain="[('type', '=', 'product')]")
    hide_zero = fields.Boolean(
        'Hide Zero Lines', default=True,
        help='Skip products with no opening stock and no movement in the period.')
    line_ids = fields.One2many('inventory.ledger.line', 'wizard_id', string='Lines')

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for wizard in self:
            if wizard.date_from > wizard.date_to:
                raise ValidationError(_('The start date must be before the end date.'))

    @api.onchange('company_ids')
    def _onchange_company_ids(self):
        self.warehouse_ids = self.warehouse_ids.filtered(
            lambda w: w.company_id in self.company_ids)

    # ---------------------------------------------------------------------
    # Query preparation
    # ---------------------------------------------------------------------
    def _get_tz(self):
        return pytz.timezone(self.env.context.get('tz') or self.env.user.tz or 'UTC')

    def _get_utc_bounds(self):
        """Period as naive UTC datetimes covering whole days in the user's timezone."""
        self.ensure_one()
        tz = self._get_tz()
        start = tz.localize(datetime.combine(self.date_from, time.min))
        end = tz.localize(datetime.combine(self.date_to, time.max))
        return (start.astimezone(pytz.utc).replace(tzinfo=None),
                end.astimezone(pytz.utc).replace(tzinfo=None, microsecond=0))

    def _get_allowed_companies(self):
        """The engine bypasses record rules, so company access is enforced here."""
        self.ensure_one()
        requested = self.sudo().company_ids
        if not requested:
            raise ValidationError(_('Select at least one company.'))
        forbidden = requested - self.env.user.company_ids
        if forbidden and not self.env.su:
            raise AccessError(_('You are not allowed to report on: %s',
                                ', '.join(forbidden.mapped('name'))))
        return requested

    def _get_query_params(self):
        self.ensure_one()
        companies = self._get_allowed_companies()
        dt_from, dt_to = self._get_utc_bounds()
        warehouses = self.sudo().warehouse_ids.filtered(lambda w: w.company_id in companies)
        categ_ids = None
        if self.categ_ids:
            categ_ids = self.env['product.category'].with_context(active_test=False).search(
                [('id', 'child_of', self.categ_ids.ids)]).ids
        return {
            'wizard_id': self.id,
            'uid': self.env.uid,
            'company_ids': companies.ids,
            'warehouse_ids': warehouses.ids or None,
            'product_ids': self.product_ids.ids or None,
            'categ_ids': categ_ids or None,
            'date_from': dt_from,
            'date_to': dt_to,
            'use_quants': self.opening_method == 'quants',
            'hide_zero': self.hide_zero,
            'epsilon': 1e-7,
        }

    def _compute_ledger(self):
        self.ensure_one()
        return self.env['inventory.ledger.engine'].compute(self)

    # ---------------------------------------------------------------------
    # Output actions
    # ---------------------------------------------------------------------
    def action_view_report(self):
        self.ensure_one()
        self._compute_ledger()
        suffix = 'summary' if self.report_type == 'summary' else 'detailed'
        ref = 'advanced_inventory_ledger_report.view_inventory_ledger_line_%s_%s'
        return {
            'type': 'ir.actions.act_window',
            'name': _('Inventory Ledger %(type)s: %(start)s - %(end)s',
                      type=dict(self._fields['report_type']._description_selection(self.env))[self.report_type],
                      start=self.date_from, end=self.date_to),
            'res_model': 'inventory.ledger.line',
            'view_mode': 'tree,pivot,graph',
            'views': [
                (self.env.ref(ref % (suffix, 'tree')).id, 'tree'),
                (self.env.ref(ref % (suffix, 'pivot')).id, 'pivot'),
                (self.env.ref(ref % (suffix, 'graph')).id, 'graph'),
            ],
            'search_view_id': [self.env.ref(
                'advanced_inventory_ledger_report.view_inventory_ledger_line_search').id],
            'domain': [('wizard_id', '=', self.id)],
            'context': {'create': False, 'edit': False, 'delete': False},
            'target': 'current',
        }

    def action_export_xlsx(self):
        self.ensure_one()
        self._compute_ledger()
        return {
            'type': 'ir.actions.act_url',
            'url': '/advanced_inventory_ledger_report/xlsx/%d' % self.id,
            'target': 'self',
        }

    def action_print_pdf(self):
        self.ensure_one()
        self._compute_ledger()
        return self.env.ref(
            'advanced_inventory_ledger_report.action_report_inventory_ledger'
        ).report_action(self)

    # ---------------------------------------------------------------------
    # Helpers shared by the PDF and XLSX renderers
    # ---------------------------------------------------------------------
    def _get_lines(self):
        self.ensure_one()
        return self.env['inventory.ledger.line'].search([('wizard_id', '=', self.id)])

    def get_filter_summary(self):
        """Human-readable filter description for report headers."""
        self.ensure_one()
        lang = self.env['res.lang']._lang_get(self.env.lang or self.env.user.lang or 'en_US')
        date_fmt = lang.date_format if lang else '%Y-%m-%d'
        all_label = _('All')
        categories = self.categ_ids.mapped('display_name')
        products = self.product_ids.mapped('display_name')
        if len(products) > 8:
            products = products[:8] + [_('... (+%s more)', len(self.product_ids) - 8)]
        return {
            'report_type': dict(self._fields['report_type']._description_selection(self.env))[self.report_type],
            'period': '%s  →  %s' % (self.date_from.strftime(date_fmt), self.date_to.strftime(date_fmt)),
            'date_from': self.date_from.strftime(date_fmt),
            'date_to': self.date_to.strftime(date_fmt),
            'rtl': bool(lang) and lang.direction == 'rtl',
            'companies': ', '.join(self.company_ids.mapped('name')) or all_label,
            'warehouses': ', '.join(self.warehouse_ids.mapped('name')) or all_label,
            'categories': ', '.join(categories) or all_label,
            'products': ', '.join(products) or all_label,
            'opening_method': dict(self._fields['opening_method']._description_selection(self.env))[self.opening_method],
            'printed_on': fields.Datetime.context_timestamp(self, fields.Datetime.now()).strftime(
                '%s %s' % (date_fmt, lang.time_format if lang else '%H:%M:%S')),
            'qty_precision': self.env['decimal.precision'].precision_get('Product Unit of Measure'),
            'value_precision': self.env['decimal.precision'].precision_get('Product Price'),
        }

    @api.model
    def _sum(self, lines, field_names):
        return {name: sum(lines.mapped(name)) for name in field_names}

    def get_report_groups(self):
        """Lines grouped for rendering.

        Summary  -> one group per (company, warehouse) with subtotals.
        Detailed -> one group per (company, warehouse, product): opening row,
                    movements and closing figures.
        """
        self.ensure_one()
        Line = self.env['inventory.ledger.line']
        lines = self._get_lines()
        groups = []
        if self.report_type == 'summary':
            for (company, warehouse), recs in groupby(lines, key=lambda l: (l.company_id, l.warehouse_id)):
                recs = Line.concat(*recs)
                groups.append({
                    'company': company,
                    'warehouse': warehouse,
                    'title': warehouse.display_name if len(self.company_ids) == 1
                    else '%s — %s' % (company.name, warehouse.display_name),
                    'lines': recs,
                    'totals': self._sum(recs, SUMMARY_TOTAL_FIELDS),
                })
        else:
            key = lambda l: (l.company_id, l.warehouse_id, l.product_id)
            for (company, warehouse, product), recs in groupby(lines, key=key):
                recs = Line.concat(*recs)
                opening = recs.filtered(lambda l: l.line_type == 'opening')[:1]
                moves = recs - opening
                last = recs[-1]
                closing_qty = last.balance_qty
                groups.append({
                    'company': company,
                    'warehouse': warehouse,
                    'product': product,
                    'title': '%s  ·  %s' % (warehouse.display_name, product.display_name),
                    'uom': product.uom_id.name,
                    'opening': opening,
                    'lines': moves,
                    'totals': {
                        'opening_qty': opening.opening_qty,
                        'opening_value': opening.opening_value,
                        'in_qty': sum(moves.mapped('in_qty')),
                        'out_qty': sum(moves.mapped('out_qty')),
                        'in_value': sum(moves.mapped('in_value')),
                        'out_value': sum(moves.mapped('out_value')),
                        'closing_qty': closing_qty,
                    },
                })
        return groups

    def get_report_totals(self):
        self.ensure_one()
        lines = self._get_lines()
        if self.report_type == 'summary':
            return self._sum(lines, SUMMARY_TOTAL_FIELDS)
        return self._sum(lines, ('opening_value', 'in_value', 'out_value'))

    def _generate_xlsx(self):
        self.ensure_one()
        return self.env['inventory.ledger.xlsx'].generate(self)
