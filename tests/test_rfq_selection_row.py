"""Tests for the targeted Selection-row refresh helpers in rfqs.py.

Covers the server side of Phase 2 of plan-rfqSelectionPerformance: the row +
totals partials rendered on their own so a single star click does not re-render
the whole RFQ detail page. These are deliberately DB-free — items carry no valid
UUID supplier ids and no part numbers, so no enrichment query runs.
"""

import json
from unittest.mock import patch

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

    def test_skips_non_shortlisted_status(self):
        """A candidate supplier with a usable quote must not be selected — the
        Selection matrix never offers it, so a direct request must not either."""
        item = _FakeItem([{
            "name": "Acme", "status": "candidate",
            "quote_status": "quoted", "quote_cost": 5.0,
        }])
        assert _select_supplier_on_item(item, "acme") == "skipped"
        assert item.suppliers[0]["quote_status"] == "quoted"
        assert item.cost_price is None


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


class TestSupplierNameJsEscaping:
    """Supplier names are external, persisted data. They must reach Alpine via
    HTML-escaped data attributes, never interpolated into a JS string literal —
    a trailing backslash would otherwise escape the closing quote and break out
    of the expression (Copilot review finding on _rfq_selection_matrix.html)."""

    HOSTILE = "Acme\\"   # trailing backslash — would escape a JS closing quote
    QUOTED = 'A"B'       # double quote — would break a double-quoted attribute

    def _render_matrix(self, name: str) -> str:
        from includes.dashboard.routes._helpers import templates

        items = [_item(1, [
            {"name": name, "status": "shortlisted", "quote_status": "quoted",
             "quote_cost": 5.0, "supplier_id": None},
        ])]
        return templates.env.get_template("partials/_rfq_selection_matrix.html").render(
            rfq=_rfq(items),
            supplier_names=[name],
            supplier_quotable_counts={name: 1},
        )

    def test_select_all_button_reads_dataset_not_literal(self):
        html = self._render_matrix(self.HOSTILE)
        assert "data-supplier-name=" in html
        assert "prepareSelectAllSupplier($el.dataset.supplierName" in html
        # The old, unsafe interpolation must be gone.
        assert "prepareSelectAllSupplier('" not in html

    def test_supplier_meta_popover_reads_dataset_not_literal(self):
        items = [_item(1, [
            {"name": self.HOSTILE, "status": "shortlisted", "quote_status": "quoted",
             "quote_cost": 5.0, "supplier_id": None},
        ])]
        rfq = _rfq(items)
        rfq["supplier_meta"] = {self.HOSTILE: {"notes": "handle with \\ care"}}
        from includes.dashboard.routes._helpers import templates

        html = templates.env.get_template("partials/_rfq_selection_matrix.html").render(
            rfq=rfq, supplier_names=[self.HOSTILE], supplier_quotable_counts={self.HOSTILE: 1},
        )
        assert "$el.dataset.supplierName" in html
        assert "$el.dataset.supplierNotes" in html
        assert "show-supplier-meta" in html
        # The old literal interpolation of the notes value must be gone.
        assert "notes: 'handle" not in html

    def test_quote_in_name_is_html_escaped(self):
        html = self._render_matrix(self.QUOTED)
        assert 'data-supplier-name="A&#34;B"' in html

    def test_selection_row_actions_read_dataset(self):
        item = _item(1, [
            {"name": self.HOSTILE, "status": "shortlisted", "quote_status": "quoted",
             "quote_cost": 5.0, "supplier_id": None},
        ])
        row = json.loads(_selection_row_json(_rfq([item]), 1).body)["row"]
        assert "$el.dataset.supplierName" in row
        # No literal supplier-name interpolation in any row action.
        assert "selectSupplier(1, 'Acme" not in row
        assert "updateSupplierQuote(1, 'Acme" not in row
        assert "toggleDeclined(1, 'Acme" not in row

    def test_quotation_table_reads_dataset(self):
        from includes.dashboard.routes._helpers import templates

        item = _item(1, [
            {"name": self.HOSTILE, "status": "shortlisted", "quote_status": "quoted",
             "quote_cost": 5.0, "quote_currency": "AUD", "quote_leadtime": "2 weeks"},
        ])
        html = templates.env.get_template("partials/_rfq_quotation_table.html").render(
            rfq=_rfq([item]),
        )
        assert "$el.dataset.supplierName" in html
        assert "selectSupplier(1, 'Acme" not in html
        assert "updateSupplierQuote(1, 'Acme" not in html
        assert "cycleQuoteStatus(1, 'Acme" not in html

    def test_supplier_search_options_reads_dataset(self):
        from includes.dashboard.routes._helpers import templates

        html = templates.env.get_template("partials/_supplier_search_options.html").render(
            results=[{"id": "sup_1", "name": self.HOSTILE, "duplicate": False,
                      "netsuite_id": None, "source": "web"}],
            q="ac",
        )
        assert "$el.dataset.supplierName" in html
        assert "name: 'Acme" not in html

    def test_last_sale_popover_reads_dataset(self):
        item = _item(1, [
            {"name": "Acme", "status": "shortlisted", "quote_status": "quoted",
             "quote_cost": 5.0, "supplier_id": None},
        ])
        item["last_sale"] = {
            "price": 12.5, "date": "2026-01-02", "vendor": "Bob's \\ Hardware",
            "doc_label": "SO", "doc_number": "123",
        }
        row = json.loads(_selection_row_json(_rfq([item]), 1).body)["row"]
        assert "$el.dataset.lastSaleVendor" in row
        assert "data-last-sale-vendor=" in row
        assert "vendor: 'Bob" not in row


class TestTabScopedEnrichment:
    """partial_rfq_detail_tab must only back-fill supplier contacts for tabs that
    render them; Selection and Communications skip it (Copilot review finding)."""

    async def _call(self, tab, calls):
        from includes.dashboard.routes import rfqs as rfq_mod

        def fake_enrich(rfq):
            calls.append(tab)

        with patch("includes.tools.quote_tools._get_rfq_dict_sync",
                   return_value={"id": "RFQ-1", "items": []}), \
             patch.object(rfq_mod, "_enrich_rfq_supplier_contacts",
                          side_effect=fake_enrich), \
             patch.object(rfq_mod, "_render_rfq_detail_partial_response",
                          return_value="ok"):
            await rfq_mod.partial_rfq_detail_tab(
                object(), "RFQ-1", tab, {"email": "t@test"}
            )

    async def test_selection_and_communications_skip_enrichment(self):
        for tab in ("selection", "communications"):
            calls = []
            await self._call(tab, calls)
            assert calls == [], f"{tab} must not back-fill supplier contacts"

    async def test_rendering_tabs_still_enrich(self):
        for tab in ("items", "quotation", "suppliers", "quotation-old"):
            calls = []
            await self._call(tab, calls)
            assert calls == [tab], f"{tab} must back-fill supplier contacts"
