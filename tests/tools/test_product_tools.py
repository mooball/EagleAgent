import pytest
from datetime import date
from unittest.mock import MagicMock, patch
from includes.tools.product_tools import _do_product_search, _do_part_purchase_history, _do_search_purchase_history, search_products, part_purchase_history, search_purchase_history
from includes.db_models import Product, Supplier, ProductSupplier

@pytest.fixture
def mock_session():
    with patch("includes.tools.product_tools.get_session") as mock_get_session:
        session = MagicMock()
        mock_get_session.return_value = session
        yield session

@pytest.fixture
def mock_embeddings():
    with patch("includes.tools.product_tools.get_embeddings_model") as mock_get_embeddings:
        model = MagicMock()
        # Ensure it returns a dummy vector
        model.embed_query.return_value = [0.1, 0.2, 0.3]
        mock_get_embeddings.return_value = model
        yield model

class TestProductSearch:

    def test_search_no_results(self, mock_session):
        # Setup mock db to return empty list
        mock_query = mock_session.query.return_value
        mock_filtered = mock_query.filter.return_value
        mock_filtered.count.return_value = 0
        mock_filtered.limit.return_value.all.return_value = []
        
        result = _do_product_search(part_number="XYZ-999")
        assert "No products found matching those criteria" in result
        
    def test_search_exact_match(self, mock_session):
        p1 = Product(id=1, part_number="P-123", brand="BrandX", description="A great tool", supplier_code="SX-1")
        
        # When part_number filtering is applied, we chain things
        mock_query = mock_session.query.return_value
        mock_filtered_query = mock_query.filter.return_value
        
        mock_filtered_query.count.return_value = 1
        mock_filtered_query.limit.return_value.all.return_value = [p1]

        result = _do_product_search(part_number="P-1")
        
        assert "Found 1 matching products" in result
        assert "Part Number: P-123" in result
        assert "Brand: BrandX" in result
        assert "Supplier Code: SX-1" in result

    def test_search_truncation_warning(self, mock_session):
        p1 = Product(id=1, part_number="P-1", description="Tool 1")
        p2 = Product(id=2, part_number="P-2", description="Tool 2")
        
        mock_query = mock_session.query.return_value
        # Mock 50 total results, but limit to 2
        mock_query.count.return_value = 50
        mock_query.limit.return_value.all.return_value = [p1, p2]

        result = _do_product_search(limit=2)
        
        assert "Found 50 matching products" in result
        assert "Displaying 2 matches" in result
        assert "There are 48 more unshown results" in result

    def test_search_vector_fallback(self, mock_session, mock_embeddings):
        # 0 string matches, 1 vector match
        p_vector = Product(id=99, part_number="V-1", description="Vector matched tool")
        
        mock_query = mock_session.query.return_value
        
        # We need the direct text search string filter to return 0.
        # Since 'Something ambiguous' is 2 words, .filter() is chained twice.
        mock_filtered = mock_query.filter.return_value.filter.return_value
        mock_filtered.count.return_value = 0
        mock_filtered.limit.return_value.all.return_value = []
        
        # Vector query returns 1
        mock_vector_query = mock_query.order_by.return_value
        mock_vector_query.limit.return_value.all.return_value = [p_vector]

        result = _do_product_search(description="Something ambiguous")
        
        # Assert embeddings model was called
        mock_embeddings.embed_query.assert_called_once_with("Something ambiguous")
        assert "Part Number: V-1" in result

@pytest.mark.asyncio
async def test_async_search_products_tool(mock_session):
    # Just test that the async wrapper works
    mock_query = mock_session.query.return_value
    mock_filtered = mock_query.filter.return_value
    mock_filtered.count.return_value = 0
    mock_filtered.limit.return_value.all.return_value = []
    
    result = await search_products.ainvoke({"part_number": "ABC"})
    assert "No products found" in result


class TestPurchaseHistorySearch:

    def test_no_matching_products(self, mock_session):
        mock_session.query.return_value.filter.return_value.all.return_value = []

        result = _do_part_purchase_history(part_number="NONEXISTENT")
        assert "No products found matching part number" in result

    def test_products_found_but_no_history(self, mock_session):
        p1 = Product(id="uuid-1", part_number="ABC-123", brand="TestBrand")
        mock_session.query.return_value.filter.return_value.all.return_value = [p1]

        # Purchase history query returns empty
        (mock_session.query.return_value
         .join.return_value
         .join.return_value
         .filter.return_value
         .group_by.return_value
         .order_by.return_value
         .limit.return_value
         .all.return_value) = []

        result = _do_part_purchase_history(part_number="ABC-123")
        assert "no purchase history records exist" in result
        assert "ABC-123" in result

    def test_returns_markdown_table(self, mock_session):
        p1 = Product(id="uuid-1", part_number="P-100", brand="BrandA")
        mock_session.query.return_value.filter.return_value.all.return_value = [p1]

        # Mock the aggregated result row
        row = MagicMock()
        row.supplier_id = "supp-uuid-1"
        row.supplier_name = "Acme Tools"
        row.supplier_city = "Sydney"
        row.supplier_country = "Australia"
        row.supplier_contacts = [{"name": "John", "email": "john@acme.com", "phone": "555-1234"}]
        row.part_number = "P-100"
        row.brand = "BrandA"
        row.most_recent_date = date(2026, 1, 15)
        row.total_quantity = 500.0
        row.order_count = 12

        (mock_session.query.return_value
         .join.return_value
         .join.return_value
         .filter.return_value
         .group_by.return_value
         .order_by.return_value
         .limit.return_value
         .all.return_value) = [row]

        # Mock the price subquery
        (mock_session.query.return_value
         .join.return_value
         .filter.return_value
         .order_by.return_value
         .first.return_value) = (42.50,)

        result = _do_part_purchase_history(part_number="P-100")

        assert "Purchase history for part number 'P-100'" in result
        assert "Supplier ID" in result
        assert "Location" in result
        assert "Acme Tools" in result
        assert "$42.50" in result
        assert "15 Jan 2026" in result
        assert "500" in result
        assert "12" in result
        assert "Sydney" in result
        assert "john@acme.com" in result

    def test_multiple_suppliers(self, mock_session):
        p1 = Product(id="uuid-1", part_number="P-200", brand="BrandB")
        mock_session.query.return_value.filter.return_value.all.return_value = [p1]

        row1 = MagicMock()
        row1.supplier_id = "supp-uuid-1"
        row1.supplier_name = "Supplier One"
        row1.supplier_city = "Melbourne"
        row1.supplier_country = "Australia"
        row1.supplier_contacts = None
        row1.part_number = "P-200"
        row1.brand = "BrandB"
        row1.most_recent_date = date(2026, 3, 1)
        row1.total_quantity = 1000.0
        row1.order_count = 20

        row2 = MagicMock()
        row2.supplier_id = "supp-uuid-2"
        row2.supplier_name = "Supplier Two"
        row2.supplier_city = None
        row2.supplier_country = "China"
        row2.supplier_contacts = []
        row2.part_number = "P-200"
        row2.brand = "BrandB"
        row2.most_recent_date = date(2025, 6, 15)
        row2.total_quantity = 200.0
        row2.order_count = 5

        (mock_session.query.return_value
         .join.return_value
         .join.return_value
         .filter.return_value
         .group_by.return_value
         .order_by.return_value
         .limit.return_value
         .all.return_value) = [row1, row2]

        (mock_session.query.return_value
         .join.return_value
         .filter.return_value
         .order_by.return_value
         .first.return_value) = (55.00,)

        result = _do_part_purchase_history(part_number="P-200")

        assert "Found 2 supplier(s)" in result
        assert "Supplier One" in result
        assert "Supplier Two" in result

    def test_handles_null_price_and_date(self, mock_session):
        p1 = Product(id="uuid-1", part_number="P-300", brand=None)
        mock_session.query.return_value.filter.return_value.all.return_value = [p1]

        row = MagicMock()
        row.supplier_id = "supp-uuid-3"
        row.supplier_name = "NullSupplier"
        row.supplier_city = None
        row.supplier_country = None
        row.supplier_contacts = None
        row.part_number = "P-300"
        row.brand = None
        row.most_recent_date = None
        row.total_quantity = None
        row.order_count = 1

        (mock_session.query.return_value
         .join.return_value
         .join.return_value
         .filter.return_value
         .group_by.return_value
         .order_by.return_value
         .limit.return_value
         .all.return_value) = [row]

        (mock_session.query.return_value
         .join.return_value
         .filter.return_value
         .order_by.return_value
         .first.return_value) = None

        result = _do_part_purchase_history(part_number="P-300")

        assert "N/A" in result
        assert "NullSupplier" in result


@pytest.mark.asyncio
async def test_async_part_purchase_history_tool(mock_session):
    mock_session.query.return_value.filter.return_value.all.return_value = []

    result = await part_purchase_history.ainvoke({"part_number": "XYZ"})
    assert "No products found" in result


class TestSearchPurchaseHistory:

    def test_no_filters_returns_summary(self, mock_session):
        stats = MagicMock()
        stats.total_records = 102880
        stats.total_pos = 5432
        stats.total_products = 8900
        stats.total_suppliers = 150
        stats.earliest_date = date(2020, 1, 15)
        stats.latest_date = date(2026, 3, 25)

        # count() for initial query
        (mock_session.query.return_value
         .join.return_value
         .join.return_value
         .count.return_value) = 102880

        # stats query
        mock_session.query.return_value.first.return_value = stats

        result = _do_search_purchase_history()

        assert "Purchase history database summary" in result
        assert "102,880" in result
        assert "5,432" in result

    def test_no_results_with_filter(self, mock_session):
        (mock_session.query.return_value
         .join.return_value
         .join.return_value
         .filter.return_value
         .count.return_value) = 0

        result = _do_search_purchase_history(supplier="NonExistent")
        assert "No purchase history records found" in result
        assert "NonExistent" in result

    def test_invalid_date_format(self, mock_session):
        result = _do_search_purchase_history(date_from="25/03/2026")
        assert "Invalid date_from format" in result

    def test_filtered_results_returns_table(self, mock_session):
        mock_query = (mock_session.query.return_value
         .join.return_value
         .join.return_value
         .filter.return_value)
        mock_query.count.return_value = 1

        row = MagicMock()
        row.po_number = "P158740"
        row.date = date(2026, 3, 10)
        row.quantity = 16.0
        row.price = 236.68
        row.status = "Pending Receipt"
        row.part_number = "ABC-123"
        row.brand = "TestBrand"
        row.supplier_name = "Acme Supplies"

        (mock_query
         .order_by.return_value
         .limit.return_value
         .all.return_value) = [row]

        result = _do_search_purchase_history(supplier="Acme")

        assert "Purchase history search" in result
        assert "P158740" in result
        assert "ABC-123" in result
        assert "Acme Supplies" in result
        assert "$236.68" in result


@pytest.mark.asyncio
async def test_async_search_purchase_history_tool(mock_session):
    # No-filter call: mock the count and stats
    (mock_session.query.return_value
     .join.return_value
     .join.return_value
     .count.return_value) = 100

    stats = MagicMock()
    stats.total_records = 100
    stats.total_pos = 10
    stats.total_products = 50
    stats.total_suppliers = 5
    stats.earliest_date = date(2025, 1, 1)
    stats.latest_date = date(2026, 3, 25)
    mock_session.query.return_value.first.return_value = stats

    result = await search_purchase_history.ainvoke({})
    assert "Purchase history database summary" in result
