"""Tests for includes/tools/supplier_quote_pipeline.py — quote classification and processing."""

import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Regex patterns — _QUOTE_KEYWORDS and _CURRENCY_RE
# ---------------------------------------------------------------------------

class TestQuoteKeywordsRegex:
    def setup_method(self):
        from includes.tools.supplier_quote_pipeline import _QUOTE_KEYWORDS
        self.pattern = _QUOTE_KEYWORDS

    def test_matches_quote(self):
        assert self.pattern.search("Please find attached quote")

    def test_matches_quotation(self):
        assert self.pattern.search("Our quotation for your reference")

    def test_matches_pricing(self):
        assert self.pattern.search("Updated pricing below")

    def test_matches_unit_price(self):
        assert self.pattern.search("Unit price: $42.50")

    def test_matches_lead_time(self):
        assert self.pattern.search("Lead time is 4-6 weeks")

    def test_matches_leadtime_no_space(self):
        assert self.pattern.search("Leadtime: 2 weeks")

    def test_matches_ex_works(self):
        assert self.pattern.search("Price is ex-works Shanghai")

    def test_matches_exworks_no_hyphen(self):
        assert self.pattern.search("Price is ex works")

    def test_matches_fob(self):
        assert self.pattern.search("FOB pricing attached")

    def test_matches_payment_terms(self):
        assert self.pattern.search("Payment terms: Net 30")

    def test_matches_proforma(self):
        assert self.pattern.search("Pro-forma invoice attached")

    def test_matches_offer(self):
        assert self.pattern.search("Our best offer for you")

    def test_no_match_generic(self):
        assert not self.pattern.search("Thank you for your email, we will get back to you.")

    def test_case_insensitive(self):
        assert self.pattern.search("QUOTATION ATTACHED")


class TestCurrencyRegex:
    def setup_method(self):
        from includes.tools.supplier_quote_pipeline import _CURRENCY_RE
        self.pattern = _CURRENCY_RE

    def test_dollar_sign(self):
        assert self.pattern.search("Total: $1,250.00")

    def test_euro_sign(self):
        assert self.pattern.search("Price: €500")

    def test_usd_prefix(self):
        assert self.pattern.search("USD 1,200.50 per unit")

    def test_aud_prefix(self):
        assert self.pattern.search("AUD 850.00")

    def test_eur_prefix(self):
        assert self.pattern.search("EUR 3,500")

    def test_gbp_prefix(self):
        assert self.pattern.search("GBP 220")

    def test_no_match_text(self):
        assert not self.pattern.search("Please confirm the order")

    def test_multiple_currencies(self):
        text = "Item A: $100, Item B: €200, Item C: AUD 300"
        assert len(self.pattern.findall(text)) == 3


# ---------------------------------------------------------------------------
# _classify_heuristic_fallback
# ---------------------------------------------------------------------------

class TestClassifyHeuristicFallback:
    def _make_tracking(self, body="", attachments=None):
        return SimpleNamespace(
            body_markdown=body,
            attachments_json=attachments,
        )

    def test_strong_quote_signals(self):
        from includes.tools.supplier_quote_pipeline import _classify_heuristic_fallback
        tracking = self._make_tracking(
            body="Please find our quotation attached. Unit price: $42.50 USD per item. "
                 "Lead time is 4 weeks ex-works. Payment terms Net 30.",
            attachments=[{"filename": "quotation.pdf", "inline": False}],
        )
        result = _classify_heuristic_fallback(tracking)
        assert result["classification"] == "quote_response"

    def test_no_signals(self):
        from includes.tools.supplier_quote_pipeline import _classify_heuristic_fallback
        tracking = self._make_tracking(
            body="Thank you for your email. We will get back to you shortly.",
        )
        result = _classify_heuristic_fallback(tracking)
        assert result["classification"] == "not_quote"

    def test_weak_signals_needs_review(self):
        from includes.tools.supplier_quote_pipeline import _classify_heuristic_fallback
        tracking = self._make_tracking(
            body="We can offer $100 for the item.",
        )
        result = _classify_heuristic_fallback(tracking)
        assert result["classification"] in ("needs_review", "quote_response")

    def test_attachment_filename_signals(self):
        from includes.tools.supplier_quote_pipeline import _classify_heuristic_fallback
        tracking = self._make_tracking(
            body="Please see attached.",
            attachments=[
                {"filename": "price_list_2026.pdf", "inline": False},
                {"filename": "proforma_invoice.xlsx", "inline": False},
            ],
        )
        result = _classify_heuristic_fallback(tracking)
        assert result["classification"] in ("quote_response", "needs_review")

    def test_inline_attachments_ignored(self):
        from includes.tools.supplier_quote_pipeline import _classify_heuristic_fallback
        tracking = self._make_tracking(
            body="Thanks for your inquiry.",
            attachments=[
                {"filename": "quote.pdf", "inline": True},  # inline — ignored
            ],
        )
        result = _classify_heuristic_fallback(tracking)
        assert result["classification"] == "not_quote"

    def test_no_attachments(self):
        from includes.tools.supplier_quote_pipeline import _classify_heuristic_fallback
        tracking = self._make_tracking(body="General follow-up email.")
        result = _classify_heuristic_fallback(tracking)
        assert result["classification"] == "not_quote"

    def test_reason_includes_counts(self):
        from includes.tools.supplier_quote_pipeline import _classify_heuristic_fallback
        tracking = self._make_tracking(
            body="Our quotation: $500 USD, pricing valid for 30 days",
        )
        result = _classify_heuristic_fallback(tracking)
        assert "Heuristic fallback" in result["reason"]


# ---------------------------------------------------------------------------
# _apply_quote_data — quote-to-RFQ matching logic
# ---------------------------------------------------------------------------

class TestApplyQuoteData:
    def _mock_rfq(self):
        return {
            "rfq_number": "RFQ-001",
            "items": [
                {
                    "line": 1,
                    "description": "Widget A",
                    "suppliers": [
                        {"name": "Acme Corp", "quote_cost": None, "contacts": []},
                    ],
                },
                {
                    "line": 2,
                    "description": "Widget B",
                    "suppliers": [],
                },
            ],
        }

    @patch("includes.tools.rfq_crud._set_supplier_meta_sync")
    @patch("includes.tools.rfq_crud._add_supplier_sync")
    @patch("includes.tools.rfq_crud._update_supplier_sync")
    @patch("includes.tools.rfq_crud._get_rfq_dict_sync")
    def test_update_existing_supplier(self, mock_get_rfq, mock_update, mock_add, mock_meta):
        from includes.tools.supplier_quote_pipeline import _apply_quote_data
        mock_get_rfq.return_value = self._mock_rfq()

        quote_data = {
            "quotes": [
                {"item_line": 1, "price": 42.50, "currency": "AUD", "lead_time": "4 weeks", "confidence": "high"},
            ],
        }
        actions = _apply_quote_data("RFQ-001", "Acme Corp", quote_data, "test@test.com")

        mock_update.assert_called_once()
        mock_add.assert_not_called()
        assert any("Line 1" in a and "42.5" in a for a in actions)

    @patch("includes.tools.rfq_crud._set_supplier_meta_sync")
    @patch("includes.tools.rfq_crud._add_supplier_sync")
    @patch("includes.tools.rfq_crud._update_supplier_sync")
    @patch("includes.tools.rfq_crud._get_rfq_dict_sync")
    def test_add_new_supplier(self, mock_get_rfq, mock_update, mock_add, mock_meta):
        from includes.tools.supplier_quote_pipeline import _apply_quote_data
        mock_get_rfq.return_value = self._mock_rfq()

        quote_data = {
            "quotes": [
                {"item_line": 2, "price": 18.00, "currency": "USD"},
            ],
        }
        actions = _apply_quote_data("RFQ-001", "NewSupplier", quote_data, "test@test.com")

        mock_add.assert_called_once()
        mock_update.assert_not_called()
        assert any("added NewSupplier" in a for a in actions)

    @patch("includes.tools.rfq_crud._set_supplier_meta_sync")
    @patch("includes.tools.rfq_crud._add_supplier_sync")
    @patch("includes.tools.rfq_crud._update_supplier_sync")
    @patch("includes.tools.rfq_crud._get_rfq_dict_sync")
    def test_rfq_not_found(self, mock_get_rfq, mock_update, mock_add, mock_meta):
        from includes.tools.supplier_quote_pipeline import _apply_quote_data
        mock_get_rfq.return_value = None

        actions = _apply_quote_data("NONEXISTENT", "Acme", {}, "test@test.com")
        assert any("not found" in a for a in actions)

    @patch("includes.tools.rfq_crud._set_supplier_meta_sync")
    @patch("includes.tools.rfq_crud._add_supplier_sync")
    @patch("includes.tools.rfq_crud._update_supplier_sync")
    @patch("includes.tools.rfq_crud._get_rfq_dict_sync")
    def test_line_not_found(self, mock_get_rfq, mock_update, mock_add, mock_meta):
        from includes.tools.supplier_quote_pipeline import _apply_quote_data
        mock_get_rfq.return_value = self._mock_rfq()

        quote_data = {"quotes": [{"item_line": 99, "price": 10.00}]}
        actions = _apply_quote_data("RFQ-001", "Acme Corp", quote_data, "test@test.com")
        assert any("99" in a and "not found" in a for a in actions)

    @patch("includes.tools.rfq_crud._set_supplier_meta_sync")
    @patch("includes.tools.rfq_crud._add_supplier_sync")
    @patch("includes.tools.rfq_crud._update_supplier_sync")
    @patch("includes.tools.rfq_crud._get_rfq_dict_sync")
    def test_declined_items(self, mock_get_rfq, mock_update, mock_add, mock_meta):
        from includes.tools.supplier_quote_pipeline import _apply_quote_data
        mock_get_rfq.return_value = self._mock_rfq()

        quote_data = {"declined_items": [1]}
        actions = _apply_quote_data("RFQ-001", "Acme Corp", quote_data, "test@test.com")
        mock_update.assert_called_once()
        assert any("declined" in a for a in actions)

    @patch("includes.tools.rfq_crud._set_supplier_meta_sync")
    @patch("includes.tools.rfq_crud._add_supplier_sync")
    @patch("includes.tools.rfq_crud._update_supplier_sync")
    @patch("includes.tools.rfq_crud._get_rfq_dict_sync")
    def test_supplier_meta(self, mock_get_rfq, mock_update, mock_add, mock_meta):
        from includes.tools.supplier_quote_pipeline import _apply_quote_data
        mock_get_rfq.return_value = self._mock_rfq()

        quote_data = {
            "shipping": {"cost": 150, "currency": "AUD"},
            "terms": "Net 30",
            "notes": "Valid for 30 days",
        }
        actions = _apply_quote_data("RFQ-001", "Acme Corp", quote_data, "test@test.com")
        mock_meta.assert_called_once()
        assert any("shipping" in a for a in actions)
        assert any("Net 30" in a for a in actions)

    @patch("includes.tools.rfq_crud._set_supplier_meta_sync")
    @patch("includes.tools.rfq_crud._add_supplier_sync")
    @patch("includes.tools.rfq_crud._update_supplier_sync")
    @patch("includes.tools.rfq_crud._get_rfq_dict_sync")
    def test_skips_missing_price(self, mock_get_rfq, mock_update, mock_add, mock_meta):
        from includes.tools.supplier_quote_pipeline import _apply_quote_data
        mock_get_rfq.return_value = self._mock_rfq()

        quote_data = {"quotes": [{"item_line": 1}]}  # no price
        actions = _apply_quote_data("RFQ-001", "Acme Corp", quote_data, "test@test.com")
        mock_update.assert_not_called()
        mock_add.assert_not_called()
