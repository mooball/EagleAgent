"""Tests for supplied-part-number recompute on the RFQ write paths.

Covers Phase 2 of `.github/prompts/plan-suppliedPartNumber.prompt.md`
(todo.vu #33073): supplier select/deselect, selected-supplier quote edits, and
the manual override. These are DB-free — the core helpers take a line item and
only touch ``flag_modified``, which is patched out here.
"""

from unittest.mock import patch

from includes.tools.rfq_crud import (
    _select_quote_core,
    _update_item_core,
    _update_supplier_core,
    supplied_after_supplier_change,
)


class _FakeLineItem:
    def __init__(self, suppliers=None, part_number=None, cost_price=None,
                 supplied_part_number=None, line=1, product_id=None,
                 match=None):
        self.suppliers = suppliers or []
        self.part_number = part_number
        self.cost_price = cost_price
        self.supplied_part_number = supplied_part_number
        self.line = line
        self.product_id = product_id
        self.match = match


def _sup(name, quote_status, quote_cost, part_number=None):
    return {
        "name": name,
        "status": "shortlisted",
        "quote_status": quote_status,
        "quote_cost": quote_cost,
        "quote_part_number": part_number,
    }


_FLAG = "sqlalchemy.orm.attributes.flag_modified"


class TestSelectQuoteCore:
    def test_select_sets_supplied_and_preserves_requested(self):
        item = _FakeLineItem(
            part_number="REQ-1",
            suppliers=[_sup("Acme", "quoted", 5.0, "SUP-9")],
        )
        with patch(_FLAG):
            action, name = _select_quote_core(None, None, item, {"name": "Acme"})
        assert name == "Acme"
        assert item.suppliers[0]["quote_status"] == "selected"
        assert item.part_number == "REQ-1"
        assert item.supplied_part_number == "SUP-9"
        assert float(item.cost_price) == 5.0

    def test_select_description_only_uses_supplied(self):
        item = _FakeLineItem(suppliers=[_sup("Acme", "quoted", 5.0, "SUP-9")])
        with patch(_FLAG):
            _select_quote_core(None, None, item, {"name": "Acme"})
        assert item.part_number is None
        assert item.supplied_part_number == "SUP-9"

    def test_select_equal_number_clears_supplied(self):
        item = _FakeLineItem(
            part_number="REQ-1",
            supplied_part_number="STALE",
            suppliers=[_sup("Acme", "quoted", 5.0, "REQ-1")],
        )
        with patch(_FLAG):
            _select_quote_core(None, None, item, {"name": "Acme"})
        assert item.supplied_part_number is None

    def test_deselect_clears_supplied(self):
        item = _FakeLineItem(
            part_number="REQ-1",
            cost_price=5.0,
            supplied_part_number="SUP-9",
            suppliers=[_sup("Acme", "selected", 5.0, "SUP-9")],
        )
        with patch(_FLAG):
            action, _ = _select_quote_core(None, None, item, {"name": "Acme"})
        assert "Deselected" in action
        assert item.supplied_part_number is None
        assert item.cost_price is None


class TestUpdateSupplierCore:
    def test_selected_supplier_quote_edit_recomputes_supplied(self):
        item = _FakeLineItem(
            part_number="REQ-1",
            suppliers=[_sup("Acme", "selected", 5.0, "OLD-9")],
            supplied_part_number="OLD-9",
        )
        with patch(_FLAG):
            changes, name = _update_supplier_core(
                None, None, item, {"name": "Acme", "quote_part_number": "NEW-9"}
            )
        assert name == "Acme"
        assert "quote_part_number" in changes
        assert item.supplied_part_number == "NEW-9"

    def test_selected_supplier_clearing_quote_number_clears_supplied(self):
        item = _FakeLineItem(
            part_number="REQ-1",
            suppliers=[_sup("Acme", "selected", 5.0, "OLD-9")],
            supplied_part_number="OLD-9",
        )
        with patch(_FLAG):
            _update_supplier_core(
                None, None, item, {"name": "Acme", "quote_part_number": ""}
            )
        assert item.supplied_part_number is None

    def test_unselected_supplier_edit_leaves_supplied_untouched(self):
        item = _FakeLineItem(
            part_number="REQ-1",
            suppliers=[_sup("Acme", "quoted", 5.0, "OLD-9")],
            supplied_part_number="manual-keep",
        )
        with patch(_FLAG):
            _update_supplier_core(
                None, None, item, {"name": "Acme", "quote_part_number": "NEW-9"}
            )
        assert item.supplied_part_number == "manual-keep"


class TestSupplierStatusChange:
    """Declining/deselecting via quote_status must clear the supplied number
    (Copilot review findings on PR #223)."""

    def test_declining_selected_supplier_clears_supplied(self):
        item = _FakeLineItem(
            part_number="REQ-1",
            supplied_part_number="SP-9",
            suppliers=[_sup("Acme", "selected", 5.0, "SP-9")],
        )
        with patch(_FLAG):
            _update_supplier_core(
                None, None, item, {"name": "Acme", "quote_status": "declined"}
            )
        assert item.supplied_part_number is None

    def test_selecting_via_quote_status_populates_supplied(self):
        item = _FakeLineItem(
            part_number="REQ-1",
            suppliers=[_sup("Acme", "quoted", 5.0, "SP-9")],
        )
        with patch(_FLAG):
            _update_supplier_core(
                None, None, item, {"name": "Acme", "quote_status": "selected"}
            )
        assert item.supplied_part_number == "SP-9"

    def test_status_change_on_unselected_supplier_keeps_override(self):
        item = _FakeLineItem(
            part_number="REQ-1",
            supplied_part_number="manual-keep",
            suppliers=[_sup("Acme", "quoted", 5.0, "SP-9")],
        )
        with patch(_FLAG):
            _update_supplier_core(
                None, None, item, {"name": "Acme", "quote_status": "declined"}
            )
        assert item.supplied_part_number == "manual-keep"


class TestSuppliedAfterSupplierChange:
    def test_selected_derives_from_quote(self):
        assert supplied_after_supplier_change(
            "REQ-1", {"quote_status": "selected", "quote_part_number": "SP-9"}, False
        ) == (True, "SP-9")

    def test_was_selected_now_not_clears(self):
        assert supplied_after_supplier_change(
            "REQ-1", {"quote_status": "declined"}, True
        ) == (True, None)

    def test_unrelated_edit_is_not_applied(self):
        assert supplied_after_supplier_change(
            "REQ-1", {"quote_status": "quoted"}, False
        ) == (False, None)


class TestUpdateItemCoreManualOverride:
    def test_sets_manual_override(self):
        item = _FakeLineItem(line=1, part_number="REQ-1")
        changes, line, reset = _update_item_core(
            None, None, item, {"supplied_part_number": "MANUAL-1"}, "u"
        )
        assert item.supplied_part_number == "MANUAL-1"
        assert "supplied_part_number" in changes
        assert reset is False

    def test_blank_clears_override(self):
        item = _FakeLineItem(line=1, supplied_part_number="MANUAL-1")
        _update_item_core(None, None, item, {"supplied_part_number": ""}, "u")
        assert item.supplied_part_number is None

    def test_placeholder_clears_override(self):
        item = _FakeLineItem(line=1, supplied_part_number="MANUAL-1")
        _update_item_core(None, None, item, {"supplied_part_number": "tbd"}, "u")
        assert item.supplied_part_number is None

    def test_override_does_not_reset_pipeline_or_drop_product(self):
        item = _FakeLineItem(
            line=1, part_number="REQ-1", product_id="P-1", match="specific",
        )
        changes, _, reset = _update_item_core(
            None, None, item, {"supplied_part_number": "MANUAL-1"}, "u"
        )
        assert item.product_id == "P-1"
        assert item.match == "specific"
        assert reset is False
