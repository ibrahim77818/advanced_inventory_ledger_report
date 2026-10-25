# -*- coding: utf-8 -*-
import base64
import io
import logging

from odoo import _, fields, models
import xlsxwriter  # Odoo 19 patches xlsxwriter globally; odoo.tools.misc no longer exports it

_logger = logging.getLogger(__name__)

try:
    from xlsxwriter.utility import xl_rowcol_to_cell
except ImportError:  # pragma: no cover - xlsxwriter ships with Odoo 16
    xl_rowcol_to_cell = None

BRAND = '#714B67'       # Odoo plum
HEADER = '#2F3E4E'      # slate
BAND_IN = '#E3F1E8'
BAND_OUT = '#FBE7E6'
BAND_VALUE = '#E8EEF8'
ZEBRA = '#F7F7FA'
GROUP_BG = '#EFE8EE'


class InventoryLedgerXlsx(models.AbstractModel):
    _name = 'inventory.ledger.xlsx'
    _description = 'Inventory Ledger Excel Generator'

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def generate(self, wizard):
        """Return (xlsx_bytes, filename) for ``wizard``'s computed lines."""
        wizard.ensure_one()
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True, 'strings_to_formulas': False})
        styles = self._get_styles(workbook, wizard)
        sheet = workbook.add_worksheet(_('Inventory Ledger'))
        filters = wizard.get_filter_summary()
        if filters['rtl']:
            sheet.right_to_left()

        if wizard.report_type == 'summary':
            columns = self._summary_columns(wizard)
            row = self._write_header(workbook, sheet, styles, wizard, filters, len(columns))
            self._write_summary(sheet, styles, wizard, columns, row)
        else:
            columns = self._detailed_columns()
            row = self._write_header(workbook, sheet, styles, wizard, filters, len(columns))
            self._write_detailed(sheet, styles, wizard, columns, row)

        sheet.set_landscape()
        sheet.set_paper(9)  # A4
        sheet.fit_to_pages(1, 0)
        sheet.set_margins(left=0.3, right=0.3, top=0.5, bottom=0.5)
        sheet.set_footer('&L%s&C&P / &N&R&D' % (wizard.company_ids[:1].name or '').replace('&', '&&'))
        workbook.close()
        filename = 'Inventory_Ledger_%s_%s_%s.xlsx' % (
            wizard.report_type, wizard.date_from, wizard.date_to)
        return output.getvalue(), filename

    # ------------------------------------------------------------------
    # Styles
    # ------------------------------------------------------------------
    def _money_format(self, wizard):
        currency = wizard.company_ids[:1].currency_id or self.env.company.currency_id
        symbol = (currency.symbol or '').replace('"', '')
        if currency.position == 'before':
            return '"%s" #,##0.00;-"%s" #,##0.00;"-"' % (symbol, symbol)
        return '#,##0.00 "%s";-#,##0.00 "%s";"-"' % (symbol, symbol)

    def _get_styles(self, workbook, wizard):
        qty_digits = wizard.env['decimal.precision'].precision_get('Product Unit of Measure')
        qty_fmt = '#,##0.%s;-#,##0.%s;"-"' % ('0' * qty_digits, '0' * qty_digits) if qty_digits else '#,##0;-#,##0;"-"'
        money_fmt = self._money_format(wizard)
        base = {'font_name': 'Calibri', 'font_size': 10, 'valign': 'vcenter'}

        def fmt(**kw):
            return workbook.add_format(dict(base, **kw))

        styles = {
            'title': fmt(bold=True, font_size=18, font_color=BRAND),
            'subtitle': fmt(font_size=11, font_color='#555555'),
            'label': fmt(bold=True, font_color=HEADER),
            'value': fmt(font_color='#333333'),
            'band_in': fmt(bold=True, align='center', bg_color=BAND_IN, font_color='#1E6B3A', border=1, border_color='#C9D3DC'),
            'band_out': fmt(bold=True, align='center', bg_color=BAND_OUT, font_color='#9B2C2C', border=1, border_color='#C9D3DC'),
            'band_value': fmt(bold=True, align='center', bg_color=BAND_VALUE, font_color='#2B4C7E', border=1, border_color='#C9D3DC'),
            'band_blank': fmt(),
            'header': fmt(bold=True, font_color='#FFFFFF', bg_color=HEADER, align='center',
                          text_wrap=True, border=1, border_color='#FFFFFF'),
            'group': fmt(bold=True, font_size=11, bg_color=GROUP_BG, font_color=BRAND,
                         top=1, top_color=BRAND),
            'opening_label': fmt(italic=True, font_color='#555555'),
        }
        for zebra in (False, True):
            suffix = '_z' if zebra else ''
            extra = {'bg_color': ZEBRA} if zebra else {}
            styles['text' + suffix] = fmt(bottom=1, bottom_color='#E4E4E8', **extra)
            styles['qty' + suffix] = fmt(num_format=qty_fmt, bottom=1, bottom_color='#E4E4E8', **extra)
            styles['money' + suffix] = fmt(num_format=money_fmt, bottom=1, bottom_color='#E4E4E8', **extra)
            styles['datetime' + suffix] = fmt(num_format='yyyy-mm-dd hh:mm', align='left',
                                              bottom=1, bottom_color='#E4E4E8', **extra)
            styles['qty_bold' + suffix] = fmt(num_format=qty_fmt, bold=True, bottom=1,
                                              bottom_color='#E4E4E8', **extra)
        total = dict(bold=True, top=2, top_color=HEADER, bottom=2, bottom_color=HEADER, bg_color='#ECEFF3')
        styles['total_text'] = fmt(**total)
        styles['total_qty'] = fmt(num_format=qty_fmt, **total)
        styles['total_money'] = fmt(num_format=money_fmt, **total)
        sub = dict(bold=True, top=1, top_color='#9AA5B1', bg_color='#F3F4F6')
        styles['sub_text'] = fmt(**sub)
        styles['sub_qty'] = fmt(num_format=qty_fmt, **sub)
        styles['sub_money'] = fmt(num_format=money_fmt, **sub)
        return styles

    # ------------------------------------------------------------------
    # Header block (logo, title, filters)
    # ------------------------------------------------------------------
    def _write_header(self, workbook, sheet, styles, wizard, filters, ncols):
        company = wizard.company_ids[:1] or self.env.company
        sheet.hide_gridlines(2)
        sheet.set_row(0, 30)
        first_text_col = 0
        if company.logo:
            try:
                image = io.BytesIO(base64.b64decode(company.logo))
                from PIL import Image
                width, height = Image.open(image).size
                scale = min(45.0 / height, 160.0 / width) if height and width else 1
                image.seek(0)
                sheet.insert_image(0, 0, 'logo.png', {
                    'image_data': image, 'x_scale': scale, 'y_scale': scale,
                    'x_offset': 4, 'y_offset': 4, 'object_position': 3})
                first_text_col = 2
            except Exception:  # a broken logo must never break the export
                _logger.debug('Inventory ledger: company logo skipped', exc_info=True)
        last = max(ncols - 1, first_text_col + 3)
        sheet.merge_range(0, first_text_col, 0, last,
                          _('Inventory Ledger — %s', filters['report_type']), styles['title'])
        sheet.merge_range(1, first_text_col, 1, last,
                          '%s   |   %s' % (company.name, filters['period']), styles['subtitle'])

        info = [
            (_('Companies'), filters['companies']),
            (_('Warehouses'), filters['warehouses']),
            (_('Categories'), filters['categories']),
            (_('Products'), filters['products']),
            (_('Opening method'), filters['opening_method']),
            (_('Printed on'), filters['printed_on']),
        ]
        row = 3
        second_col = 6 if ncols > 12 else 4  # land on a wide numeric column
        for i in range(0, len(info), 2):
            for j, (label, value) in enumerate(info[i:i + 2]):
                col = second_col if j else 0
                sheet.write(row, col, label, styles['label'])
                sheet.merge_range(row, col + 1, row, col + 3, value, styles['value'])
            row += 1
        return row + 1

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    def _summary_columns(self, wizard):
        """(key, label, kind, width, band, total)"""
        cols = []
        if len(wizard.company_ids) > 1:
            cols.append(('company', _('Company'), 'text', 20, None, False))
        cols += [
            ('warehouse', _('Warehouse'), 'text', 16, None, False),
            ('default_code', _('Internal Ref'), 'text', 14, None, False),
            ('product', _('Product'), 'text', 34, None, False),
            ('categ', _('Category'), 'text', 18, None, False),
            ('uom', _('UoM'), 'text', 8, None, False),
            ('opening_qty', _('Opening'), 'qty_bold', 12, None, True),
            ('in_purchase_qty', _('Purchases / Receipts'), 'qty', 12, 'in', True),
            ('in_return_qty', _('Customer Returns'), 'qty', 11, 'in', True),
            ('in_internal_qty', _('Internal IN'), 'qty', 11, 'in', True),
            ('in_other_qty', _('Adjust. / Prod.'), 'qty', 11, 'in', True),
            ('in_qty', _('Total IN'), 'qty_bold', 12, 'in', True),
            ('out_sale_qty', _('Sales / Deliveries'), 'qty', 12, 'out', True),
            ('out_return_qty', _('Vendor Returns'), 'qty', 11, 'out', True),
            ('out_internal_qty', _('Internal OUT'), 'qty', 11, 'out', True),
            ('out_other_qty', _('Adjust. / Prod.'), 'qty', 11, 'out', True),
            ('out_qty', _('Total OUT'), 'qty_bold', 12, 'out', True),
            ('closing_qty', _('Closing'), 'qty_bold', 12, None, True),
            ('unit_cost', _('Avg. Unit Cost'), 'money', 13, 'value', False),
            ('opening_value', _('Opening Value'), 'money', 15, 'value', True),
            ('in_value', _('IN Value'), 'money', 15, 'value', True),
            ('out_value', _('OUT Value'), 'money', 15, 'value', True),
            ('closing_value', _('Closing Value'), 'money', 16, 'value', True),
        ]
        return cols

    def _write_bands(self, sheet, styles, columns, row):
        labels = {'in': _('INBOUND'), 'out': _('OUTBOUND'), 'value': _('VALUATION')}
        col = 0
        while col < len(columns):
            band = columns[col][4]
            end = col
            while end + 1 < len(columns) and columns[end + 1][4] == band:
                end += 1
            if band:
                if end > col:
                    sheet.merge_range(row, col, row, end, labels[band], styles['band_' + band])
                else:
                    sheet.write(row, col, labels[band], styles['band_' + band])
            col = end + 1

    def _write_column_headers(self, sheet, styles, columns, row):
        sheet.set_row(row, 32)
        for col, (key, label, kind, width, band, total) in enumerate(columns):
            sheet.set_column(col, col, width)
            sheet.write(row, col, label, styles['header'])

    def _summary_value(self, line, key):
        return {
            'company': lambda: line.company_id.name,
            'warehouse': lambda: line.warehouse_id.name,
            'default_code': lambda: line.product_id.default_code or '',
            'product': lambda: line.product_id.with_context(display_default_code=False).display_name,
            'categ': lambda: line.categ_id.complete_name or '',
            'uom': lambda: line.uom_id.name,
        }.get(key, lambda: line[key])()

    def _write_summary(self, sheet, styles, wizard, columns, row):
        self._write_bands(sheet, styles, columns, row)
        header_row = row + 1
        self._write_column_headers(sheet, styles, columns, header_row)
        sheet.freeze_panes(header_row + 1, len([c for c in columns if c[2] == 'text']))
        sheet.repeat_rows(row, header_row)

        lines = wizard._get_lines()
        first_data = header_row + 1
        row = first_data
        for idx, line in enumerate(lines):
            suffix = '_z' if idx % 2 else ''
            for col, (key, label, kind, width, band, total) in enumerate(columns):
                sheet.write(row, col, self._summary_value(line, key), styles[kind + suffix])
            row += 1
        last_data = row - 1

        if lines:
            sheet.autofilter(header_row, 0, last_data, len(columns) - 1)
        # Totals: SUBTOTAL(9, ...) respects the autofilter
        totals = wizard.get_report_totals()
        text_cols = [i for i, c in enumerate(columns) if c[2] == 'text']
        sheet.merge_range(row, 0, row, max(text_cols), _('TOTAL'), styles['total_text'])
        for col, (key, label, kind, width, band, total) in enumerate(columns):
            if col in text_cols:
                continue
            style = styles['total_money'] if kind == 'money' else styles['total_qty']
            if total and lines and xl_rowcol_to_cell:
                formula = '=SUBTOTAL(9,%s:%s)' % (xl_rowcol_to_cell(first_data, col),
                                                  xl_rowcol_to_cell(last_data, col))
                sheet.write_formula(row, col, formula, style, totals.get(key, 0.0))
            else:
                sheet.write_blank(row, col, None, style)

    # ------------------------------------------------------------------
    # Detailed
    # ------------------------------------------------------------------
    def _detailed_columns(self):
        return [
            ('date', _('Date'), 'datetime', 17, None, False),
            ('reference', _('Reference'), 'text', 24, None, False),
            ('partner', _('Partner'), 'text', 26, None, False),
            ('movement_type', _('Movement Type'), 'text', 24, None, False),
            ('in_qty', _('IN'), 'qty', 12, 'in', True),
            ('out_qty', _('OUT'), 'qty', 12, 'out', True),
            ('balance_qty', _('Balance'), 'qty_bold', 13, None, False),
            ('unit_cost', _('Unit Cost'), 'money', 13, 'value', False),
            ('in_value', _('IN Value'), 'money', 15, 'value', True),
            ('out_value', _('OUT Value'), 'money', 15, 'value', True),
        ]

    def _local_dt(self, wizard, value):
        if not value:
            return None
        return fields.Datetime.context_timestamp(wizard, value).replace(tzinfo=None)

    def _write_detailed(self, sheet, styles, wizard, columns, row):
        self._write_bands(sheet, styles, columns, row)
        header_row = row + 1
        self._write_column_headers(sheet, styles, columns, header_row)
        sheet.freeze_panes(header_row + 1, 0)
        sheet.repeat_rows(row, header_row)
        labels = self.env['inventory.ledger.line'].movement_type_labels()
        ncols = len(columns)
        row = header_row + 1
        grand = {'in_value': 0.0, 'out_value': 0.0}

        for group in wizard.get_report_groups():
            sheet.merge_range(row, 0, row, ncols - 1, '%s   (%s)' % (group['title'], group['uom']),
                              styles['group'])
            row += 1
            opening = group['opening']
            if opening and opening.date:
                sheet.write_datetime(row, 0, self._local_dt(wizard, opening.date), styles['datetime'])
            else:
                sheet.write_blank(row, 0, None, styles['text'])
            sheet.merge_range(row, 1, row, 3, _('Opening Balance'), styles['opening_label'])
            sheet.write_blank(row, 4, None, styles['qty'])
            sheet.write_blank(row, 5, None, styles['qty'])
            sheet.write(row, 6, group['totals']['opening_qty'], styles['qty_bold'])
            sheet.write(row, 7, opening.unit_cost if opening else 0.0, styles['money'])
            sheet.write(row, 8, group['totals']['opening_value'], styles['money'])
            sheet.write_blank(row, 9, None, styles['money'])
            row += 1
            for idx, line in enumerate(group['lines']):
                suffix = '_z' if idx % 2 else ''
                sheet.write_datetime(row, 0, self._local_dt(wizard, line.date), styles['datetime' + suffix])
                sheet.write(row, 1, line.reference or '', styles['text' + suffix])
                sheet.write(row, 2, line.partner_id.display_name or '', styles['text' + suffix])
                sheet.write(row, 3, labels.get(line.movement_type, ''), styles['text' + suffix])
                sheet.write(row, 4, line.in_qty, styles['qty' + suffix])
                sheet.write(row, 5, line.out_qty, styles['qty' + suffix])
                sheet.write(row, 6, line.balance_qty, styles['qty_bold' + suffix])
                sheet.write(row, 7, line.unit_cost, styles['money' + suffix])
                sheet.write(row, 8, line.in_value, styles['money' + suffix])
                sheet.write(row, 9, line.out_value, styles['money' + suffix])
                row += 1
            totals = group['totals']
            sheet.merge_range(row, 0, row, 3, _('Closing Balance'), styles['sub_text'])
            sheet.write(row, 4, totals['in_qty'], styles['sub_qty'])
            sheet.write(row, 5, totals['out_qty'], styles['sub_qty'])
            sheet.write(row, 6, totals['closing_qty'], styles['sub_qty'])
            sheet.write_blank(row, 7, None, styles['sub_money'])
            sheet.write(row, 8, totals['in_value'], styles['sub_money'])
            sheet.write(row, 9, totals['out_value'], styles['sub_money'])
            grand['in_value'] += totals['in_value']
            grand['out_value'] += totals['out_value']
            row += 2

        sheet.merge_range(row, 0, row, 7, _('TOTAL MOVEMENT VALUE'), styles['total_text'])
        sheet.write(row, 8, grand['in_value'], styles['total_money'])
        sheet.write(row, 9, grand['out_value'], styles['total_money'])
