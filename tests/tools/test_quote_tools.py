"""Tests for RFQ (Request for Quote) management tools.

Tests create, update, supplier operations, assignment, status changes,
note appending, external linking, and query/filter via SQL tables.
"""

import pytest
from unittest.mock import patch
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from includes.dashboard.models import Base, RFQ, RFQItem
from includes.tools.quote_tools import (
    create_quote_tools, _notify_rfq_updated, _render_rfq_summary,
    _enrich_supplier_pricing,
)


@pytest.fixture
def db_session():
    """Create a test DB session using the project's PostgreSQL database.

    Uses a SAVEPOINT so that session.commit() inside sync helpers doesn't
    end the outer transaction — everything rolls back at the end.
    """
    from includes.dashboard.database import _sync_url
    engine = create_engine(_sync_url(), pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection)
    session = Session(bind=connection)

    # Start a nested savepoint so that commit() inside the helpers
    # only releases the savepoint, not the outer transaction.
    session.begin_nested()

    # After each commit (savepoint release), start a new savepoint
    from sqlalchemy import event

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess, trans):
        if trans.nested and not trans._parent.nested:
            sess.begin_nested()

    # Prevent the sync helpers from actually closing our test session
    session.close = lambda: None

    yield session

    transaction.rollback()
    connection.close()


@pytest.fixture
def rfq_tools(db_session, test_user_id):
    """Create RFQ tools with mocked DB session."""
    with patch("includes.tools.quote_tools._get_session", return_value=db_session):
        tools = create_quote_tools(test_user_id)
    return tools


@pytest.fixture
def manage(rfq_tools):
    return rfq_tools[0]


@pytest.fixture
def get(rfq_tools):
    return rfq_tools[1]


# ---- helpers ----

async def _create_sample_rfq(manage, db_session, **overrides):
    """Create a basic RFQ and return the result string."""
    data = {
        "customer": "Acme Construction",
        "customer_contact": {"name": "John Smith", "email": "john@acme.com.au"},
        "items": [
            {"input_description": "Cordless drill", "input_code": "DHP486Z", "quantity": 4},
            {"input_description": "Dumpy level", "input_code": "Topcon brand", "quantity": 1},
        ],
    }
    data.update(overrides)
    with patch("includes.tools.quote_tools._get_session", return_value=db_session):
        return await manage.ainvoke({"action": "create", "data": data})


def _get_rfq_from_db(db_session, customer: str = "Acme Construction") -> RFQ:
    """Get the most recently created RFQ matching the customer name."""
    return db_session.query(RFQ).filter(
        RFQ.customer == customer
    ).order_by(RFQ.rfq_number.desc()).first()


# ===========================================================================
# manage_rfq — create
# ===========================================================================

class TestManageRfqCreate:
    async def test_create_basic(self, manage, db_session):
        result = await _create_sample_rfq(manage, db_session)
        assert "RFQ-" in result
        assert "Acme Construction" in result
        assert "Cordless drill" in result
        assert "Dumpy level" in result

    async def test_create_assigns_sequential_ids(self, manage, db_session):
        pre_count = db_session.query(RFQ).count()
        r1 = await _create_sample_rfq(manage, db_session)
        r2 = await _create_sample_rfq(manage, db_session, customer="Beta Corp")
        rfqs = db_session.query(RFQ).order_by(RFQ.rfq_number).all()
        assert len(rfqs) == pre_count + 2
        # The two newest RFQs should have sequential numbers
        new_rfqs = rfqs[-2:]
        num1 = int(new_rfqs[0].rfq_number.split("-")[-1])
        num2 = int(new_rfqs[1].rfq_number.split("-")[-1])
        assert num2 == num1 + 1

    async def test_create_requires_customer(self, manage, db_session):
        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            result = await manage.ainvoke({"action": "create", "data": {}})
        assert "error" in result.lower()

    async def test_create_sets_default_status_draft(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)
        assert rfq.status == "draft"

    async def test_create_records_history(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)
        assert len(rfq.history) == 1
        assert "Created RFQ with 2 items" in rfq.history[0]["action"]

    async def test_create_stores_customer_contact(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)
        assert rfq.customer_contact["name"] == "John Smith"
        assert rfq.customer_contact["email"] == "john@acme.com.au"

    async def test_create_items_stored(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)
        items = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).order_by(RFQItem.line).all()
        assert len(items) == 2
        assert items[0].line == 1
        assert items[0].status == "unidentified"
        assert items[1].line == 2


# ===========================================================================
# manage_rfq — update_item
# ===========================================================================

class TestManageRfqUpdateItem:
    async def test_update_item_part_number(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "update_item",
                "rfq_id": rfq.rfq_number,
                "data": {"line": 1, "part_number": "DHP486Z", "brand": "Makita", "status": "confirmed"},
            })
        assert "Confirmed" in result or "confirmed" in result.lower()

        db_session.expire_all()
        item = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id, RFQItem.line == 1).first()
        assert item.part_number == "DHP486Z"
        assert item.brand == "Makita"
        assert item.status == "confirmed"

    async def test_update_item_missing_line(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "update_item", "rfq_id": rfq.rfq_number, "data": {"part_number": "X"},
            })
        assert "error" in result.lower()

    async def test_update_item_invalid_line(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "update_item", "rfq_id": rfq.rfq_number, "data": {"line": 99},
            })
        assert "error" in result.lower()

    async def test_update_item_line_number_alias(self, manage, db_session):
        """LLMs sometimes use 'line_number' instead of 'line'."""
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "update_item",
                "rfq_id": rfq.rfq_number,
                "data": {"line_number": 1, "part_number": "DHP486Z", "status": "confirmed"},
            })
        assert "error" not in result.lower()


# ===========================================================================
# manage_rfq — add_supplier / update_supplier
# ===========================================================================

class TestManageRfqSuppliers:
    async def test_add_supplier(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "add_supplier",
                "rfq_id": rfq.rfq_number,
                "data": {
                    "line": 1,
                    "name": "Sydney Tools",
                    "price": 189.00,
                    "contacts": [{"type": "email", "value": "sales@sydneytools.com.au"}],
                },
            })
        assert "Sydney Tools" in result

        db_session.expire_all()
        item = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id, RFQItem.line == 1).first()
        assert len(item.suppliers) == 1
        assert item.suppliers[0]["name"] == "Sydney Tools"
        assert item.suppliers[0]["price"] == 189.00

    async def test_add_multiple_suppliers_batch(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "add_supplier",
                "rfq_id": rfq.rfq_number,
                "data": {
                    "line": 1,
                    "suppliers": [
                        {"name": "Sydney Tools", "price": 189.00, "contacts": [{"email": "info@sydneytools.com.au"}]},
                        {"name": "Total Tools", "price": 195.00, "contacts": [{"email": "info@totaltools.com.au"}]},
                        {"name": "ToolMart Online", "contacts": [{"url": "https://toolmart.com.au"}]},
                    ],
                },
            })
        assert "Sydney Tools" in result
        assert "Total Tools" in result
        assert "ToolMart Online" in result

        db_session.expire_all()
        item = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id, RFQItem.line == 1).first()
        assert len(item.suppliers) == 3

    async def test_update_supplier_status(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "add_supplier",
                "rfq_id": rfq.rfq_number,
                "data": {"line": 1, "name": "Sydney Tools", "contacts": [{"email": "info@sydneytools.com.au"}]},
            })
            result = await manage.ainvoke({
                "action": "update_supplier",
                "rfq_id": rfq.rfq_number,
                "data": {"line": 1, "name": "Sydney Tools", "status": "shortlisted", "price": 200.0},
            })
        assert "Sydney Tools" in result

        db_session.expire_all()
        item = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id, RFQItem.line == 1).first()
        assert item.suppliers[0]["status"] == "shortlisted"
        assert item.suppliers[0]["price"] == 200.0

    async def test_update_supplier_not_found(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "update_supplier",
                "rfq_id": rfq.rfq_number,
                "data": {"line": 1, "name": "Nonexistent"},
            })
        assert "error" in result.lower()

    async def test_add_supplier_missing_name(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "add_supplier",
                "rfq_id": rfq.rfq_number,
                "data": {"line": 1},
            })
        assert "error" in result.lower()


# ===========================================================================
# manage_rfq — assign, update_status, add_note, link_external
# ===========================================================================

class TestManageRfqMisc:
    async def test_assign(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "assign", "rfq_id": rfq.rfq_number,
                "data": {"assigned_to": "sarah@eagle.com.au"},
            })
        db_session.expire_all()
        rfq = _get_rfq_from_db(db_session)
        assert rfq.assigned_to == "sarah@eagle.com.au"

    async def test_update_status(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "update_status", "rfq_id": rfq.rfq_number,
                "data": {"status": "in_progress"},
            })
        db_session.expire_all()
        rfq = _get_rfq_from_db(db_session)
        assert rfq.status == "in_progress"

    async def test_update_status_invalid(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "update_status", "rfq_id": rfq.rfq_number,
                "data": {"status": "bogus"},
            })
        assert "error" in result.lower()

    async def test_add_note(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "add_note", "rfq_id": rfq.rfq_number,
                "data": {"note": "Urgent — needed by end of month"},
            })
        db_session.expire_all()
        rfq = _get_rfq_from_db(db_session)
        assert "Urgent" in rfq.notes

    async def test_add_note_appends(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "add_note", "rfq_id": rfq.rfq_number,
                "data": {"note": "First note"},
            })
            await manage.ainvoke({
                "action": "add_note", "rfq_id": rfq.rfq_number,
                "data": {"note": "Second note"},
            })
        db_session.expire_all()
        rfq = _get_rfq_from_db(db_session)
        assert "First note" in rfq.notes
        assert "Second note" in rfq.notes

    async def test_link_external_netsuite(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "link_external", "rfq_id": rfq.rfq_number,
                "data": {"netsuite_opportunity": "OPP-12345"},
            })
        db_session.expire_all()
        rfq = _get_rfq_from_db(db_session)
        assert rfq.netsuite_opportunity == "OPP-12345"

    async def test_link_external_empty(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "link_external", "rfq_id": rfq.rfq_number, "data": {},
            })
        assert "error" in result.lower()

    async def test_unknown_action(self, manage, db_session):
        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            result = await manage.ainvoke({"action": "explode"})
        assert "error" in result.lower()

    async def test_rfq_not_found(self, manage, db_session):
        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "update_status", "rfq_id": "RFQ-9999-0000",
                "data": {"status": "draft"},
            })
        assert "not found" in result.lower()

    async def test_history_accumulates(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "update_status", "rfq_id": rfq.rfq_number,
                "data": {"status": "in_progress"},
            })
            await manage.ainvoke({
                "action": "assign", "rfq_id": rfq.rfq_number,
                "data": {"assigned_to": "sarah@eagle.com.au"},
            })
        db_session.expire_all()
        rfq = _get_rfq_from_db(db_session)
        assert len(rfq.history) == 3  # create + status + assign


# ===========================================================================
# get_rfq
# ===========================================================================

class TestGetRfq:
    async def test_get_single(self, manage, get, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            result = await get.ainvoke({"rfq_id": rfq.rfq_number})
        assert rfq.rfq_number in result
        assert "Acme Construction" in result

    async def test_get_not_found(self, get, db_session):
        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            result = await get.ainvoke({"rfq_id": "RFQ-0000-0000"})
        assert "not found" in result.lower()

    async def test_list_all(self, manage, get, db_session):
        await _create_sample_rfq(manage, db_session)
        await _create_sample_rfq(manage, db_session, customer="Beta Corp")

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            result = await get.ainvoke({"list_all": True})
        assert "Acme Construction" in result
        assert "Beta Corp" in result
        assert "RFQs total" in result

    async def test_filter_by_status(self, manage, get, db_session):
        await _create_sample_rfq(manage, db_session, customer="FilterTestCo")
        rfq = _get_rfq_from_db(db_session, customer="FilterTestCo")

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "update_status", "rfq_id": rfq.rfq_number,
                "data": {"status": "awaiting_quotes"},
            })
            await _create_sample_rfq(manage, db_session, customer="Beta Corp")
            result = await get.ainvoke({"status": "awaiting_quotes"})
        assert "FilterTestCo" in result
        assert "Beta" not in result

    async def test_default_shows_my_rfqs(self, manage, get, db_session, test_user_id):
        await _create_sample_rfq(manage, db_session)
        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            result = await get.ainvoke({})
        assert "Acme" in result


# ===========================================================================
# Rendering
# ===========================================================================

class TestRendering:
    async def test_summary_shows_supplier_price(self, manage, db_session):
        await _create_sample_rfq(manage, db_session)
        rfq = _get_rfq_from_db(db_session)

        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "add_supplier",
                "rfq_id": rfq.rfq_number,
                "data": {"line": 1, "name": "Sydney Tools", "price": 189.0, "price_type": "previous_purchase", "contacts": [{"email": "info@sydneytools.com.au"}]},
            })
            result = await manage.ainvoke({
                "action": "add_supplier",
                "rfq_id": rfq.rfq_number,
                "data": {"line": 1, "name": "Total Tools", "status": "dropped", "contacts": [{"email": "info@totaltools.com.au"}]},
            })
        assert "$189.00" in result
        assert "prev" in result
        assert "~~Total Tools~~" in result

    async def test_summary_shows_contact(self, manage, db_session):
        result = await _create_sample_rfq(manage, db_session)
        assert "John Smith" in result
        assert "john@acme.com.au" in result

    async def test_notify_rfq_updated_no_chainlit_context(self):
        """_notify_rfq_updated gracefully skips when not in a Chainlit session."""
        await _notify_rfq_updated()


# ===========================================================================
# Pricing enrichment
# ===========================================================================

class TestPricingEnrichment:
    """Test _enrich_supplier_pricing populates cost/sale/quote fields."""

    @pytest.fixture
    def pricing_data(self, db_session):
        """Create a product, supplier, and transactions for pricing tests."""
        import uuid
        import datetime
        from includes.dashboard.models import Product, Supplier, Transaction

        product = Product(
            id=uuid.uuid4(),
            part_number="TEST-PRICE-001",
            description="Test product for pricing",
        )
        supplier = Supplier(
            id=uuid.uuid4(),
            name="Test Pricing Supplier",
            contacts=[{"email": "test@pricing.com"}],
        )
        db_session.add_all([product, supplier])
        db_session.flush()

        # SalesOrder — most recent, cost=64.81, price=87.49
        so = Transaction(
            id=uuid.uuid4(),
            doc_number="SO-TEST-001",
            doc_type="SalesOrder",
            product_id=product.id,
            supplier_id=supplier.id,
            quantity=5,
            price=87.49,
            cost=64.81,
            date=datetime.date(2025, 4, 10),
        )
        # Quote — older, cost=55.00, price=140.00
        qt = Transaction(
            id=uuid.uuid4(),
            doc_number="QT-TEST-001",
            doc_type="Quote",
            product_id=product.id,
            supplier_id=supplier.id,
            quantity=20,
            price=140.00,
            cost=55.00,
            date=datetime.date(2025, 3, 1),
        )
        db_session.add_all([so, qt])
        db_session.flush()

        return {"product": product, "supplier": supplier}

    def test_enrich_uses_most_recent_transaction(self, db_session, pricing_data):
        """Should use the most recent SO or Quote for both cost and sale."""
        suppliers = [{"supplier_id": str(pricing_data["supplier"].id), "name": "Test Pricing Supplier"}]
        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            _enrich_supplier_pricing(suppliers, str(pricing_data["product"].id))
        # SO is most recent (2025-04-10 vs 2025-03-01)
        assert suppliers[0]["cost_price"] == 64.81
        assert suppliers[0]["sale_price"] == 87.49
        assert suppliers[0]["price_doc"] == "SO-TEST-001"
        assert suppliers[0]["price_doc_type"] == "SalesOrder"
        assert suppliers[0]["price_date"] == "2025-04-10"

    def test_enrich_uses_quote_when_most_recent(self, db_session):
        """When a Quote is newer than any SO, use it."""
        import uuid
        import datetime
        from includes.dashboard.models import Product, Supplier, Transaction

        product = Product(id=uuid.uuid4(), part_number="QT-NEWEST", description="Quote newest")
        supplier = Supplier(id=uuid.uuid4(), name="Quote Supplier", contacts=[])
        db_session.add_all([product, supplier])
        db_session.flush()

        qt = Transaction(
            id=uuid.uuid4(), doc_number="QT-NEW-001", doc_type="Quote",
            product_id=product.id, supplier_id=supplier.id,
            quantity=10, price=200.00, cost=120.00, date=datetime.date(2026, 6, 1),
        )
        so = Transaction(
            id=uuid.uuid4(), doc_number="SO-OLD-001", doc_type="SalesOrder",
            product_id=product.id, supplier_id=supplier.id,
            quantity=3, price=180.00, cost=100.00, date=datetime.date(2026, 1, 1),
        )
        db_session.add_all([qt, so])
        db_session.flush()

        suppliers = [{"supplier_id": str(supplier.id), "name": "Quote Supplier"}]
        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            _enrich_supplier_pricing(suppliers, str(product.id))
        assert suppliers[0]["cost_price"] == 120.00
        assert suppliers[0]["sale_price"] == 200.00
        assert suppliers[0]["price_doc"] == "QT-NEW-001"
        assert suppliers[0]["price_doc_type"] == "Quote"

    def test_enrich_counts_transactions(self, db_session, pricing_data):
        suppliers = [{"supplier_id": str(pricing_data["supplier"].id), "name": "Test Pricing Supplier"}]
        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            _enrich_supplier_pricing(suppliers, str(pricing_data["product"].id))
        assert suppliers[0]["transaction_count"] == 2  # SO + Quote

    def test_enrich_skips_without_product_id(self, db_session, pricing_data):
        suppliers = [{"supplier_id": str(pricing_data["supplier"].id), "name": "Test Pricing Supplier"}]
        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            _enrich_supplier_pricing(suppliers, None)
        assert "cost_price" not in suppliers[0]

    def test_enrich_skips_without_supplier_id(self, db_session, pricing_data):
        suppliers = [{"name": "Unknown Supplier"}]
        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            _enrich_supplier_pricing(suppliers, str(pricing_data["product"].id))
        assert "cost_price" not in suppliers[0]

    def test_enrich_ignores_purchase_orders(self, db_session):
        """PurchaseOrders should be completely ignored by enrichment."""
        import uuid
        import datetime
        from includes.dashboard.models import Product, Supplier, Transaction

        product = Product(id=uuid.uuid4(), part_number="PO-ONLY", description="PO only product")
        supplier = Supplier(id=uuid.uuid4(), name="PO Supplier", contacts=[])
        db_session.add_all([product, supplier])
        db_session.flush()

        po = Transaction(
            id=uuid.uuid4(), doc_number="PO-001", doc_type="PurchaseOrder",
            product_id=product.id, supplier_id=supplier.id,
            quantity=10, price=100.00, cost=50.00, date=datetime.date(2026, 5, 1),
        )
        db_session.add(po)
        db_session.flush()

        suppliers = [{"supplier_id": str(supplier.id), "name": "PO Supplier"}]
        with patch("includes.tools.quote_tools._get_session", return_value=db_session):
            _enrich_supplier_pricing(suppliers, str(product.id))
        assert "cost_price" not in suppliers[0]
        assert "sale_price" not in suppliers[0]

    def test_render_shows_cost_sale_margin(self, db_session, pricing_data):
        """Verify _render_rfq_summary shows cost/sale/margin when present."""
        rfq_dict = {
            "id": "RFQ-2025-0099",
            "customer": "Test Margin Corp",
            "status": "draft",
            "assigned_to": "tester",
            "items": [{
                "line": 1,
                "input_description": "Widget",
                "part_number": "W-001",
                "quantity": 5,
                "status": "confirmed",
                "suppliers": [
                    {"name": "Sup A", "cost_price": 50.0, "sale_price": 100.0, "status": "candidate"},
                    {"name": "Sup B", "cost_price": 80.0, "sale_price": 100.0, "status": "candidate"},
                    {"name": "Sup C", "status": "dropped"},
                ],
            }],
        }
        result = _render_rfq_summary(rfq_dict)
        assert "cost $50.00" in result
        assert "sale $100.00" in result
        assert "50%" in result  # (100-50)/100 margin for Sup A
        assert "20%" in result  # (100-80)/100 margin for Sup B
        assert "~~Sup C~~" in result
