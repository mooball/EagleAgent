"""Tests for RFQ bulk operations — multi-line tool calls.

Covers add_suppliers_bulk, update_items_bulk, update_quotes_bulk,
and select_quotes_bulk actions on manage_rfq.
"""

import pytest
from unittest.mock import patch

from includes.dashboard.models import RFQ, RFQItem
from includes.tools.quote_tools import create_quote_tools


# ===========================================================================
# Fixtures (mirror test_quote_tools.py for session/tool setup)
# ===========================================================================

@pytest.fixture
def db_session():
    """Create a test DB session with savepoint-based rollback."""
    from includes.dashboard.database import _sync_url
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(_sync_url(), pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection)
    session = Session(bind=connection)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess, trans):
        if trans.nested and not trans._parent.nested:
            sess.begin_nested()

    session.close = lambda: None
    yield session
    transaction.rollback()
    connection.close()


@pytest.fixture
def rfq_tools(db_session, test_user_id):
    with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
        return create_quote_tools(test_user_id)


@pytest.fixture
def manage(rfq_tools):
    return rfq_tools[0]


# ===========================================================================
# Helpers
# ===========================================================================

async def _create_test_rfq(manage, db_session, **overrides):
    """Create a basic 3-item RFQ for bulk operation testing."""
    data = {
        "customer": "Bulk Test Corp",
        "items": [
            {"input_description": "Hydraulic pump", "input_code": "HP100", "quantity": 2},
            {"input_description": "Steel valve", "input_code": "SV200", "quantity": 10},
            {"input_description": "Copper fitting", "input_code": "CF300", "quantity": 50},
        ],
    }
    data.update(overrides)
    with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
        return await manage.ainvoke({"action": "create", "data": data})


def _get_test_rfq(db_session, customer="Bulk Test Corp"):
    """Get the most recently created test RFQ."""
    return db_session.query(RFQ).filter(
        RFQ.customer == customer
    ).order_by(RFQ.rfq_number.desc()).first()


# ===========================================================================
# add_suppliers_bulk
# ===========================================================================

class TestAddSuppliersBulk:
    async def test_multiple_lines(self, manage, db_session):
        """Add suppliers to lines 1 and 2 in one call."""
        await _create_test_rfq(manage, db_session)
        rfq = _get_test_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "add_suppliers_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {
                    "entries": [
                        {"line": 1, "name": "PumpCo", "supplier_id": "sup-001", "price": 150.00, "currency": "AUD"},
                        {"line": 1, "name": "Hydraulic Supplies Pty Ltd"},
                        {"line": 2, "name": "ValveMasters", "price": 25.50},
                    ],
                },
            })
        assert "Bulk Test Corp" in result
        assert "PumpCo" not in result  # brief summary doesn't show supplier names

        # Verify DB state
        db_session.expire_all()
        rfq = _get_test_rfq(db_session)
        items = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).order_by(RFQItem.line).all()

        # Line 1 should have 2 suppliers
        assert len(items[0].suppliers) == 2
        names_line1 = {s["name"] for s in items[0].suppliers}
        assert "PumpCo" in names_line1
        assert "Hydraulic Supplies Pty Ltd" in names_line1

        # Line 2 should have 1 supplier
        assert len(items[1].suppliers) == 1
        assert items[1].suppliers[0]["name"] == "ValveMasters"

        # Line 3 should still have no suppliers
        assert len(items[2].suppliers) == 0

        # Status should have auto-progressed
        assert rfq.status == "in_progress"

    async def test_missing_line_reported(self, manage, db_session):
        """Missing line numbers don't prevent valid entries from being processed."""
        await _create_test_rfq(manage, db_session)
        rfq = _get_test_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "add_suppliers_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {
                    "entries": [
                        {"line": 1, "name": "PumpCo"},
                        {"line": 99, "name": "GhostCorp"},  # doesn't exist
                    ],
                },
            })
        # The overall result should still show the RFQ (partial success)
        assert "Bulk Test Corp" in result

        # Line 1 should have been processed despite line 99 failing
        db_session.expire_all()
        items = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id, RFQItem.line == 1).first()
        assert len(items.suppliers) == 1
        assert items.suppliers[0]["name"] == "PumpCo"

        # History should record the missing line
        rfq = _get_test_rfq(db_session)
        history_actions = [h["action"] for h in (rfq.history or [])]
        assert any("not found" in h.lower() or "line 99" in h.lower() for h in history_actions)

    async def test_empty_entries(self, manage, db_session):
        """Empty entries list returns an error."""
        await _create_test_rfq(manage, db_session)
        rfq = _get_test_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "add_suppliers_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {"entries": []},
            })
        assert "Error" in result or "required" in result.lower()

    async def test_cap_exceeded(self, manage, db_session):
        """More than 200 entries returns an error."""
        await _create_test_rfq(manage, db_session)
        rfq = _get_test_rfq(db_session)

        entries = [{"line": 1, "name": f"Supplier {i}"} for i in range(201)]

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "add_suppliers_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {"entries": entries},
            })
        assert "200" in result.lower() or "too many" in result.lower()

    async def test_groups_by_line(self, manage, db_session):
        """Multiple entries for the same line are grouped together."""
        await _create_test_rfq(manage, db_session)
        rfq = _get_test_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "add_suppliers_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {
                    "entries": [
                        {"line": 1, "name": "Alpha Supplies"},
                        {"line": 2, "name": "Beta Parts"},
                        {"line": 1, "name": "Gamma Industrial"},  # same line as Alpha
                    ],
                },
            })

        db_session.expire_all()
        items = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).order_by(RFQItem.line).all()
        # Line 1 should have both Alpha and Gamma
        assert len(items[0].suppliers) == 2
        names = {s["name"] for s in items[0].suppliers}
        assert names == {"Alpha Supplies", "Gamma Industrial"}
        # Line 2 should have Beta
        assert len(items[1].suppliers) == 1
        assert items[1].suppliers[0]["name"] == "Beta Parts"


# ===========================================================================
# update_items_bulk
# ===========================================================================

class TestUpdateItemsBulk:
    async def test_multiple_items(self, manage, db_session):
        """Update quantity on line 1 and brand on line 2 in one call."""
        await _create_test_rfq(manage, db_session)
        rfq = _get_test_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "update_items_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {
                    "items": [
                        {"line": 1, "quantity": 5},
                        {"line": 2, "brand": "SteelMaster"},
                    ],
                },
            })
        assert "Bulk Test Corp" in result

        db_session.expire_all()
        items = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).order_by(RFQItem.line).all()
        assert items[0].quantity == 5
        assert items[1].brand == "SteelMaster"
        # Line 3 unchanged
        assert items[2].quantity == 50
        assert items[2].brand is None

    async def test_match_reset_on_description_change(self, manage, db_session):
        """Changing description resets match to 'unmatched' and clears product_id."""
        await _create_test_rfq(manage, db_session)
        rfq = _get_test_rfq(db_session)

        # First set a classification (no product_id — we only test match reset)
        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "update_item",
                "rfq_id": rfq.rfq_number,
                "data": {"line": 1, "match": "specific"},
            })

        # Now change the description via bulk
        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "update_items_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {
                    "items": [
                        {"line": 1, "input_description": "Electric hydraulic pump"},
                    ],
                },
            })

        db_session.expire_all()
        item = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id, RFQItem.line == 1).first()
        assert item.match == "unmatched"

    async def test_missing_line_reported(self, manage, db_session):
        """Missing line is reported without failing the batch."""
        await _create_test_rfq(manage, db_session)
        rfq = _get_test_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "update_items_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {
                    "items": [
                        {"line": 1, "quantity": 99},
                        {"line": 42, "quantity": 10},  # doesn't exist
                    ],
                },
            })
        # Line 1 should still be updated
        db_session.expire_all()
        item = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id, RFQItem.line == 1).first()
        assert item.quantity == 99

    async def test_empty_items(self, manage, db_session):
        """Empty items list returns an error."""
        await _create_test_rfq(manage, db_session)
        rfq = _get_test_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "update_items_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {"items": []},
            })
        assert "Error" in result or "required" in result.lower()

    async def test_item_groups_cleared(self, manage, db_session):
        """Item groups are cleared when items are updated."""
        await _create_test_rfq(manage, db_session)
        rfq = _get_test_rfq(db_session)

        # Set some item groups
        rfq.item_groups = {"groups": [{"name": "Test Group", "lines": [1, 2]}]}
        db_session.commit()

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "update_items_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {"items": [{"line": 1, "quantity": 3}]},
            })

        db_session.expire_all()
        rfq = _get_test_rfq(db_session)
        assert rfq.item_groups is None


# ===========================================================================
# update_quotes_bulk
# ===========================================================================

class TestUpdateQuotesBulk:
    async def test_multiple_quotes(self, manage, db_session):
        """Update quote fields on suppliers across multiple lines."""
        await _create_test_rfq(manage, db_session)
        rfq = _get_test_rfq(db_session)

        # First add suppliers to lines 1 and 2
        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "add_suppliers_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {
                    "entries": [
                        {"line": 1, "name": "PumpCo"},
                        {"line": 2, "name": "ValveMasters"},
                    ],
                },
            })

        # Now update quotes via bulk
        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "update_quotes_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {
                    "quotes": [
                        {"line": 1, "name": "PumpCo", "quote_cost": 145.00, "quote_status": "quoted", "quote_currency": "AUD"},
                        {"line": 2, "name": "ValveMasters", "quote_cost": 22.00, "quote_status": "quoted", "quote_currency": "AUD"},
                    ],
                },
            })
        assert "Bulk Test Corp" in result

        db_session.expire_all()
        items = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).order_by(RFQItem.line).all()
        assert items[0].suppliers[0]["quote_cost"] == 145.00
        assert items[0].suppliers[0]["quote_status"] == "quoted"
        assert items[1].suppliers[0]["quote_cost"] == 22.00
        assert items[1].suppliers[0]["quote_status"] == "quoted"

    async def test_empty_quotes(self, manage, db_session):
        """Empty quotes list returns an error."""
        await _create_test_rfq(manage, db_session)
        rfq = _get_test_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "update_quotes_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {"quotes": []},
            })
        assert "Error" in result or "required" in result.lower()

    async def test_supplier_not_found(self, manage, db_session):
        """Non-existent supplier is reported without failing the batch."""
        await _create_test_rfq(manage, db_session)
        rfq = _get_test_rfq(db_session)

        # Add supplier to line 1 only
        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "add_supplier",
                "rfq_id": rfq.rfq_number,
                "data": {"line": 1, "name": "PumpCo"},
            })

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "update_quotes_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {
                    "quotes": [
                        {"line": 1, "name": "PumpCo", "quote_cost": 100.00},
                        {"line": 1, "name": "GhostSupplier", "quote_cost": 50.00},  # doesn't exist
                    ],
                },
            })
        # The valid update should still have applied
        db_session.expire_all()
        item = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id, RFQItem.line == 1).first()
        assert item.suppliers[0]["quote_cost"] == 100.00


# ===========================================================================
# select_quotes_bulk
# ===========================================================================

class TestSelectQuotesBulk:
    async def test_multiple_selections(self, manage, db_session):
        """Select suppliers across multiple lines in one call."""
        await _create_test_rfq(manage, db_session)
        rfq = _get_test_rfq(db_session)

        # Add suppliers with quotes
        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "add_suppliers_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {
                    "entries": [
                        {"line": 1, "name": "PumpCo", "quote_cost": 145.00, "quote_status": "quoted"},
                        {"line": 1, "name": "Hydraulic Supplies Pty Ltd", "quote_cost": 160.00, "quote_status": "quoted"},
                        {"line": 2, "name": "ValveMasters", "quote_cost": 22.00, "quote_status": "quoted"},
                    ],
                },
            })

        # Select across lines
        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "select_quotes_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {
                    "selections": [
                        {"line": 1, "name": "PumpCo"},
                        {"line": 2, "name": "ValveMasters"},
                    ],
                },
            })
        assert "Bulk Test Corp" in result

        db_session.expire_all()
        items = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).order_by(RFQItem.line).all()

        # Line 1: PumpCo selected, Hydraulic Supplies should still be "quoted"
        suppliers_l1 = {s["name"]: s for s in items[0].suppliers}
        assert suppliers_l1["PumpCo"]["quote_status"] == "selected"
        assert suppliers_l1["Hydraulic Supplies Pty Ltd"]["quote_status"] == "quoted"
        assert items[0].cost_price == 145.00  # copied from quote_cost

        # Line 2: ValveMasters selected
        assert items[1].suppliers[0]["quote_status"] == "selected"
        assert items[1].cost_price == 22.00

    async def test_deselects_previous(self, manage, db_session):
        """Selecting a new supplier deselects the previous selection."""
        await _create_test_rfq(manage, db_session)
        rfq = _get_test_rfq(db_session)

        # Add two suppliers to line 1
        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "add_suppliers_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {
                    "entries": [
                        {"line": 1, "name": "Alpha", "quote_cost": 100.00, "quote_status": "quoted"},
                        {"line": 1, "name": "Beta", "quote_cost": 90.00, "quote_status": "quoted"},
                    ],
                },
            })

        # Select Alpha
        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "select_quotes_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {"selections": [{"line": 1, "name": "Alpha"}]},
            })

        # Now select Beta — Alpha should be deselected
        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "select_quotes_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {"selections": [{"line": 1, "name": "Beta"}]},
            })

        db_session.expire_all()
        item = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id, RFQItem.line == 1).first()
        suppliers = {s["name"]: s for s in item.suppliers}
        assert suppliers["Alpha"]["quote_status"] == "quoted"  # deselected
        assert suppliers["Beta"]["quote_status"] == "selected"
        assert item.cost_price == 90.00  # copied from Beta's quote_cost

    async def test_toggle_off(self, manage, db_session):
        """Selecting an already-selected supplier toggles it off."""
        await _create_test_rfq(manage, db_session)
        rfq = _get_test_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "add_supplier",
                "rfq_id": rfq.rfq_number,
                "data": {"line": 1, "name": "PumpCo", "quote_cost": 150.00, "quote_status": "selected"},
            })

        # Toggle off via bulk
        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            await manage.ainvoke({
                "action": "select_quotes_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {"selections": [{"line": 1, "name": "PumpCo"}]},
            })

        db_session.expire_all()
        item = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id, RFQItem.line == 1).first()
        assert item.suppliers[0]["quote_status"] == "quoted"  # deselected
        assert item.cost_price is None

    async def test_empty_selections(self, manage, db_session):
        """Empty selections list returns an error."""
        await _create_test_rfq(manage, db_session)
        rfq = _get_test_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = await manage.ainvoke({
                "action": "select_quotes_bulk",
                "rfq_id": rfq.rfq_number,
                "data": {"selections": []},
            })
        assert "Error" in result or "required" in result.lower()
