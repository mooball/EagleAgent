"""Tests for the opportunity item sync (Update Opportunity flow).

All NetSuite calls are mocked — nothing here touches the live NetSuite API
or the local database.
"""

from types import SimpleNamespace

import pytest
from unittest.mock import MagicMock, patch

from includes.dashboard.routes import rfqs as rfqs_module
from includes.netsuite.records.base import CreateResult


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_item(
    line=1,
    part_number="BOLT-123",
    description="Hex bolt M12",
    brand="SampleBrand",
    brand_is_excluded=False,
    cost_price=10.5,
    sale_price=22.0,
    quantity=4,
    department_id="8",
    product_ns_id=None,
    brand_ns_id=None,
    issues=None,
    selected=None,
):
    return {
        "line": line,
        "part_number": part_number,
        "input_description": description,
        "brand": brand,
        "brand_is_excluded": brand_is_excluded,
        "brand_ns_id": brand_ns_id,
        "cost_price": cost_price,
        "sale_price": sale_price,
        "quantity": quantity,
        "department_id": department_id,
        "product_ns_id": product_ns_id,
        "sync_issues": issues or [],
        "selected_supplier": selected,
    }


def _make_selected(name="Acme Supplies", ns_linked=True, netsuite_id="77", near_matches=None, quote_currency=None):
    return {
        "name": name,
        "ns_linked": ns_linked,
        "netsuite_id": netsuite_id,
        "near_matches": near_matches or [],
        "quote_currency": quote_currency,
    }


def _patch_sync_env(monkeypatch, rfq_stub, opp_stub, items, ensure=None, upsert=None,
                    rfq_items=None, product=None):
    """Patch the environment around _sync_opportunity_items_sync.

    Every NetSuite-touching dependency is stubbed by default so tests can
    never reach the live API; pass custom mocks to assert specific calls.
    """

    def make_session_mock(rfq_row, opp_row):
        s = MagicMock()
        rfq_q = MagicMock()
        rfq_q.filter.return_value.first.return_value = rfq_row
        opp_q = MagicMock()
        opp_q.get.return_value = opp_row
        items_q = MagicMock()
        items_q.filter.return_value.all.return_value = rfq_items or []
        prod_q = MagicMock()
        prod_q.filter.return_value.first.return_value = product
        prod_q.get.return_value = None

        def query_side_effect(model):
            name = getattr(model, "__name__", "")
            if name == "RFQ":
                return rfq_q
            if name == "Opportunity":
                return opp_q
            if name == "RFQItem":
                return items_q
            if name == "Product":
                return prod_q
            m = MagicMock()
            m.filter.return_value.all.return_value = []
            m.get.return_value = None
            return m

        s.query.side_effect = query_side_effect
        return s

    monkeypatch.setattr(
        rfqs_module._helpers,
        "get_session",
        MagicMock(side_effect=lambda: make_session_mock(rfq_stub, opp_stub)),
    )

    monkeypatch.setattr(
        "includes.tools.quote_tools._get_rfq_dict_sync",
        lambda rfq_id: {"items": items, "opportunity_id": rfq_stub.opportunity_id},
    )
    monkeypatch.setattr(rfqs_module, "_rfq_sync_readiness", lambda d: None)
    monkeypatch.setattr(
        "includes.netsuite.records.opportunity.get_opportunity_currency",
        lambda ns_id: "AUD",
    )
    monkeypatch.setattr(
        "includes.netsuite.records.item.get_vendor_context",
        lambda ns_id: {"currency": "AUD", "tax_item_id": None},
    )
    monkeypatch.setattr(
        "includes.currency.convert",
        lambda amount, from_iso, to_iso: float(amount),
    )
    monkeypatch.setattr(
        "includes.netsuite.records.opportunity.upsert_opportunity_lines",
        upsert or MagicMock(return_value=CreateResult(success=True)),
    )
    monkeypatch.setattr(
        "includes.netsuite.records.item.ensure_item_with_vendor",
        ensure or MagicMock(return_value=CreateResult(success=True, netsuite_id="555")),
    )
    monkeypatch.setattr(
        "includes.netsuite.records.item.get_or_create_brand",
        MagicMock(return_value=CreateResult(success=True, netsuite_id="brand-1")),
    )
    monkeypatch.setattr("includes.netsuite.records.opportunity.NetSuiteClient", MagicMock)
    monkeypatch.setattr("includes.netsuite.records.item.NetSuiteClient", MagicMock)


@pytest.fixture()
def base_stubs():
    rfq = SimpleNamespace(
        id="rfq-uuid-1",
        rfq_number="RFQ-2026-0001",
        opportunity_id="opp-uuid-1",
        history=[],
        opportunity_sync_state=None,
    )
    opp = SimpleNamespace(netsuite_id="999", opportunity_number="OP99999")
    return rfq, opp


# ---------------------------------------------------------------------------
# upsert_opportunity_lines + get_opportunity_currency (records/opportunity.py)
# ---------------------------------------------------------------------------

class TestOpportunityRecordHelpers:
    def test_upsert_merges_existing_lines_by_item_id(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.get.return_value.json.return_value = {
            "item": {
                "items": [
                    {"item": {"id": "111"}, "quantity": 1, "rate": 10.0},
                ]
            }
        }
        monkeypatch.setattr(
            "includes.netsuite.records.opportunity.NetSuiteClient",
            lambda: fake_client,
        )
        from includes.netsuite.records.opportunity import upsert_opportunity_lines

        result = upsert_opportunity_lines("999", [
            {"item": {"id": "111"}, "quantity": 5, "rate": 12.0, "custcol_po_vendor": {"id": "77"}},
            {"item": {"id": "222"}, "quantity": 2, "rate": 3.0},
        ])

        assert result.success
        patch_call = fake_client.update_record.call_args
        assert patch_call.args[1] == "999"
        assert patch_call.kwargs["params"] == {"replace": "item"}
        merged = patch_call.args[2]["item"]["items"]
        by_id = {str(m["item"]["id"]): m for m in merged}
        assert len(merged) == 2
        assert by_id["111"]["quantity"] == 5
        assert by_id["111"]["rate"] == 12.0
        assert by_id["111"]["custcol_po_vendor"] == {"id": "77"}
        assert by_id["222"]["quantity"] == 2

    def test_upsert_read_failure_returns_error(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.get.side_effect = RuntimeError("boom")
        monkeypatch.setattr(
            "includes.netsuite.records.opportunity.NetSuiteClient",
            lambda: fake_client,
        )
        from includes.netsuite.records.opportunity import upsert_opportunity_lines

        result = upsert_opportunity_lines("999", [{"item": {"id": "1"}}])
        assert not result.success
        assert "boom" in (result.error or "")

    def test_upsert_strips_line_identity_and_amount(self, monkeypatch):
        """Existing lines must be re-added fresh (no line/links/amount) so
        NetSuite recomputes amount from quantity × rate."""
        fake_client = MagicMock()
        fake_client.get.return_value.json.return_value = {
            "item": {
                "items": [
                    {
                        "item": {"id": "111", "refName": "BOLT-1"},
                        "line": 18,
                        "links": [{"rel": "self", "href": "http://x"}],
                        "quantity": 1,
                        "rate": 10.0,
                        "amount": 160.0,
                        "custcol_po_rate": 9.0,
                    }
                ]
            }
        }
        monkeypatch.setattr(
            "includes.netsuite.records.opportunity.NetSuiteClient",
            lambda: fake_client,
        )
        from includes.netsuite.records.opportunity import upsert_opportunity_lines

        upsert_opportunity_lines("999", [
            {"item": {"id": "111"}, "quantity": 5, "rate": 12.0},
        ])
        sent = fake_client.update_record.call_args.args[2]["item"]["items"][0]
        assert "line" not in sent
        assert "links" not in sent
        assert "amount" not in sent
        assert sent["quantity"] == 5
        assert sent["custcol_po_rate"] == 9.0  # other fields preserved

    def test_get_opportunity_currency_reads_refname(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.get_record.return_value = {"currency": {"refName": "USD"}}
        monkeypatch.setattr(
            "includes.netsuite.records.opportunity.NetSuiteClient",
            lambda: fake_client,
        )
        from includes.netsuite.records.opportunity import get_opportunity_currency

        assert get_opportunity_currency("999") == "USD"

    def test_get_opportunity_currency_defaults_to_aud_on_failure(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.get_record.side_effect = RuntimeError("boom")
        monkeypatch.setattr(
            "includes.netsuite.records.opportunity.NetSuiteClient",
            lambda: fake_client,
        )
        from includes.netsuite.records.opportunity import get_opportunity_currency

        assert get_opportunity_currency("999") == "AUD"


# ---------------------------------------------------------------------------
# _sync_opportunity_items_sync (routes/rfqs.py)
# ---------------------------------------------------------------------------

class TestSyncOpportunityItems:
    def test_requires_linked_opportunity(self, monkeypatch, base_stubs):
        rfq, opp = base_stubs
        rfq.opportunity_id = None
        _patch_sync_env(monkeypatch, rfq, opp, [_make_item()])

        result = rfqs_module._sync_opportunity_items_sync("RFQ-2026-0001", "tester")
        assert result["status"] == "error"
        assert "opportunity" in result["message"].lower()

    def test_opportunity_without_ns_record_errors(self, monkeypatch, base_stubs):
        rfq, opp = base_stubs
        opp.netsuite_id = None
        _patch_sync_env(monkeypatch, rfq, opp, [_make_item()])

        result = rfqs_module._sync_opportunity_items_sync("RFQ-2026-0001", "tester")
        assert result["status"] == "error"

    def test_supplier_not_in_netsuite_blocks_line(self, monkeypatch, base_stubs):
        rfq, opp = base_stubs
        upsert = MagicMock(return_value=CreateResult(success=True))
        item = _make_item(selected=_make_selected(ns_linked=False))
        _patch_sync_env(monkeypatch, rfq, opp, [item], upsert=upsert)

        result = rfqs_module._sync_opportunity_items_sync("RFQ-2026-0001", "tester")
        assert result["status"] == "ok"
        assert result["synced"] == []
        assert len(result["blocked"]) == 1
        assert "NS+ form" in result["blocked"][0]["reasons"][0]
        upsert.assert_not_called()

    def test_issues_block_line(self, monkeypatch, base_stubs):
        rfq, opp = base_stubs
        upsert = MagicMock(return_value=CreateResult(success=True))
        item = _make_item(
            selected=_make_selected(),
            issues=[{"key": "department", "label": "Department not set"}],
        )
        _patch_sync_env(monkeypatch, rfq, opp, [item], upsert=upsert)

        result = rfqs_module._sync_opportunity_items_sync("RFQ-2026-0001", "tester")
        assert result["synced"] == []
        assert result["blocked"][0]["reasons"] == ["Department not set"]

    def test_warnings_gate_requires_confirmation(self, monkeypatch, base_stubs):
        rfq, opp = base_stubs
        upsert = MagicMock(return_value=CreateResult(success=True))
        item = _make_item(
            selected=_make_selected(near_matches=[{"name": "Acme Duplicate", "reasons": []}]),
        )
        _patch_sync_env(monkeypatch, rfq, opp, [item], upsert=upsert)

        result = rfqs_module._sync_opportunity_items_sync("RFQ-2026-0001", "tester")
        assert result["status"] == "warnings"
        assert result["lines"][0]["supplier"] == "Acme Supplies"
        upsert.assert_not_called()

        # Confirmed — proceeds and syncs
        result = rfqs_module._sync_opportunity_items_sync(
            "RFQ-2026-0001", "tester", confirm_warnings=True
        )
        assert result["status"] == "ok"
        assert len(result["synced"]) == 1
        assert len(result["warnings"]) == 1
        upsert.assert_called_once()

    def test_sync_links_item_to_local_product(self, monkeypatch, base_stubs):
        """After syncing, the RFQ item must be linked to the local Product
        row so the 'NS needs-to-be-created' badge clears."""
        rfq, opp = base_stubs
        upsert = MagicMock(return_value=CreateResult(success=True))
        item = _make_item(selected=_make_selected(), product_ns_id="555")
        item_row = SimpleNamespace(line=1, product_id=None)
        product = SimpleNamespace(id="prod-1", netsuite_id="555")
        _patch_sync_env(monkeypatch, rfq, opp, [item], upsert=upsert,
                        rfq_items=[item_row], product=product)

        result = rfqs_module._sync_opportunity_items_sync("RFQ-2026-0001", "tester")
        assert result["status"] == "ok"
        assert item_row.product_id == "prod-1"

    def test_other_brand_syncs_as_formal_ns_record(self, monkeypatch, base_stubs):
        """'Other' is a formal NetSuite brand record — it must sync, not block."""
        rfq, opp = base_stubs
        upsert = MagicMock(return_value=CreateResult(success=True))
        item = _make_item(brand="Other", brand_is_excluded=True, selected=_make_selected())
        _patch_sync_env(monkeypatch, rfq, opp, [item], upsert=upsert)

        result = rfqs_module._sync_opportunity_items_sync("RFQ-2026-0001", "tester")
        assert result["status"] == "ok"
        assert len(result["synced"]) == 1
        assert len(result["blocked"]) == 0
        # The line's New Item Brand field must carry the resolved brand id.
        assert result["synced"][0]["brand_ns_id"] == "brand-1"
        upsert.assert_called_once()

    def test_non_other_excluded_brand_still_blocks(self, monkeypatch, base_stubs):
        rfq, opp = base_stubs
        upsert = MagicMock(return_value=CreateResult(success=True))
        item = _make_item(brand="n/a", brand_is_excluded=True, selected=_make_selected())
        _patch_sync_env(monkeypatch, rfq, opp, [item], upsert=upsert)

        result = rfqs_module._sync_opportunity_items_sync("RFQ-2026-0001", "tester")
        assert result["synced"] == []
        assert len(result["blocked"]) == 1
        assert "cannot be created in NetSuite" in result["blocked"][0]["reasons"][0]
        upsert.assert_not_called()

    def test_sync_creates_item_when_product_ns_id_missing(self, monkeypatch, base_stubs):
        rfq, opp = base_stubs
        upsert = MagicMock(return_value=CreateResult(success=True))
        ensure = MagicMock(return_value=CreateResult(success=True, netsuite_id="555"))
        item = _make_item(selected=_make_selected(), product_ns_id=None)
        _patch_sync_env(monkeypatch, rfq, opp, [item], upsert=upsert, ensure=ensure)

        result = rfqs_module._sync_opportunity_items_sync("RFQ-2026-0001", "tester")
        assert result["status"] == "ok"
        ensure.assert_called_once()
        kwargs = ensure.call_args.kwargs
        assert kwargs["part_number"] == "BOLT-123"
        assert kwargs["vendor_netsuite_id"] == "77"
        assert kwargs["purchase_price"] == 10.5
        assert kwargs["department_id"] == "8"

    def test_sync_uses_existing_product_ns_id(self, monkeypatch, base_stubs):
        rfq, opp = base_stubs
        upsert = MagicMock(return_value=CreateResult(success=True))
        ensure = MagicMock()
        item = _make_item(selected=_make_selected(), product_ns_id="555")
        _patch_sync_env(monkeypatch, rfq, opp, [item], upsert=upsert, ensure=ensure)

        result = rfqs_module._sync_opportunity_items_sync("RFQ-2026-0001", "tester")
        assert result["status"] == "ok"
        ensure.assert_not_called()
        sent = upsert.call_args.args[1]
        assert sent[0]["item"] == {"id": "555"}

    def test_line_payload_fields(self, monkeypatch, base_stubs):
        rfq, opp = base_stubs
        upsert = MagicMock(return_value=CreateResult(success=True))
        item = _make_item(selected=_make_selected(), product_ns_id="555")
        _patch_sync_env(monkeypatch, rfq, opp, [item], upsert=upsert)

        rfqs_module._sync_opportunity_items_sync("RFQ-2026-0001", "tester")
        sent = upsert.call_args.args[1][0]
        assert sent["item"] == {"id": "555"}
        assert sent["quantity"] == 4
        assert sent["rate"] == 22.0
        assert sent["custcol_po_rate"] == 10.5
        assert sent["custcol_po_vendor"] == {"id": "77"}
        assert sent["department"] == {"id": "8"}
        # Estimate fields — always AUD (converted from the quote currency)
        assert sent["costEstimateRate"] == 10.5
        assert sent["costEstimate"] == 42.0
        # amount is omitted — NetSuite recomputes it when lines are re-added
        assert "amount" not in sent
        # New Item Code / New Item Brand custom fields
        assert sent["custcol_new_item_code"] == "BOLT-123"
        assert sent["custcol_new_item_brand"] == {"id": "brand-1"}

    def test_net_suite_failure_returns_error(self, monkeypatch, base_stubs):
        rfq, opp = base_stubs
        upsert = MagicMock(return_value=CreateResult(success=False, error="NS exploded"))
        item = _make_item(selected=_make_selected(), product_ns_id="555")
        _patch_sync_env(monkeypatch, rfq, opp, [item], upsert=upsert)

        result = rfqs_module._sync_opportunity_items_sync("RFQ-2026-0001", "tester")
        assert result["status"] == "error"
        assert "NS exploded" in result["message"]

    def test_history_entry_appended(self, monkeypatch, base_stubs):
        rfq, opp = base_stubs
        upsert = MagicMock(return_value=CreateResult(success=True))
        item = _make_item(selected=_make_selected(), product_ns_id="555")
        _patch_sync_env(monkeypatch, rfq, opp, [item], upsert=upsert)

        result = rfqs_module._sync_opportunity_items_sync("RFQ-2026-0001", "tester")
        assert result["status"] == "ok"
        assert rfq.history, "history entry should have been appended"
        assert "OP99999" in rfq.history[-1]["action"]

    def test_sync_state_persisted(self, monkeypatch, base_stubs):
        rfq, opp = base_stubs
        upsert = MagicMock(return_value=CreateResult(success=True))
        item = _make_item(selected=_make_selected(), product_ns_id="555")
        _patch_sync_env(monkeypatch, rfq, opp, [item], upsert=upsert)

        rfqs_module._sync_opportunity_items_sync("RFQ-2026-0001", "tester")
        state = rfq.opportunity_sync_state
        assert state, "opportunity_sync_state should be persisted"
        assert state["lines"] == [1]
        assert state["items"] == {"1": "555"}
        assert state["last_synced_at"]
        snap = state["snapshot"]["1"]
        assert snap["ns_item_id"] == "555"
        assert snap["part_number"] == "BOLT-123"
        assert snap["sale_price"] == 22.0
        assert snap["cost_price"] == 10.5
        assert snap["cost_currency"] == "AUD"
        assert snap["quantity"] == 4
        assert snap["department_id"] == "8"
        assert snap["supplier_ns_id"] == "77"
        assert snap["brand_ns_id"] == "brand-1"

    def test_sync_state_replaces_snapshot_with_latest_run(self, monkeypatch, base_stubs):
        rfq, opp = base_stubs
        rfq.opportunity_sync_state = {
            "last_synced_at": "2026-09-09T00:00:00",
            "lines": [1],
            "items": {"1": "555"},
            "snapshot": {"1": {"ns_item_id": "555"}},
        }
        upsert = MagicMock(return_value=CreateResult(success=True))
        item = _make_item(line=2, part_number="WASHER-9", product_ns_id="777",
                           selected=_make_selected())
        _patch_sync_env(monkeypatch, rfq, opp, [item], upsert=upsert)

        rfqs_module._sync_opportunity_items_sync("RFQ-2026-0001", "tester")
        state = rfq.opportunity_sync_state
        assert state["lines"] == [2]
        assert state["items"] == {"2": "777"}
        assert "1" not in state["snapshot"]

    def test_usd_quote_cost_converts_from_usd(self, monkeypatch, base_stubs):
        """Regression: cost prices carry the selected supplier's quote
        currency (USD quotes are stored as USD, not AUD)."""
        rfq, opp = base_stubs
        upsert = MagicMock(return_value=CreateResult(success=True))
        ensure = MagicMock(return_value=CreateResult(success=True, netsuite_id="555"))
        conversions = []

        def record_convert(amount, from_iso, to_iso):
            conversions.append((from_iso, to_iso))
            return float(amount)

        item = _make_item(
            cost_price=100.0,
            selected=_make_selected(quote_currency="USD"),
            product_ns_id=None,
        )
        _patch_sync_env(monkeypatch, rfq, opp, [item], upsert=upsert, ensure=ensure)
        monkeypatch.setattr("includes.currency.convert", record_convert)

        result = rfqs_module._sync_opportunity_items_sync("RFQ-2026-0001", "tester")
        assert result["status"] == "ok"
        assert ensure.call_args.kwargs["price_currency"] == "USD"
        # cost conversions start from USD: PO rate → vendor currency, estimate → AUD
        assert ("USD", "AUD") in conversions
        # sale price stays AUD-based (to opportunity currency)
        assert ("AUD", "AUD") in conversions
        sent = upsert.call_args.args[1][0]
        assert sent["costEstimateRate"] == 100.0
        assert sent["costEstimate"] == 400.0

    def test_payload_sets_purchorderrate_estimate_type(self, monkeypatch, base_stubs):
        """Regression (OP73275): lines must carry costEstimateType
        PURCHORDERRATE or NetSuite defaults to AVGCOST and drops the
        explicit estimate costs — the converted Quotation then has no
        extended cost."""
        rfq, opp = base_stubs
        upsert = MagicMock(return_value=CreateResult(success=True))
        item = _make_item(selected=_make_selected(), product_ns_id="555")
        _patch_sync_env(monkeypatch, rfq, opp, [item], upsert=upsert)

        result = rfqs_module._sync_opportunity_items_sync("RFQ-2026-0001", "tester")
        assert result["status"] == "ok"
        sent = upsert.call_args.args[1][0]
        assert sent["costEstimateType"] == {"id": "PURCHORDERRATE"}
        assert sent["costEstimateRate"] == 10.5
        assert sent["costEstimate"] == 42.0  # 10.5 × qty 4, rounded


# ---------------------------------------------------------------------------
# _diff_sync_snapshot (pure dirty-flag helper)
# ---------------------------------------------------------------------------

SNAP = {
    "part_number": "BOLT-123",
    "ns_item_id": "555",
    "sale_price": 22.0,
    "cost_price": 10.5,
    "cost_currency": "AUD",
    "quantity": 4,
    "department_id": "8",
    "supplier_ns_id": "77",
}


def _clean_item():
    return {
        "line": 1,
        "part_number": "BOLT-123",
        "product_ns_id": "555",
        "sale_price": 22.0,
        "cost_price": 10.5,
        "quantity": 4,
        "department_id": "8",
        "selected_supplier": _make_selected(),
    }


class TestDiffSyncSnapshot:
    def test_new_item(self):
        assert rfqs_module._diff_sync_snapshot(_clean_item(), None, "") == ["new item"]

    def test_clean_item(self):
        assert rfqs_module._diff_sync_snapshot(_clean_item(), SNAP, "AUD") == []

    def test_sale_changed(self):
        item = _clean_item()
        item["sale_price"] = 99.0
        assert "sale" in rfqs_module._diff_sync_snapshot(item, SNAP, "AUD")

    def test_cost_changed(self):
        item = _clean_item()
        item["cost_price"] = 8.0
        assert "cost" in rfqs_module._diff_sync_snapshot(item, SNAP, "AUD")

    def test_cost_currency_changed(self):
        assert "cost" in rfqs_module._diff_sync_snapshot(_clean_item(), SNAP, "USD")

    def test_quantity_changed(self):
        item = _clean_item()
        item["quantity"] = 9
        assert "qty" in rfqs_module._diff_sync_snapshot(item, SNAP, "AUD")

    def test_department_changed(self):
        item = _clean_item()
        item["department_id"] = "5"
        assert "department" in rfqs_module._diff_sync_snapshot(item, SNAP, "AUD")

    def test_supplier_changed(self):
        item = _clean_item()
        item["selected_supplier"] = _make_selected(netsuite_id="999")
        assert "supplier" in rfqs_module._diff_sync_snapshot(item, SNAP, "AUD")

    def test_brand_changed(self):
        item = _clean_item()
        item["brand_ns_id"] = "42"
        assert "brand" in rfqs_module._diff_sync_snapshot(item, SNAP, "AUD")

    def test_part_number_changed(self):
        item = _clean_item()
        item["part_number"] = "NUT-42"
        assert "item" in rfqs_module._diff_sync_snapshot(item, SNAP, "AUD")

    def test_ns_item_id_mismatch(self):
        """The line now resolves to a different NetSuite item than it was
        pushed under (stale/cross-wired product_id) — must count as dirty."""
        item = _clean_item()
        item["part_number"] = "BOLT-123"
        item["product_ns_id"] = "999"
        assert "item" in rfqs_module._diff_sync_snapshot(item, SNAP, "AUD")

    def test_multiple_fields(self):
        item = _clean_item()
        item["sale_price"] = 1.0
        item["quantity"] = 2
        dirty = rfqs_module._diff_sync_snapshot(item, SNAP, "AUD")
        assert set(dirty) == {"sale", "qty"}


# ---------------------------------------------------------------------------
# get_or_create_brand — local-first resolution (sync/read path parity)
# ---------------------------------------------------------------------------

class TestGetOrCreateBrandLocalFirst:
    """The sync path must resolve brands the same way the dashboard read
    path does (canonical local rows only), or the snapshot and the dirty
    check disagree and a line shows 'changed: brand' forever.
    """

    def _patch_local_brands(self, monkeypatch, rows):
        from includes.netsuite.records import item as item_module

        session = MagicMock()
        q = MagicMock()
        q.filter.return_value.order_by.return_value.all.return_value = rows
        session.query.return_value = q
        monkeypatch.setattr(item_module, "get_session", lambda: session)
        return session

    def test_local_canonical_row_wins_over_ns_search(self, monkeypatch):
        from includes.netsuite.records import item as item_module

        self._patch_local_brands(monkeypatch, [
            SimpleNamespace(name="Ridgid", netsuite_id="1029"),
        ])
        ns_search = MagicMock(return_value="300")
        create = MagicMock()
        monkeypatch.setattr(item_module, "find_brand_by_name", ns_search)
        monkeypatch.setattr(item_module, "create_brand", create)

        result = item_module.get_or_create_brand("Ridgid")
        assert result.success
        assert result.netsuite_id == "1029"
        ns_search.assert_not_called()
        create.assert_not_called()

    def test_falls_back_to_ns_search_when_no_local_canonical(self, monkeypatch):
        from includes.netsuite.records import item as item_module

        self._patch_local_brands(monkeypatch, [])
        monkeypatch.setattr(
            item_module, "find_brand_by_name", MagicMock(return_value="55")
        )
        monkeypatch.setattr(item_module, "_writeback_brand_sync", MagicMock())
        create = MagicMock()
        monkeypatch.setattr(item_module, "create_brand", create)

        result = item_module.get_or_create_brand("Acme")
        assert result.success
        assert result.netsuite_id == "55"
        create.assert_not_called()

    def test_creates_when_neither_local_nor_ns(self, monkeypatch):
        from includes.netsuite.records import item as item_module

        self._patch_local_brands(monkeypatch, [])
        monkeypatch.setattr(
            item_module, "find_brand_by_name", MagicMock(return_value=None)
        )
        create = MagicMock(
            return_value=CreateResult(success=True, netsuite_id="77", record_type="brand")
        )
        monkeypatch.setattr(item_module, "create_brand", create)

        result = item_module.get_or_create_brand("NewBrand")
        assert result.success
        assert result.netsuite_id == "77"
        create.assert_called_once()
