import pytest
from unittest.mock import MagicMock, patch
from includes.tools.product_tools import _do_product_search, search_products
from includes.db_models import Product

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
