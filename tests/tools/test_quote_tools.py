"""Tests for RFQ (Request for Quote) management tools.

Tests create, update, supplier operations, assignment, status changes,
note appending, external linking, and query/filter via the LangGraph Store.
"""

import pytest
from includes.tools.quote_tools import create_quote_tools, NAMESPACE, _send_rfq_element


@pytest.fixture
def rfq_tools(test_store, test_user_id):
    """Create RFQ tools bound to test store and user."""
    return create_quote_tools(test_store, test_user_id)


@pytest.fixture
def manage(rfq_tools):
    return rfq_tools[0]  # manage_rfq


@pytest.fixture
def get(rfq_tools):
    return rfq_tools[1]  # get_rfq


# ---- helpers ----

async def _create_sample_rfq(manage, **overrides):
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
    return await manage.ainvoke({"action": "create", "data": data})


# ===========================================================================
# manage_rfq — create
# ===========================================================================

class TestManageRfqCreate:
    async def test_create_basic(self, manage, test_store):
        result = await _create_sample_rfq(manage)
        assert "RFQ-" in result
        assert "Acme Construction" in result
        assert "Cordless drill" in result
        assert "Dumpy level" in result

    async def test_create_assigns_sequential_ids(self, manage, test_store):
        r1 = await _create_sample_rfq(manage)
        r2 = await _create_sample_rfq(manage, customer="Beta Corp")
        # Both should have IDs, second should be +1
        assert "RFQ-" in r1
        assert "RFQ-" in r2
        # Verify two distinct items in store
        items = await test_store.asearch(NAMESPACE, limit=100)
        assert len(items) == 2
        keys = sorted(i.key for i in items)
        assert keys[0].endswith("0001")
        assert keys[1].endswith("0002")

    async def test_create_requires_customer(self, manage):
        result = await manage.ainvoke({"action": "create", "data": {}})
        assert "error" in result.lower()

    async def test_create_sets_default_status_draft(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq = items[0].value
        assert rfq["status"] == "draft"

    async def test_create_records_history(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq = items[0].value
        assert len(rfq["history"]) == 1
        assert "Created RFQ with 2 items" in rfq["history"][0]["action"]

    async def test_create_stores_customer_contact(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq = items[0].value
        assert rfq["customer_contact"]["name"] == "John Smith"
        assert rfq["customer_contact"]["email"] == "john@acme.com.au"

    async def test_create_items_default_unidentified(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq = items[0].value
        for item in rfq["items"]:
            assert item["status"] == "unidentified"
            assert item["suppliers"] == []


# ===========================================================================
# manage_rfq — update_item
# ===========================================================================

class TestManageRfqUpdateItem:
    async def test_update_item_part_number(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        result = await manage.ainvoke({
            "action": "update_item",
            "rfq_id": rfq_id,
            "data": {"line": 1, "part_number": "DHP486Z", "brand": "Makita", "status": "confirmed"},
        })
        assert "Confirmed" in result or "confirmed" in result.lower()

        updated = await test_store.aget(NAMESPACE, rfq_id)
        line1 = updated.value["items"][0]
        assert line1["part_number"] == "DHP486Z"
        assert line1["brand"] == "Makita"
        assert line1["status"] == "confirmed"

    async def test_update_item_missing_line(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        result = await manage.ainvoke({
            "action": "update_item", "rfq_id": rfq_id, "data": {"part_number": "X"},
        })
        assert "error" in result.lower()

    async def test_update_item_invalid_line(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        result = await manage.ainvoke({
            "action": "update_item", "rfq_id": rfq_id, "data": {"line": 99},
        })
        assert "error" in result.lower()

    async def test_update_item_line_number_alias(self, manage, test_store):
        """LLMs sometimes use 'line_number' instead of 'line'."""
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        result = await manage.ainvoke({
            "action": "update_item",
            "rfq_id": rfq_id,
            "data": {"line_number": 1, "part_number": "DHP486Z", "status": "confirmed"},
        })
        assert "error" not in result.lower()

        updated = await test_store.aget(NAMESPACE, rfq_id)
        assert updated.value["items"][0]["part_number"] == "DHP486Z"


# ===========================================================================
# manage_rfq — add_supplier / update_supplier
# ===========================================================================

class TestManageRfqSuppliers:
    async def test_add_supplier(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        result = await manage.ainvoke({
            "action": "add_supplier",
            "rfq_id": rfq_id,
            "data": {
                "line": 1,
                "name": "Sydney Tools",
                "price": 189.00,
                "contacts": [{"type": "email", "value": "sales@sydneytools.com.au"}],
            },
        })
        assert "Sydney Tools" in result

        updated = await test_store.aget(NAMESPACE, rfq_id)
        suppliers = updated.value["items"][0]["suppliers"]
        assert len(suppliers) == 1
        assert suppliers[0]["name"] == "Sydney Tools"
        assert suppliers[0]["price"] == 189.00

    async def test_add_multiple_suppliers_batch(self, manage, test_store):
        """Adding multiple suppliers in one call via 'suppliers' list."""
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        result = await manage.ainvoke({
            "action": "add_supplier",
            "rfq_id": rfq_id,
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

        updated = await test_store.aget(NAMESPACE, rfq_id)
        suppliers = updated.value["items"][0]["suppliers"]
        assert len(suppliers) == 3
        names = [s["name"] for s in suppliers]
        assert "Sydney Tools" in names
        assert "Total Tools" in names
        assert "ToolMart Online" in names

    async def test_update_supplier_status(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        await manage.ainvoke({
            "action": "add_supplier",
            "rfq_id": rfq_id,
            "data": {"line": 1, "name": "Sydney Tools", "contacts": [{"email": "info@sydneytools.com.au"}]},
        })
        result = await manage.ainvoke({
            "action": "update_supplier",
            "rfq_id": rfq_id,
            "data": {"line": 1, "name": "Sydney Tools", "status": "shortlisted", "price": 200.0},
        })
        assert "Sydney Tools" in result

        updated = await test_store.aget(NAMESPACE, rfq_id)
        sup = updated.value["items"][0]["suppliers"][0]
        assert sup["status"] == "shortlisted"
        assert sup["price"] == 200.0

    async def test_update_supplier_not_found(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        result = await manage.ainvoke({
            "action": "update_supplier",
            "rfq_id": rfq_id,
            "data": {"line": 1, "name": "Nonexistent"},
        })
        assert "error" in result.lower()

    async def test_add_supplier_missing_name(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        result = await manage.ainvoke({
            "action": "add_supplier",
            "rfq_id": rfq_id,
            "data": {"line": 1},
        })
        assert "error" in result.lower()


# ===========================================================================
# manage_rfq — assign, update_status, add_note, link_external
# ===========================================================================

class TestManageRfqMisc:
    async def test_assign(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        await manage.ainvoke({
            "action": "assign", "rfq_id": rfq_id,
            "data": {"assigned_to": "sarah@eagle.com.au"},
        })
        updated = await test_store.aget(NAMESPACE, rfq_id)
        assert updated.value["assigned_to"] == "sarah@eagle.com.au"

    async def test_update_status(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        await manage.ainvoke({
            "action": "update_status", "rfq_id": rfq_id,
            "data": {"status": "in_progress"},
        })
        updated = await test_store.aget(NAMESPACE, rfq_id)
        assert updated.value["status"] == "in_progress"

    async def test_update_status_invalid(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        result = await manage.ainvoke({
            "action": "update_status", "rfq_id": rfq_id,
            "data": {"status": "bogus"},
        })
        assert "error" in result.lower()

    async def test_add_note(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        await manage.ainvoke({
            "action": "add_note", "rfq_id": rfq_id,
            "data": {"note": "Urgent — needed by end of month"},
        })
        updated = await test_store.aget(NAMESPACE, rfq_id)
        assert "Urgent" in updated.value["notes"]

    async def test_add_note_appends(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        await manage.ainvoke({
            "action": "add_note", "rfq_id": rfq_id,
            "data": {"note": "First note"},
        })
        await manage.ainvoke({
            "action": "add_note", "rfq_id": rfq_id,
            "data": {"note": "Second note"},
        })
        updated = await test_store.aget(NAMESPACE, rfq_id)
        assert "First note" in updated.value["notes"]
        assert "Second note" in updated.value["notes"]

    async def test_link_external_netsuite(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        await manage.ainvoke({
            "action": "link_external", "rfq_id": rfq_id,
            "data": {"netsuite_opportunity": "OPP-12345"},
        })
        updated = await test_store.aget(NAMESPACE, rfq_id)
        assert updated.value["netsuite_opportunity"] == "OPP-12345"

    async def test_link_external_hubspot(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        await manage.ainvoke({
            "action": "link_external", "rfq_id": rfq_id,
            "data": {"hubspot_deal": "D-9876"},
        })
        updated = await test_store.aget(NAMESPACE, rfq_id)
        assert updated.value["hubspot_deal"] == "D-9876"

    async def test_link_external_empty(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        result = await manage.ainvoke({
            "action": "link_external", "rfq_id": rfq_id, "data": {},
        })
        assert "error" in result.lower()

    async def test_unknown_action(self, manage):
        result = await manage.ainvoke({"action": "explode"})
        assert "error" in result.lower()

    async def test_rfq_not_found(self, manage):
        result = await manage.ainvoke({
            "action": "update_status", "rfq_id": "RFQ-9999-0000",
            "data": {"status": "draft"},
        })
        assert "not found" in result.lower()

    async def test_history_accumulates(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        await manage.ainvoke({
            "action": "update_status", "rfq_id": rfq_id,
            "data": {"status": "in_progress"},
        })
        await manage.ainvoke({
            "action": "assign", "rfq_id": rfq_id,
            "data": {"assigned_to": "sarah@eagle.com.au"},
        })
        updated = await test_store.aget(NAMESPACE, rfq_id)
        assert len(updated.value["history"]) == 3  # create + status + assign


# ===========================================================================
# get_rfq
# ===========================================================================

class TestGetRfq:
    async def test_get_single(self, manage, get, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        result = await get.ainvoke({"rfq_id": rfq_id})
        assert rfq_id in result
        assert "Acme Construction" in result

    async def test_get_not_found(self, get):
        result = await get.ainvoke({"rfq_id": "RFQ-0000-0000"})
        assert "not found" in result.lower()

    async def test_list_all(self, manage, get, test_store):
        await _create_sample_rfq(manage)
        await _create_sample_rfq(manage, customer="Beta Corp")

        result = await get.ainvoke({"list_all": True})
        assert "Acme Construction" in result
        assert "Beta Corp" in result
        assert "2 RFQs total" in result

    async def test_filter_by_status(self, manage, get, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        await manage.ainvoke({
            "action": "update_status", "rfq_id": rfq_id,
            "data": {"status": "in_progress"},
        })
        await _create_sample_rfq(manage, customer="Beta Corp")

        result = await get.ainvoke({"status": "in_progress"})
        assert "Acme" in result
        assert "Beta" not in result

    async def test_filter_by_assigned_to(self, manage, get, test_store, test_user_id):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        await manage.ainvoke({
            "action": "assign", "rfq_id": rfq_id,
            "data": {"assigned_to": "sarah@eagle.com.au"},
        })

        result = await get.ainvoke({"assigned_to": "sarah@eagle.com.au"})
        assert "Acme" in result

        result2 = await get.ainvoke({"assigned_to": test_user_id})
        assert "No RFQs found" in result2

    async def test_default_shows_my_rfqs(self, manage, get, test_store, test_user_id):
        """get_rfq() with no args shows current user's RFQs."""
        await _create_sample_rfq(manage)
        result = await get.ainvoke({})
        # Should show RFQs assigned to test_user_id (the creator)
        assert "Acme" in result


# ===========================================================================
# Rendering
# ===========================================================================

class TestRendering:
    async def test_summary_shows_supplier_price(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        await manage.ainvoke({
            "action": "add_supplier",
            "rfq_id": rfq_id,
            "data": {"line": 1, "name": "Sydney Tools", "price": 189.0, "price_type": "previous_purchase", "contacts": [{"email": "info@sydneytools.com.au"}]},
        })
        result = await manage.ainvoke({
            "action": "add_supplier",
            "rfq_id": rfq_id,
            "data": {"line": 1, "name": "Total Tools", "status": "dropped", "contacts": [{"email": "info@totaltools.com.au"}]},
        })
        assert "$189.00" in result
        assert "prev" in result
        assert "~~Total Tools~~" in result

    async def test_summary_shows_estimated_label(self, manage, test_store):
        await _create_sample_rfq(manage)
        items = await test_store.asearch(NAMESPACE, limit=10)
        rfq_id = items[0].key

        result = await manage.ainvoke({
            "action": "add_supplier",
            "rfq_id": rfq_id,
            "data": {"line": 1, "name": "WebSupplier", "price": 250.0, "price_type": "estimated", "contacts": [{"url": "https://websupplier.com.au"}]},
        })
        assert "$250.00 est" in result

    async def test_summary_shows_contact(self, manage):
        result = await _create_sample_rfq(manage)
        assert "John Smith" in result
        assert "john@acme.com.au" in result

    async def test_send_rfq_element_no_chainlit_context(self):
        """_send_rfq_element gracefully skips when not in a Chainlit session."""
        rfq = {"id": "RFQ-2026-0001", "customer": "Test", "items": [], "status": "draft"}
        # Should not raise even without a Chainlit context
        await _send_rfq_element(rfq)
