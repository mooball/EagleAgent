"""Tests for brand DB-status annotation on RFQ items (dashboard items table)."""

from unittest.mock import patch

from includes.dashboard.routes.rfqs import _annotate_brand_db_status


def _rfq_with_items(*brands):
    return {
        "items": [
            {"line": i + 1, "brand": brand}
            for i, brand in enumerate(brands)
        ]
    }


def _result(status, alternatives=None):
    return {"status": status, "brand": None, "alternatives": alternatives or []}


class TestAnnotateBrandDbStatus:
    def test_statuses_annotated(self):
        rfq = _rfq_with_items("Toyota", "toyzz", "Zzz")
        lookup = {
            "Toyota": _result("exact", ["Toyota Parts", "Toyota Industrial"]),
            "toyzz": _result("near", ["Toyzz Parts", "Toyzz Industrial", "Toyzz Mining", "Toyzz Civil"]),
            "Zzz": _result("none"),
        }
        with patch("includes.tools.product_tools.match_brands", return_value=lookup):
            _annotate_brand_db_status(rfq)

        items = {i["line"]: i for i in rfq["items"]}
        assert items[1]["brand_db_status"] == "exact"
        assert items[1]["brand_db_alternatives"] == ["Toyota Parts", "Toyota Industrial"]
        assert items[1]["brand_db_alt_total"] == 2
        assert items[2]["brand_db_status"] == "near"
        assert items[2]["brand_db_alternatives"] == ["Toyzz Parts", "Toyzz Industrial", "Toyzz Mining"]
        assert items[2]["brand_db_alt_total"] == 4
        assert items[3]["brand_db_status"] == "none"
        assert items[3]["brand_db_alternatives"] == []

    def test_exclusions_and_blank_skipped(self):
        rfq = _rfq_with_items("Other", "n/a", "", None)
        with patch("includes.tools.product_tools.match_brands", return_value={}) as mock:
            _annotate_brand_db_status(rfq)
        # No brands worth looking up → matcher never called
        mock.assert_not_called()
        for item in rfq["items"]:
            assert "brand_db_status" not in item

    def test_lookup_failure_leaves_items_untouched(self):
        rfq = _rfq_with_items("Toyota")
        with patch("includes.tools.product_tools.match_brands", side_effect=Exception("db down")):
            _annotate_brand_db_status(rfq)
        assert "brand_db_status" not in rfq["items"][0]

    def test_no_items(self):
        with patch("includes.tools.product_tools.match_brands", return_value={}) as mock:
            _annotate_brand_db_status({"items": []})
        mock.assert_not_called()
