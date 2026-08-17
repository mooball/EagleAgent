"""The supplier search menu builds ActionSpecs and sends via the ChatContext.

This module was at 0% coverage before the Phase 1 conversion.
"""

import pytest

from includes.chat.context import ActionSpec
from includes.chat.supplier_search_gate import build_menu_actions, show_search_menu

ALL_TYPES = ["SEARCH_PREVIOUS", "SEARCH_BRAND", "SEARCH_WEB_AU", "SEARCH_WEB_INTL"]


# --- build_menu_actions ---------------------------------------------------

def test_all_buttons_when_viable_types_is_none():
    actions = build_menu_actions("RFQ-1", "u@e.com")

    assert all(isinstance(a, ActionSpec) for a in actions)
    assert [a.name for a in actions] == [
        "rfq_pipeline_previous_suppliers",
        "rfq_pipeline_brand_suppliers",
        "rfq_pipeline_new_domestic",
        "rfq_pipeline_new_international",
        "rfq_pipeline_supplier_search_done",
    ]


def test_done_button_is_always_present():
    actions = build_menu_actions("RFQ-1", "u@e.com", viable_types=[])
    assert [a.name for a in actions] == ["rfq_pipeline_supplier_search_done"]


@pytest.mark.parametrize(
    "viable,expected",
    [
        (["SEARCH_PREVIOUS"], ["rfq_pipeline_previous_suppliers"]),
        (["SEARCH_BRAND"], ["rfq_pipeline_brand_suppliers"]),
        (["SEARCH_WEB_AU"], ["rfq_pipeline_new_domestic"]),
        (["SEARCH_WEB_INTL"], ["rfq_pipeline_new_international"]),
    ],
)
def test_only_viable_buttons_are_built(viable, expected):
    actions = build_menu_actions("RFQ-1", "u@e.com", viable_types=viable)
    assert [a.name for a in actions][:-1] == expected


def test_payload_carries_rfq_and_user():
    actions = build_menu_actions("RFQ-1", "u@e.com")
    assert all(a.payload["rfq_id"] == "RFQ-1" for a in actions)
    assert all(a.payload["user_id"] == "u@e.com" for a in actions)
    assert all("line_filter" not in a.payload for a in actions)


def test_line_filter_is_added_to_every_payload():
    actions = build_menu_actions("RFQ-1", "u@e.com", line_filter=[2, 5])
    assert all(a.payload["line_filter"] == [2, 5] for a in actions)


def test_payloads_are_independent_copies():
    actions = build_menu_actions("RFQ-1", "u@e.com")
    actions[0].payload["extra"] = 1
    assert "extra" not in actions[1].payload


# --- show_search_menu -----------------------------------------------------

@pytest.fixture
def viable(monkeypatch):
    """Control get_viable_search_types, which show_search_menu imports inline."""
    import includes.tools.supplier_search_tools as sst

    box = {"viable": list(ALL_TYPES)}
    monkeypatch.setattr(
        sst, "get_viable_search_types", lambda rfq_id, line_numbers=None: box
    )
    return box


@pytest.mark.asyncio
async def test_menu_is_sent_through_the_context(viable, chat_ctx):
    await show_search_menu("RFQ-1", "u@e.com", ctx=chat_ctx)

    assert len(chat_ctx.messages) == 1
    msg = chat_ctx.messages[0]
    assert msg.author == "EagleAgent"
    assert "**Supplier Search Options:**" in msg.content
    assert len(msg.actions) == 5


@pytest.mark.asyncio
async def test_options_text_lists_only_viable_searches(viable, chat_ctx):
    viable["viable"] = ["SEARCH_BRAND", "SEARCH_WEB_INTL"]
    await show_search_menu("RFQ-1", "u@e.com", ctx=chat_ctx)

    content = chat_ctx.messages[0].content
    assert "1. 🏷️ **Brand Suppliers**" in content
    assert "2. 🌐 **New International Suppliers**" in content
    assert "Previous Sales" not in content
    assert [a.name for a in chat_ctx.messages[0].actions] == [
        "rfq_pipeline_brand_suppliers",
        "rfq_pipeline_new_international",
        "rfq_pipeline_supplier_search_done",
    ]


@pytest.mark.asyncio
async def test_summary_is_prepended(viable, chat_ctx):
    await show_search_menu("RFQ-1", "u@e.com", summary="✅ 3 found.", ctx=chat_ctx)
    assert chat_ctx.messages[0].content.startswith("✅ 3 found.\n\n")


@pytest.mark.asyncio
async def test_single_line_scope_is_singular(viable, chat_ctx):
    await show_search_menu("RFQ-1", "u@e.com", line_filter=[4], ctx=chat_ctx)
    assert "for **line 4**" in chat_ctx.messages[0].content


@pytest.mark.asyncio
async def test_multi_line_scope_is_plural(viable, chat_ctx):
    await show_search_menu("RFQ-1", "u@e.com", line_filter=[4, 7], ctx=chat_ctx)
    assert "for **lines 4, 7**" in chat_ctx.messages[0].content


@pytest.mark.asyncio
async def test_falls_back_to_the_bound_context(viable, bound_chat_ctx):
    await show_search_menu("RFQ-1", "u@e.com")
    assert len(bound_chat_ctx.messages) == 1


@pytest.mark.asyncio
async def test_raises_when_no_context_is_available(viable):
    with pytest.raises(RuntimeError, match="No ChatContext bound"):
        await show_search_menu("RFQ-1", "u@e.com")
