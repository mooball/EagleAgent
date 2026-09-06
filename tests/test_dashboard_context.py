"""Tests for the dashboard context in-memory store."""

from includes.dashboard.context import (
    set_context,
    get_context,
    set_thread_context,
    lookup_context,
    format_context_for_prompt,
)


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


class TestThreadContextIsolation:
    def test_threads_isolated(self):
        set_thread_context("thread-a", {"view": "rfq_detail", "id": "RFQ-A"})
        set_thread_context("thread-b", {"view": "rfq_detail", "id": "RFQ-B"})
        assert lookup_context("t@example.com", "thread-a")["id"] == "RFQ-A"
        assert lookup_context("t@example.com", "thread-b")["id"] == "RFQ-B"

    def test_unknown_thread_falls_back_to_user_entry(self):
        set_context("t@example.com", {"view": "dashboard"})
        assert lookup_context("t@example.com", "thread-x") == {"view": "dashboard"}

    def test_no_thread_falls_back_to_user_entry(self):
        set_context("t@example.com", {"view": "rfq_list"})
        assert lookup_context("t@example.com", None) == {"view": "rfq_list"}

    def test_thread_entry_wins_over_user_entry(self):
        set_context("t@example.com", {"view": "dashboard"})
        set_thread_context("thread-a", {"view": "rfq_detail", "id": "RFQ-A"})
        assert lookup_context("t@example.com", "thread-a")["id"] == "RFQ-A"

    def test_format_prefers_thread_context(self):
        set_context("t@example.com", {"view": "dashboard"})
        set_thread_context("thread-a", {"view": "rfq_detail", "id": "RFQ-2026-1"})
        out = format_context_for_prompt("t@example.com", thread_id="thread-a")
        assert "RFQ-2026-1" in out
        # Without a thread, the user-level entry is used
        assert "dashboard" in format_context_for_prompt("t@example.com")

    def test_empty_thread_id_ignored(self):
        set_thread_context("", {"view": "rfq_detail", "id": "RFQ-EMPTY"})
        assert lookup_context("empty-thread@example.com", None) is None


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

    def test_rfq_detail_includes_summary_fields(self):
        set_context("rfq1@example.com", {
            "view": "rfq_detail",
            "entity": "rfq",
            "id": "RFQ-2026-0042",
            "customer": "Acme Construction",
            "status": "in_progress",
            "assigned_to": "tom@eagle.com",
            "item_count": 5,
            "identified_count": 3,
        })
        result = format_context_for_prompt("rfq1@example.com")
        assert "Customer: Acme Construction" in result
        assert "Status: in_progress" in result
        assert "Items: 5 (3 identified)" in result
        assert "Assigned to: tom@eagle.com" in result

    def test_rfq_detail_without_summary_fields(self):
        set_context("rfq2@example.com", {
            "view": "rfq_detail",
            "entity": "rfq",
            "id": "RFQ-2026-0001",
        })
        result = format_context_for_prompt("rfq2@example.com")
        assert "rfq_detail" in result
        assert "Customer:" not in result
