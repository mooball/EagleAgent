"""Tests for rfq_item_import — content detection, parsing, and column mapping."""

import sys
import os
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from includes.tools.rfq_item_import import (
    detect_content_type,
    parse_html_table,
    parse_text_table,
    auto_detect_columns,
    _parse_quantity,
    _COLUMN_PATTERNS,
    MAX_IMAGE_SIZE_MB,
    MAX_BATCH_SIZE,
)


# ============================================================================
# Content type detection
# ============================================================================

class TestDetectContentType:
    def test_detects_html_table_with_table_tag(self):
        result = detect_content_type("<table><tr><td>X</td></tr></table>", None, None)
        assert result == "html_table"

    def test_detects_html_table_with_tr_tag(self):
        result = detect_content_type("<div><tr><td>X</td></tr></div>", None, None)
        assert result == "html_table"

    def test_detects_image(self):
        result = detect_content_type(None, "iVBORw0KGgo...", None)
        assert result == "image"

    def test_detects_tsv(self):
        result = detect_content_type(None, None, "Name\tQty\tBrand\nDrill\t5\tMakita")
        assert result == "tsv"

    def test_detects_csv(self):
        result = detect_content_type(None, None, "Name,Qty,Brand\nDrill,5,Makita")
        assert result == "csv"

    def test_plain_text_fallback(self):
        result = detect_content_type(None, None, "Just some text\nno delimiters here")
        assert result == "plain_text"

    def test_unknown_when_empty(self):
        result = detect_content_type(None, None, None)
        assert result == "unknown"

    def test_html_takes_priority_over_text(self):
        result = detect_content_type(
            "<table><tr><td>X</td></tr></table>",
            None,
            "Name,Qty\nDrill,5",
        )
        assert result == "html_table"


# ============================================================================
# HTML table parsing
# ============================================================================

class TestParseHtmlTable:
    def test_simple_table_with_header(self):
        html = """
        <table>
            <tr><th>Description</th><th>Qty</th><th>Brand</th></tr>
            <tr><td>Cordless Drill</td><td>5</td><td>Makita</td></tr>
            <tr><td>Impact Driver</td><td>3</td><td>Makita</td></tr>
        </table>
        """
        result = parse_html_table(html)
        assert result["item_count"] == 2
        assert result["headers"] == ["Description", "Qty", "Brand"]
        assert result["items"][0]["input_description"] == "Cordless Drill"
        assert result["items"][0]["quantity"] == 5
        assert result["items"][0]["brand"] == "Makita"
        assert result["items"][1]["input_description"] == "Impact Driver"
        assert result["items"][1]["quantity"] == 3

    def test_table_without_th_detects_headers_by_content(self):
        """Without <th>, if first row contains known column names ('Item', 'Part #',
        'Qty Req'd'), it should be detected as a header row via probe matching."""
        html = """
        <table>
            <tr><td>Item</td><td>Part #</td><td>Qty Req'd</td></tr>
            <tr><td>Drill</td><td>DHP486Z</td><td>10</td></tr>
        </table>
        """
        result = parse_html_table(html)
        # "Item" matches input_description, "Part #" matches input_code,
        # "Qty Req'd" matches quantity — ≥2 matches → treated as header row
        assert result["item_count"] == 1
        assert result["items"][0]["input_description"] == "Drill"
        assert result["items"][0]["input_code"] == "DHP486Z"
        assert result["items"][0]["quantity"] == 10

    def test_table_without_th_no_known_headers_falls_back(self):
        """Without <th> AND no known column names in first row, treats all rows as data."""
        html = """
        <table>
            <tr><td>Foo</td><td>Bar</td><td>Baz</td></tr>
            <tr><td>X</td><td>Y</td><td>Z</td></tr>
        </table>
        """
        result = parse_html_table(html)
        # No column patterns matched → col_0, col_1, col_2 headers, all rows as data
        assert result["headers"] == ["col_0", "col_1", "col_2"]
        # Items have no input_description (no mapping), so they get skipped
        assert result["item_count"] == 0

    def test_returns_fields_parallel_to_headers(self):
        html = """
        <table>
            <tr><th>Description</th><th>Qty</th><th>Brand</th></tr>
            <tr><td>Drill</td><td>5</td><td>Makita</td></tr>
        </table>
        """
        result = parse_html_table(html)
        assert "fields" in result
        assert len(result["fields"]) == len(result["headers"])
        # fields should parallel headers
        for i, h in enumerate(result["headers"]):
            assert result["fields"][i] == result["column_mapping"].get(h, "")

    def test_skips_row_without_description(self):
        html = """
        <table>
            <tr><th>Description</th><th>Qty</th></tr>
            <tr><td></td><td>5</td></tr>
            <tr><td>Valid Item</td><td>3</td></tr>
        </table>
        """
        result = parse_html_table(html)
        assert result["item_count"] == 1
        assert result["items"][0]["input_description"] == "Valid Item"

    def test_warns_on_bad_quantity(self):
        html = """
        <table>
            <tr><th>Description</th><th>Qty</th></tr>
            <tr><td>Item A</td><td>N/A</td></tr>
        </table>
        """
        result = parse_html_table(html)
        assert result["item_count"] == 1
        assert result["items"][0]["quantity"] is None
        assert any("quantity" in w.lower() for w in result["warnings"])

    def test_handles_bold_and_links_in_cells(self):
        html = """
        <table>
            <tr><th>Description</th><th>Brand</th></tr>
            <tr><td><b>Cordless Drill</b></td><td><a href="#">Makita</a></td></tr>
        </table>
        """
        result = parse_html_table(html)
        assert result["items"][0]["input_description"] == "Cordless Drill"
        assert result["items"][0]["brand"] == "Makita"

    def test_no_table_returns_empty(self):
        result = parse_html_table("<div>No table here</div>")
        assert result["item_count"] == 0
        assert result["warnings"]

    def test_empty_table_returns_empty(self):
        result = parse_html_table("<table></table>")
        assert result["item_count"] == 0
        assert result["warnings"]

    def test_picks_best_table_when_nested(self):
        html = """
        <table><tr><td>Outer</td></tr></table>
        <div>
            <table>
                <tr><th>Description</th><th>Qty</th></tr>
                <tr><td>Drill</td><td>5</td></tr>
                <tr><td>Saw</td><td>2</td></tr>
            </table>
        </div>
        """
        result = parse_html_table(html)
        # Should pick the table with <th> (header), not the outer single-row one
        assert result["item_count"] == 2


# ============================================================================
# Text/CSV/TSV parsing
# ============================================================================

class TestParseTextTable:
    def test_csv_with_header(self):
        text = "Description,Qty,Brand\nCordless Drill,5,Makita\nImpact Driver,3,Makita"
        result = parse_text_table(text, "csv")
        assert result["item_count"] == 2
        assert result["items"][0]["input_description"] == "Cordless Drill"
        assert result["items"][0]["quantity"] == 5

    def test_tsv_with_header(self):
        text = "Description\tQty\tBrand\nCordless Drill\t5\tMakita"
        result = parse_text_table(text, "tsv")
        assert result["item_count"] == 1
        assert result["items"][0]["brand"] == "Makita"

    def test_csv_without_header(self):
        text = "Unknown Col A,Unknown Col B\nSome Value,Another"
        result = parse_text_table(text, "csv")
        # No known columns detected — first row treated as data
        # Headers become col_0, col_1
        assert len(result["headers"]) >= 1

    def test_empty_text(self):
        result = parse_text_table("", "csv")
        assert result["item_count"] == 0
        assert result["warnings"]

    def test_blank_rows_skipped(self):
        text = "Description,Qty\nItem A,5\n,\nItem B,3\n,,,"
        result = parse_text_table(text, "csv")
        assert result["item_count"] == 2

    def test_extra_columns_ignored(self):
        text = "Description,Qty,Brand,Extra\nDrill,5,Makita,ignored"
        result = parse_text_table(text, "csv")
        # Extra column not mapped — just means fewer mapped fields
        assert result["item_count"] == 1


# ============================================================================
# Column auto-detection
# ============================================================================

class TestAutoDetectColumns:
    def test_detects_standard_headers(self):
        mapping = auto_detect_columns(["description", "quantity", "brand"])
        assert mapping["description"] == "input_description"
        assert mapping["quantity"] == "quantity"
        assert mapping["brand"] == "brand"

    def test_detects_aliases(self):
        mapping = auto_detect_columns(["item", "qty", "make"])
        assert mapping["item"] == "input_description"
        assert mapping["qty"] == "quantity"
        assert mapping["make"] == "brand"

    def test_normalises_separators_for_fuzzy_match(self):
        """Underscores, slashes, and other separators are normalised.
        'Part Number' (space) should match 'part_number' (underscore) pattern.
        With strict exact matching, only headers that exactly equal a known
        pattern (after normalisation) are mapped."""
        mapping = auto_detect_columns(["Part Number", "Qty Req'd", "Item"])
        assert mapping["Part Number"] == "input_code"
        assert mapping["Qty Req'd"] == "quantity"
        # "Item" exact-matches the "item" pattern
        assert mapping["Item"] == "input_description"

    def test_strict_no_fuzzy_matching(self):
        """Headers that don't exactly match a known pattern are NOT mapped.
        This prevents false positives like 'Vendor Name' matching 'name'."""
        mapping = auto_detect_columns(["Item Desc", "Vendor Name", "Department", "Part Num"])
        assert "Item Desc" not in mapping
        assert "Vendor Name" not in mapping
        assert "Department" not in mapping
        assert "Part Num" not in mapping  # not exact — should be "Part No" or "Part Number"

    def test_part_number_aliases(self):
        mapping = auto_detect_columns(["Part #", "MPN", "SKU", "stock code"])
        assert mapping["Part #"] == "input_code"
        assert mapping["MPN"] == "input_code"
        assert mapping["SKU"] == "input_code"
        assert mapping["stock code"] == "input_code"

    def test_uom_aliases(self):
        mapping = auto_detect_columns(["UOM", "unit of measure", "pack"])
        assert mapping["UOM"] == "uom"

    def test_unrecognized_headers_omitted(self):
        mapping = auto_detect_columns(["unknown_column", "description"])
        assert "unknown_column" not in mapping
        assert mapping["description"] == "input_description"

    def test_notes_aliases(self):
        mapping = auto_detect_columns(["notes", "comment", "remarks"])
        assert mapping["notes"] == "notes"
        assert mapping["comment"] == "notes"
        assert mapping["remarks"] == "notes"

    def test_department_does_not_match_part(self):
        """'Department' contains 'part' as substring — must NOT match input_code."""
        mapping = auto_detect_columns(["Department", "Part No", "Description"])
        # Department should not match anything (no word-boundary match for "part")
        assert "Department" not in mapping
        # Part No should still match
        assert mapping["Part No"] == "input_code"
        assert mapping["Description"] == "input_description"

    def test_vendor_name_does_not_match_name(self):
        """'Vendor Name' contains 'name' — must NOT match input_description
        because no generic 'name' pattern exists (only 'part name')."""
        mapping = auto_detect_columns(["Vendor Name", "Description"])
        assert "Vendor Name" not in mapping
        assert mapping["Description"] == "input_description"

    def test_purchase_price_does_not_match(self):
        """'Purchase Price' contains no RFQ field pattern — must remain unmapped."""
        mapping = auto_detect_columns(["Purchase Price", "Sell Price", "QTY"])
        assert "Purchase Price" not in mapping
        assert "Sell Price" not in mapping
        assert mapping["QTY"] == "quantity"


# ============================================================================
# Quantity parsing
# ============================================================================

class TestParseQuantity:
    def test_integer(self):
        assert _parse_quantity("5") == 5

    def test_float_string(self):
        assert _parse_quantity("3.0") == 3

    def test_with_tilde(self):
        assert _parse_quantity("~10") == 10

    def test_empty_string(self):
        assert _parse_quantity("") is None

    def test_none(self):
        assert _parse_quantity(None) is None

    def test_non_numeric(self):
        assert _parse_quantity("N/A") is None

    def test_zero(self):
        assert _parse_quantity("0") == 0

    def test_whitespace(self):
        assert _parse_quantity("  42  ") == 42

    def test_already_int(self):
        assert _parse_quantity(5) == 5

    def test_already_float(self):
        assert _parse_quantity(3.0) == 3

    def test_zero_int(self):
        assert _parse_quantity(0) == 0

    def test_negative(self):
        assert _parse_quantity(-1) is None


# ============================================================================
# Constants
# ============================================================================

class TestConstants:
    def test_max_image_size_mb_is_reasonable(self):
        assert 1 <= MAX_IMAGE_SIZE_MB <= 50

    def test_max_batch_size_is_reasonable(self):
        assert 50 <= MAX_BATCH_SIZE <= 1000

    def test_column_patterns_are_comprehensive(self):
        """Every RFQItem field should have at least one pattern."""
        expected_fields = {
            "input_description", "input_code", "brand",
            "quantity", "uom", "notes",
        }
        assert set(_COLUMN_PATTERNS.keys()) == expected_fields


# ============================================================================
# Image extraction (mocked — doesn't call Gemini)
# ============================================================================

class TestExtractItemsFromImage:
    async def test_rejects_oversized_image(self):
        """Image over MAX_IMAGE_SIZE_MB should be rejected before API call."""
        from includes.tools.rfq_item_import import extract_items_from_image

        oversized_len = int((MAX_IMAGE_SIZE_MB + 1) * 1024 * 1024 * 4 / 3)
        fake_b64 = "A" * oversized_len

        result = await extract_items_from_image(fake_b64, "image/png")
        assert result["item_count"] == 0
        assert any("exceeds" in w.lower() for w in result["warnings"])

    def test_column_mapping_remaps_header_keys_to_field_names(self):
        """auto_detect_columns + header_to_field should remap Gemini's keys."""
        from includes.tools.rfq_item_import import auto_detect_columns

        # Simulate what Gemini might return: items keyed by header name
        headers = ["Part #", "Description", "Qty", "Brand"]
        column_mapping = auto_detect_columns([h.lower() for h in headers])

        # Build reverse lookup
        header_to_field = {}
        for h in headers:
            h_lower = h.strip().lower()
            field = column_mapping.get(h_lower)
            if field:
                header_to_field[h.strip()] = field
                header_to_field[h_lower] = field

        # Gemini item (keyed by header names)
        item = {"Part #": "DHP486Z", "Description": "Cordless Drill", "Qty": 5, "Brand": "Makita"}
        remapped = {}
        for key, value in item.items():
            field = header_to_field.get(key.strip().lower())
            if field:
                remapped[field] = value
            else:
                remapped[key] = value

        assert remapped["input_code"] == "DHP486Z"
        assert remapped["input_description"] == "Cordless Drill"
        assert remapped["quantity"] == 5
        assert remapped["brand"] == "Makita"
