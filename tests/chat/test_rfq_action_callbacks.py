"""Characterisation tests for a representative sample of the 21 action handlers.

Not all 21 — one per distinct shape, per the Phase 0 plan.

Phase 1 Step 6 converted these to ``on_x(payload, ctx)`` and deleted the
thread-pinning helpers, so the *call convention* below changed. Every
assertion is unchanged: what each handler does — which messages it sends,
which session keys it touches, which dashboard notifications it fires — is
still exactly what is pinned here.

Deliberately asserts structure and side effects rather than exact prose, so
copy changes do not create false failures.
"""

import pytest

import includes.chat.rfq_actions as rfq_actions


@pytest.fixture
def rfq(chat_ctx, monkeypatch):
    """A recording ChatContext plus stubs for rfq_actions' I/O helpers."""
    supplier_updates: list[tuple] = []
    resumes: list[tuple] = []
    reentries: list[tuple] = []

    def _update_supplier(rfq_id, supplier, user_id):
        supplier_updates.append((rfq_id, supplier, user_id))

    async def _resume(rfq_id, user_id, stage, ctx):
        resumes.append((rfq_id, user_id, stage))

    async def _run_turn(text, ctx, **kwargs):
        reentries.append((text, ctx.thread_id))

    monkeypatch.setattr(rfq_actions, "_update_supplier_sync", _update_supplier)
    monkeypatch.setattr(rfq_actions, "_resume_pipeline_from", _resume)
    monkeypatch.setattr("includes.chat.runner.run_turn", _run_turn)

    # The old fixture exposed these off the fake `cl`; keep the same names.
    chat_ctx.notifications = chat_ctx.dashboard_calls
    chat_ctx.session = chat_ctx._state
    chat_ctx.supplier_updates = supplier_updates
    chat_ctx.resumes = resumes
    chat_ctx.reentries = reentries
    return chat_ctx


def action(**payload):
    """The payload as the handlers now receive it."""
    return payload


class TestRfqRefresh:
    """Simplest shape: notify only."""

    async def test_notifies_the_dashboard(self, rfq):
        await rfq_actions.on_rfq_refresh(action(rfq_id="RFQ-1"), rfq)
        assert rfq.notifications == [("dashboard_refresh", None)]

    async def test_missing_rfq_id_is_a_no_op(self, rfq):
        await rfq_actions.on_rfq_refresh(action(), rfq)
        assert rfq.notifications == []


class TestRfqIdentifyItemsQuoteBrand:
    """The dashboard 'Classify & Validate' button must also auto-set the
    quote brand (deterministic majority) once classify/validate finish."""

    async def test_sets_quote_brand_after_validation(self, rfq, monkeypatch):
        item_updates: list = []
        quote_brand_calls: list = []

        def _update_item(rfq_id, data, user_id):
            item_updates.append((rfq_id, data, user_id))

        def _set_quote_brand(rfq_id, user_id):
            quote_brand_calls.append((rfq_id, user_id))
            return "Auto-set quote brand to 'Komatsu' (majority item brand, 2/2 items)."

        monkeypatch.setattr(rfq_actions, "_update_item_sync", _update_item)
        monkeypatch.setattr(
            "includes.tools.rfq_crud._set_quote_brand_from_items_sync",
            _set_quote_brand,
        )
        monkeypatch.setattr(
            "includes.tools.rfq_crud._set_item_departments_sync",
            lambda rfq_id, user_id: "Departments auto-set: 2 by LLM.",
        )
        monkeypatch.setattr(
            "includes.tools.product_tools._find_product_by_code",
            lambda pn, brand=None: None,  # nothing matched in the internal DB
        )
        monkeypatch.setattr(
            "includes.tools.rfq_crud._validate_items_sync",
            lambda rfq_id, web_items, user_id: {"validated": []},
        )

        await rfq_actions.on_rfq_identify_items(action(
            rfq_id="RFQ-1",
            items=[
                {"line": 1, "description": "desc", "part_number": "PN1", "brand": "Komatsu"},
                {"line": 2, "description": "desc2", "part_number": "PN2", "brand": "Komatsu"},
            ],
        ), rfq)

        assert quote_brand_calls == [("RFQ-1", "tester@example.com")]
        assert any("Auto-set quote brand" in m.content for m in rfq.messages)
        assert ("dashboard_refresh", None) in rfq.notifications

    async def test_skips_quote_brand_when_no_items(self, rfq, monkeypatch):
        quote_brand_calls: list = []

        def _set_quote_brand(rfq_id, user_id):
            quote_brand_calls.append((rfq_id, user_id))
            return None

        monkeypatch.setattr(rfq_actions, "_update_item_sync", lambda *a: None)
        monkeypatch.setattr(
            "includes.tools.rfq_crud._set_quote_brand_from_items_sync",
            _set_quote_brand,
        )
        monkeypatch.setattr(
            "includes.tools.rfq_crud._set_item_departments_sync",
            lambda rfq_id, user_id: None,
        )

        await rfq_actions.on_rfq_identify_items(action(rfq_id="RFQ-1", items=[]), rfq)

        assert quote_brand_calls == []
        assert rfq.messages == []

    async def test_sets_item_departments_after_quote_brand(self, rfq, monkeypatch):
        """Step D runs after quote brand: departments are auto-set and the
        result is reported to the user."""
        dept_calls: list = []

        def _set_departments(rfq_id, user_id):
            dept_calls.append((rfq_id, user_id))
            return "Departments auto-set: 1 from product match, 1 by LLM."

        monkeypatch.setattr(rfq_actions, "_update_item_sync", lambda *a: None)
        monkeypatch.setattr(
            "includes.tools.rfq_crud._set_quote_brand_from_items_sync",
            lambda rfq_id, user_id: None,
        )
        monkeypatch.setattr(
            "includes.tools.rfq_crud._set_item_departments_sync",
            _set_departments,
        )
        monkeypatch.setattr(
            "includes.tools.product_tools._find_product_by_code",
            lambda pn, brand=None: None,
        )
        monkeypatch.setattr(
            "includes.tools.rfq_crud._validate_items_sync",
            lambda rfq_id, web_items, user_id: {"validated": []},
        )

        await rfq_actions.on_rfq_identify_items(action(
            rfq_id="RFQ-1",
            items=[
                {"line": 1, "description": "desc", "part_number": "PN1", "brand": "Komatsu"},
            ],
        ), rfq)

        assert dept_calls == [("RFQ-1", "tester@example.com")]
        assert any("Departments auto-set" in m.content for m in rfq.messages)
        assert ("dashboard_refresh", None) in rfq.notifications


class TestRfqIdentifyItemsMultiBrand:
    """A standard cross-brand part number (belt size codes etc.) must not
    block the RFQ: multi_brand results are reported, not flagged."""

    async def test_multi_brand_findings_reported(self, rfq, monkeypatch):
        monkeypatch.setattr(rfq_actions, "_update_item_sync", lambda *a: None)
        monkeypatch.setattr(
            "includes.tools.rfq_crud._set_quote_brand_from_items_sync",
            lambda *a: None,
        )
        monkeypatch.setattr(
            "includes.tools.rfq_crud._set_item_departments_sync",
            lambda *a: None,
        )
        monkeypatch.setattr(
            "includes.tools.product_tools._find_product_by_code",
            lambda pn, brand=None: None,
        )
        monkeypatch.setattr(
            "includes.tools.rfq_crud._validate_items_sync",
            lambda rfq_id, web_items, user_id: {
                "validated": [
                    {"line": 1, "status": "multi_brand",
                     "findings": "B82 is a standard belt size used by Gates, Bando, etc."}
                ]
            },
        )

        await rfq_actions.on_rfq_identify_items(action(
            rfq_id="RFQ-1",
            items=[{"line": 1, "description": "V-belt", "part_number": "B82", "brand": ""}],
        ), rfq)

        assert any("multi-brand" in m.content for m in rfq.messages)


class TestRfqUpdateSupplier:
    """Dashboard-initiated write, then refresh and confirm."""

    async def test_writes_then_refreshes_then_confirms(self, rfq):
        await rfq_actions.on_rfq_update_supplier(action(
            rfq_id="RFQ-1", line=3, supplier_name="Acme", status="quote_received",
        ), rfq)

        assert rfq.supplier_updates == [
            ("RFQ-1", {"line": 3, "name": "Acme", "status": "quote_received"},
             "tester@example.com"),
        ]
        assert ("dashboard_refresh", None) in rfq.notifications
        assert len(rfq.messages) == 1
        assert "Acme" in rfq.messages[0].content

    @pytest.mark.parametrize("missing", ["rfq_id", "line", "supplier_name", "status"])
    async def test_any_missing_field_aborts_before_writing(self, rfq, missing):
        payload = {
            "rfq_id": "RFQ-1", "line": 3,
            "supplier_name": "Acme", "status": "quote_received",
        }
        payload.pop(missing)

        await rfq_actions.on_rfq_update_supplier(action(**payload), rfq)

        assert rfq.supplier_updates == []
        assert rfq.messages == []

    async def test_unknown_user_falls_back_to_a_placeholder(self, make_chat_ctx, monkeypatch):
        updates: list[tuple] = []
        monkeypatch.setattr(
            rfq_actions, "_update_supplier_sync",
            lambda r, s, u: updates.append((r, s, u)),
        )
        ctx = make_chat_ctx(user_email="")

        await rfq_actions.on_rfq_update_supplier(action(
            rfq_id="RFQ-1", line=1, supplier_name="Acme", status="sent",
        ), ctx)

        assert updates[0][2] == "unknown"


class TestPipelineCounterHandlers:
    """These read and write the per-RFQ pipeline_fixes_{id} session counter."""

    async def test_skip_validation_resets_the_counter(self, rfq):
        rfq.session["pipeline_fixes_RFQ-1"] = 4

        await rfq_actions.on_rfq_pipeline_skip_validation(action(rfq_id="RFQ-1"), rfq)

        assert rfq.session["pipeline_fixes_RFQ-1"] == 0

    async def test_skip_validation_resumes_at_the_group_stage(self, rfq):
        await rfq_actions.on_rfq_pipeline_skip_validation(action(rfq_id="RFQ-1"), rfq)

        assert rfq.resumes == [("RFQ-1", "tester@example.com", "group")]

    async def test_payload_user_id_wins_over_the_session(self, rfq):
        await rfq_actions.on_rfq_pipeline_skip_validation(
            action(rfq_id="RFQ-1", user_id="payload-user"), rfq
        )

        assert rfq.resumes[0][1] == "payload-user"

    async def test_missing_rfq_id_errors_without_resuming(self, rfq):
        await rfq_actions.on_rfq_pipeline_skip_validation(action(), rfq)

        assert rfq.resumes == []
        assert len(rfq.messages) == 1
        assert "Error" in rfq.messages[0].content

    async def test_retry_validation_resumes_at_the_validate_stage(self, rfq):
        await rfq_actions.on_rfq_pipeline_retry_validation(action(rfq_id="RFQ-1"), rfq)

        assert rfq.resumes == [("RFQ-1", "tester@example.com", "validate")]

    async def test_retry_validation_leaves_the_counter_alone(self, rfq):
        rfq.session["pipeline_fixes_RFQ-1"] = 2

        await rfq_actions.on_rfq_pipeline_retry_validation(action(rfq_id="RFQ-1"), rfq)

        assert rfq.session["pipeline_fixes_RFQ-1"] == 2


class TestRfqFindAllSuppliers:
    """The only handler that re-enters the graph.

    Phase 1 replaced the synthetic-message + _main_pinned round trip with a
    direct run_turn(..., on_busy="wait") call, so the observable contract is what
    matters: a prompt naming the RFQ, run against this context's thread.
    """

    async def test_reenters_the_graph_with_a_prompt_naming_the_rfq(self, rfq):
        await rfq_actions.on_rfq_find_all_suppliers(action(rfq_id="RFQ-2026-0042"), rfq)

        assert len(rfq.reentries) == 1
        prompt, thread_id = rfq.reentries[0]
        assert "RFQ-2026-0042" in prompt
        assert thread_id == "thread-abc"

    async def test_missing_rfq_id_still_reenters_with_a_placeholder(self, rfq):
        """Characterisation, not endorsement.

        Unlike its siblings there is no guard, so a missing rfq_id sends the
        agent a prompt containing '???'. Tracked as todo.vu #32822.
        """
        await rfq_actions.on_rfq_find_all_suppliers(action(), rfq)

        assert len(rfq.reentries) == 1
        assert "???" in rfq.reentries[0][0]
