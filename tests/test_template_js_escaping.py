"""Guards against JS string-literal interpolation of server-side data.

Server values (supplier names, contact emails, part numbers) are external,
persisted data. They must reach Alpine via HTML-escaped ``data-*`` attributes and
be read back with ``$el.dataset`` — never interpolated into a JS string literal,
where a backslash or quote can break out of the expression. This covers the
Copilot review findings on the RFQ Selection/Quotation templates and the items
table's copy-email / price-history controls.
"""

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"

# The manual escaping idiom the templates must not use again:
#   replace("'", "\\'")
_FRAGILE_ESCAPE_IDIOM = 'replace("' + "'" + '", "' + "\\\\" + "'" + '")'


def _template_files():
    return sorted(TEMPLATES.rglob("*.html"))


def test_no_manual_apostrophe_escaping_in_templates():
    offenders = [
        str(p.relative_to(TEMPLATES.parent))
        for p in _template_files()
        if _FRAGILE_ESCAPE_IDIOM in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        "Templates still escape apostrophes to interpolate server data into JS "
        f"string literals: {offenders}. Use an HTML-escaped data-* attribute and "
        "$el.dataset instead."
    )


def _render_items_table(supplier, item_overrides=None):
    from includes.dashboard.routes._helpers import templates

    item = {
        "line": 1,
        "input_description": "M16 bolt",
        "part_number": "AB-1'\\x",
        "brand": "Acme",
        "quantity": 4,
        "uom": "ea",
        "match": "unmatched",
        "cost_price": None,
        "sale_price": None,
        "product_id": "11111111-1111-1111-1111-111111111111",
        "suppliers": [supplier],
    }
    item.update(item_overrides or {})
    return templates.env.get_template("partials/_rfq_items_table.html").render(
        table_items=[item],
        rfq={"id": "RFQ-1"},
        show_add_row=True,
        pipeline_active=False,
        departments=[],
    )


class TestItemsTableJsEscaping:
    def test_copy_email_uses_dataset(self):
        html = _render_items_table({
            "name": "Acme",
            "supplier_id": "22222222-2222-2222-2222-222222222222",
            "status": "shortlisted",
            "contacts": [{"email": "o'brien@example.com", "label": "Main"}],
        })
        assert "$el.dataset.email" in html
        assert 'data-email="o&#39;brien@example.com"' in html
        # The old literal interpolation must be gone.
        assert "writeText('o" not in html

    def test_price_history_url_uses_dataset_and_is_encoded(self):
        html = _render_items_table({
            "name": "Acme",
            "supplier_id": "22222222-2222-2222-2222-222222222222",
            "status": "shortlisted",
            "cost_price": 5.0,
        })
        assert "$el.dataset.priceHistoryUrl" in html
        match = re.search(r'data-price-history-url="([^"]*)"', html)
        assert match, "price-history button must carry its URL in a data attribute"
        url = match.group(1)
        # Part number is percent-encoded; the query separators stay HTML entities.
        assert "part_number=AB-1%27%5Cx" in url
        assert "fetch('/partial/rfqs/price-history" not in html

    def test_price_history_absent_without_product(self):
        html = _render_items_table(
            {
                "name": "Acme",
                "supplier_id": "22222222-2222-2222-2222-222222222222",
                "status": "shortlisted",
                "cost_price": 5.0,
            },
            item_overrides={"product_id": None},
        )
        assert "data-price-history-url" not in html
