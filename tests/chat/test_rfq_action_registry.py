"""The RFQ action registry must not silently lose a handler.

Before Phase 1 Step 6 these were 21 `@cl.action_callback` decorators. The
registry replaced them, so the guard is that every name still resolves and
every button the code emits still has a handler.
"""

import inspect

import pytest

from includes.chat.rfq_actions import RFQ_ACTIONS

# The 21 names that carried a @cl.action_callback decorator before Step 6.
EXPECTED_NAMES = {
    "rfq_refresh",
    "rfq_update_supplier",
    "rfq_identify_items",
    "rfq_find_suppliers",
    "rfq_find_web_suppliers_for_line",
    "rfq_dismiss",
    "rfq_pipeline_fix_part",
    "rfq_pipeline_skip_validation",
    "rfq_pipeline_retry_validation",
    "rfq_pipeline_web_search",
    "rfq_pipeline_previous_suppliers",
    "rfq_pipeline_brand_suppliers",
    "rfq_pipeline_new_domestic",
    "rfq_pipeline_new_international",
    "rfq_pipeline_supplier_search_done",
    "rfq_group_items",
    "rfq_find_all_suppliers",
    "rfq_find_previous_suppliers",
    "rfq_add_brand_supplier",
    "rfq_find_new_suppliers",
    "rfq_find_brand_suppliers",
}


def test_no_action_name_was_lost():
    assert set(RFQ_ACTIONS) == EXPECTED_NAMES


def test_there_are_twenty_one_handlers():
    assert len(RFQ_ACTIONS) == 21


@pytest.mark.parametrize("name", sorted(EXPECTED_NAMES))
def test_every_handler_is_an_async_payload_ctx_callable(name):
    handler = RFQ_ACTIONS[name]
    assert inspect.iscoroutinefunction(handler), f"{name} must be async"

    params = list(inspect.signature(handler).parameters)
    assert params[:2] == ["payload", "ctx"], f"{name} has signature {params}"


def test_handlers_are_distinct():
    """A copy-paste slip could point two names at one handler."""
    assert len(set(RFQ_ACTIONS.values())) == len(RFQ_ACTIONS)


def test_the_chainlit_adapter_registers_every_name():
    """app.py's loop must cover the whole registry."""
    import app  # noqa: F401 — importing runs the registration loop
    from chainlit.config import config as cl_config

    assert set(RFQ_ACTIONS) <= set(cl_config.code.action_callbacks)


def test_buttons_emitted_by_the_search_menu_all_have_handlers():
    """Every ActionSpec the supplier-search menu builds must be dispatchable."""
    from includes.chat.supplier_search_gate import build_menu_actions

    emitted = {a.name for a in build_menu_actions("RFQ-1", "u@e.com")}
    assert emitted <= set(RFQ_ACTIONS), f"orphan buttons: {emitted - set(RFQ_ACTIONS)}"
