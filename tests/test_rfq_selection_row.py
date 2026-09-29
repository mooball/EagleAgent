"""Tests for the targeted Selection-row refresh helpers in rfqs.py.

Covers the server side of Phase 2 of plan-rfqSelectionPerformance: the row +
totals partials rendered on their own so a single star click does not re-render
the whole RFQ detail page. These are deliberately DB-free — items carry no valid
UUID supplier ids and no part numbers, so no enrichment query runs.
"""

import json

from includes.dashboard.routes.rfqs import (
    _select_supplier_on_item,
    _selection_row_json,
    _selection_supplier_names,
    _selection_supplier_quotable_counts,
)


def _item(line, suppliers, **overrides):
    item = {
        "line": line,
        "input_description": f"Item {line}",
        "part_number": None,
        "brand": None,
        "quantity": 2,
        "uom": "ea",
        "cost_price": 5.0,
        "sale_price": 10.0,
        "suppliers": suppliers,
    }
    item.update(overrides)
    return item


def _rfq(items):
    return {
        "id": "RFQ-TEST",
        "rfq_number": "RFQ-TEST",
        "items": items,
        "supplier_meta": {},
    }


class TestSelectionSupplierNames:
    def test_ordered_unique_shortlisted_and_selected_only(self):
        items = [
            _item(1, [
                {"name": "Acme", "status": "shortlisted"},
                {"name": "Beta", "status": "candidate"},
                {"name": "Acme", "status": "selected"},
            ]),
            _item(2, [
                {"name": "Gamma", "status": "shortlisted"},
                {"name": "Acme", "status": "shortlisted"},
            ]),
        ]
        assert _selection_supplier_names(items) == ["Acme", "Gamma"]

    def test_empty_items(self):
        assert _selection_supplier_names([]) == []


class TestSelectionRowJson:
    def test_row_and_totals_render(self):
        rfq = _rfq([_item(1, [
            {"name": "Acme", "status": "shortlisted", "quote_status": "quoted",
             "quote_cost": 5.0, "supplier_id": None},
        ])])
        resp = _selection_row_json(rfq, 1)
        assert resp is not None
        data = json.loads(resp.body)
        assert 'data-line="1"' in data["row"]
        assert "Acme" in data["row"]
        assert "data-selection-totals" in data["totals"]
        # 2 x 5.00 cost = 10.00, 2 x 10.00 sale = 20.00
        assert "$10.00" in data["totals"]
        assert "$20.00" in data["totals"]

    def test_missing_line_returns_none(self):
        rfq = _rfq([_item(1, [])])
        assert _selection_row_json(rfq, 99) is None

    def test_selected_and_broken_link_are_reflected(self):
        rfq = _rfq([_item(1, [
            {"name": "Acme", "status": "shortlisted", "quote_status": "selected",
             "quote_cost": 5.0, "supplier_id": "sup_legacy"},
        ])])
        data = json.loads(_selection_row_json(rfq, 1).body)
        assert "quote-selected" in data["row"]
        # Broken-link warning carries the explanatory title text
        assert "database link is broken" in data["row"]

    def test_totals_only_sum_costing_lines(self):
        rfq = _rfq([
            _item(1, [], cost_price=5.0, sale_price=10.0),
            _item(2, [], cost_price=None, sale_price=None),
        ])
        data = json.loads(_selection_row_json(rfq, 1).body)
        assert "$10.00" in data["totals"]
        assert "$20.00" in data["totals"]


class _FakeItem:
    def __init__(self, suppliers, part_number=None, cost_price=None):
        self.suppliers = suppliers
        self.part_number = part_number
        self.cost_price = cost_price


def _sup(name, quote_status, quote_cost, part_number=None):
    return {
        "name": name,
        "status": "shortlisted",
        "quote_status": quote_status,
        "quote_cost": quote_cost,
        "quote_part_number": part_number,
    }


class TestSelectSupplierOnItem:
    """The bulk action must apply the same per-line state as one star click."""

    def test_selects_and_copies_cost_and_part_number(self):
        item = _FakeItem([_sup("Acme", "quoted", 5.0, "PN-9")])
        assert _select_supplier_on_item(item, "acme") == "changed"
        assert item.suppliers[0]["quote_status"] == "selected"
        assert float(item.cost_price) == 5.0
        assert item.part_number == "PN-9"

    def test_deselects_previous_selection(self):
        item = _FakeItem([
            _sup("Other", "selected", 1.0),
            _sup("Acme", "quoted", 7.0),
        ])
        assert _select_supplier_on_item(item, "acme") == "changed"
        assert item.suppliers[0]["quote_status"] == "quoted"
        assert item.suppliers[1]["quote_status"] == "selected"
        assert float(item.cost_price) == 7.0

    def test_preserves_existing_part_number(self):
        item = _FakeItem([_sup("Acme", "quoted", 5.0, "SUPPLIER-PN")],
                         part_number="EXISTING")
        assert _select_supplier_on_item(item, "acme") == "changed"
        assert item.part_number == "EXISTING"

    def test_non_changing_outcomes(self):
        assert _select_supplier_on_item(_FakeItem([]), "acme") == "absent"
        assert _select_supplier_on_item(
            _FakeItem([_sup("Acme", "declined", None)]), "acme") == "skipped"
        assert _select_supplier_on_item(
            _FakeItem([_sup("Acme", "quoted", None)]), "acme") == "skipped"
        assert _select_supplier_on_item(
            _FakeItem([_sup("Acme", "selected", 5.0)]), "acme") == "already"


class TestSelectionSupplierQuotableCounts:
    def test_counts_only_usable_quotes(self):
        items = [
            {"suppliers": [
                _sup("Acme", "quoted", 5.0),
                _sup("Beta", "declined", 0.0),
            ]},
            {"suppliers": [
                _sup("Acme", "selected", 4.0),
                {"name": "Gamma", "status": "candidate", "quote_status": "quoted", "quote_cost": 2.0},
            ]},
            {"suppliers": [
                _sup("Acme", "quoted", None),
            ]},
        ]
        assert _selection_supplier_quotable_counts(items) == {"Acme": 2}
