# -*- coding: utf-8 -*-
from . import models
from . import wizard
from . import controllers

LEDGER_INDEXES = (
    'stock_move_ledger_done_product_date_idx',
    'stock_valuation_layer_ledger_idx',  # created by 16.0-18.0; dropped if a migrated DB still has it
)


def uninstall_hook(env):
    """Drop the helper indexes this module created on core tables."""
    for index in LEDGER_INDEXES:
        env.cr.execute('DROP INDEX IF EXISTS %s' % index)
