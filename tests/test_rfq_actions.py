"""Tests for includes/chat/rfq_actions.py — RFQ callback logic.

Tests the deterministic / pure-logic portions without requiring Chainlit.
"""

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Item classification logic (from on_rfq_identify_items Step A)
# ---------------------------------------------------------------------------

class TestItemClassificationLogic:
    """Test the classification rules extracted from on_rfq_identify_items.

    Rules:
      - specific: has part_number + description
      - branded: has brand + description (no part_number)
      - generic: description only
      - None: no description
    """

    @staticmethod
    def _classify(part_number="", brand="", description=""):
        """Replicate the classification logic from rfq_actions.py."""
        has_part = bool(part_number)
        has_brand = bool(
            brand and brand.strip().lower() not in ("other", "n/a", "na", "none", "unknown")
        )
        has_desc = bool(description)

        if has_part and has_desc:
            return "specific"
        elif has_brand and has_desc:
            return "branded"
        elif has_desc:
            return "generic"
        return None

    def test_specific_with_part_and_desc(self):
        assert self._classify(part_number="ABC-123", description="Widget") == "specific"

    def test_specific_with_part_brand_desc(self):
        assert self._classify(part_number="ABC-123", brand="Acme", description="Widget") == "specific"

    def test_branded_with_brand_and_desc(self):
        assert self._classify(brand="Acme", description="Widget") == "branded"

    def test_generic_desc_only(self):
        assert self._classify(description="Stainless steel pipe 50mm") == "generic"

    def test_none_without_desc(self):
        assert self._classify(part_number="ABC-123") is None

    def test_none_empty(self):
        assert self._classify() is None

    def test_brand_other_ignored(self):
        assert self._classify(brand="Other", description="Widget") == "generic"

    def test_brand_na_ignored(self):
        assert self._classify(brand="N/A", description="Widget") == "generic"

    def test_brand_none_ignored(self):
        assert self._classify(brand="none", description="Widget") == "generic"

    def test_brand_unknown_ignored(self):
        assert self._classify(brand="Unknown", description="Widget") == "generic"

    def test_brand_case_insensitive(self):
        assert self._classify(brand="OTHER", description="Widget") == "generic"

    def test_brand_with_spaces(self):
        assert self._classify(brand="  n/a  ", description="Widget") == "generic"

    def test_valid_brand(self):
        assert self._classify(brand="3M", description="Tape") == "branded"

    def test_part_without_desc_is_none(self):
        """Part number alone isn't enough — need description too."""
        assert self._classify(part_number="ABC-123", brand="Acme") is None


# ---------------------------------------------------------------------------
# _cross_apply_suppliers_sync — supplier dedup logic
# ---------------------------------------------------------------------------

class TestCrossApplySuppliers:
    @patch("includes.tools.rfq_crud._get_session")
    def test_adds_new_supplier(self, mock_get_session):
        """New supplier is appended to the item's suppliers list."""
        from includes.dashboard.models import RFQ, RFQItem

        mock_item = MagicMock()
        mock_item.suppliers = [{"name": "Existing Co", "status": "candidate"}]

        mock_rfq = MagicMock()
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.side_effect = [
            mock_rfq,   # RFQ query
            mock_item,  # RFQItem query
        ]
        mock_get_session.return_value = mock_session

        from includes.chat.rfq_actions import _cross_apply_suppliers_sync
        _cross_apply_suppliers_sync("RFQ-001", 1, [
            {"name": "New Supplier", "contacts": [{"email": "a@b.com"}]},
        ])

        # Should have 2 suppliers now
        assert len(mock_item.suppliers) == 2
        assert mock_item.suppliers[1]["name"] == "New Supplier"
        mock_session.commit.assert_called_once()

    @patch("includes.tools.rfq_crud._get_session")
    def test_deduplicates_existing(self, mock_get_session):
        """Supplier already on item is not added again."""
        mock_item = MagicMock()
        mock_item.suppliers = [{"name": "Acme Corp", "status": "candidate"}]

        mock_rfq = MagicMock()
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.side_effect = [
            mock_rfq,
            mock_item,
        ]
        mock_get_session.return_value = mock_session

        from includes.chat.rfq_actions import _cross_apply_suppliers_sync
        _cross_apply_suppliers_sync("RFQ-001", 1, [
            {"name": "Acme Corp"},  # duplicate
        ])

        assert len(mock_item.suppliers) == 1
        mock_session.commit.assert_called_once()

    @patch("includes.tools.rfq_crud._get_session")
    def test_dedup_case_insensitive(self, mock_get_session):
        """Deduplication is case-insensitive."""
        mock_item = MagicMock()
        mock_item.suppliers = [{"name": "Acme Corp", "status": "candidate"}]

        mock_rfq = MagicMock()
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.side_effect = [
            mock_rfq,
            mock_item,
        ]
        mock_get_session.return_value = mock_session

        from includes.chat.rfq_actions import _cross_apply_suppliers_sync
        _cross_apply_suppliers_sync("RFQ-001", 1, [
            {"name": "acme corp"},  # same, different case
        ])

        assert len(mock_item.suppliers) == 1

    @patch("includes.tools.rfq_crud._get_session")
    def test_rfq_not_found(self, mock_get_session):
        """Gracefully handles missing RFQ."""
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = None
        mock_get_session.return_value = mock_session

        from includes.chat.rfq_actions import _cross_apply_suppliers_sync
        _cross_apply_suppliers_sync("NONEXISTENT", 1, [{"name": "X"}])
        mock_session.commit.assert_not_called()

    @patch("includes.tools.rfq_crud._get_session")
    def test_empty_initial_suppliers(self, mock_get_session):
        """Works when item has no existing suppliers (None)."""
        mock_item = MagicMock()
        mock_item.suppliers = None

        mock_rfq = MagicMock()
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.side_effect = [
            mock_rfq,
            mock_item,
        ]
        mock_get_session.return_value = mock_session

        from includes.chat.rfq_actions import _cross_apply_suppliers_sync
        _cross_apply_suppliers_sync("RFQ-001", 1, [
            {"name": "First Supplier"},
        ])

        assert len(mock_item.suppliers) == 1
        assert mock_item.suppliers[0]["name"] == "First Supplier"
