# -*- coding: utf-8 -*-
from werkzeug.exceptions import NotFound

from odoo import http
from odoo.http import content_disposition, request

XLSX_MIMETYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


class InventoryLedgerController(http.Controller):

    @http.route('/advanced_inventory_ledger_report/xlsx/<int:wizard_id>',
                type='http', auth='user', methods=['GET'])
    def download_xlsx(self, wizard_id, **kwargs):
        """Stream the XLSX for a ledger run owned by the current user."""
        wizard = request.env['inventory.ledger.wizard'].browse(wizard_id).exists()
        if not wizard:
            raise NotFound()
        # ACL + record rule (own runs only) - raises AccessError otherwise
        wizard.check_access_rights('read')
        wizard.check_access_rule('read')
        content, filename = wizard._generate_xlsx()
        return request.make_response(content, headers=[
            ('Content-Type', XLSX_MIMETYPE),
            ('Content-Length', len(content)),
            ('Content-Disposition', content_disposition(filename)),
            ('Cache-Control', 'no-store'),
        ])
