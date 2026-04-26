"""Tests for the dashboard context in-memory store."""

from includes.dashboard_context import set_context, get_context, format_context_for_prompt


class TestSetAndGetContext:
    def test_set_and_get(self):
        set_context("alice@example.com", {"view": "supplier_list"})
        assert get_context("alice@example.com") == {"view": "supplier_list"}

    def test_overwrite(self):
        set_context("bob@example.com", {"view": "product_list"})
        set_context("bob@example.com", {"view": "rfq_detail", "id": "RFQ-1"})
        assert get_context("bob@example.com") == {"view": "rfq_detail", "id": "RFQ-1"}

    def test_unknown_user_returns_none(self):
        assert get_context("nobody@example.com") is None

    def test_users_isolated(self):
        set_context("u1@example.com", {"view": "a"})
        set_context("u2@example.com", {"view": "b"})
        assert get_context("u1@example.com")["view"] == "a"
        assert get_context("u2@example.com")["view"] == "b"


class TestFormatContextForPrompt:
    def test_no_context_returns_empty(self):
        assert format_context_for_prompt("nonexistent@example.com") == ""

    def test_empty_view_returns_empty(self):
        set_context("fmt@example.com", {})
        assert format_context_for_prompt("fmt@example.com") == ""

    def test_view_only(self):
        set_context("fmt1@example.com", {"view": "supplier_list"})
        result = format_context_for_prompt("fmt1@example.com")
        assert "supplier_list" in result
        assert "[Dashboard Context]" in result

    def test_full_context(self):
        set_context("fmt2@example.com", {
            "view": "supplier_detail",
            "entity": "supplier",
            "id": "42",
            "params": {"tab": "purchases"},
            "breadcrumb": ["Suppliers", "Acme Corp"],
        })
        result = format_context_for_prompt("fmt2@example.com")
        assert "supplier_detail" in result
        assert "Entity type: supplier" in result
        assert "ID: 42" in result
        assert "Parameters:" in result
        assert "Breadcrumb: Suppliers > Acme Corp" in result

    def test_view_with_entity_no_id(self):
        set_context("fmt3@example.com", {"view": "product_list", "entity": "product"})
        result = format_context_for_prompt("fmt3@example.com")
        assert "product_list" in result
        assert "Entity type: product" in result
        assert "ID:" not in result
