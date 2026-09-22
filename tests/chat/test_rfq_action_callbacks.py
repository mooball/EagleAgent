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


class TestRfqIdentifyItems:
    """The dashboard 'Classify & Validate' button.

    The handler is a thin renderer over ``rfq_crud._classify_rfq_items_sync``.
    The orchestrator's own behaviour (target selection, validation, quote
    brand, departments) is covered in tests/tools/test_rfq_crud.py; here we
    pin what the user sees and what the handler asks the orchestrator to do.
    """

    def _result(self, **overrides):
        base = {
            "targets": [1, 2],
            "classified": {"specific": [1], "branded": [2], "generic": []},
            "db_matches": [],
            "brand_results": [],
            "to_validate": [],
            "unclassifiable": [],
            "validation": None,
            "quote_brand_result": None,
            "department_result": None,
        }
        base.update(overrides)
        return base

    def _stub(self, monkeypatch, result):
        """Patch the orchestrator; returns the recorded call kwargs."""
        calls: list[dict] = []

        def _fake(rfq_id, user_id, **kwargs):
            calls.append({"rfq_id": rfq_id, "user_id": user_id, **kwargs})
            progress = kwargs.get("progress")
            if progress is not None and result.get("targets"):
                progress(f"Classifying & validating {len(result['targets'])} item(s) in {rfq_id}...")
            return result

        monkeypatch.setattr(
            "includes.tools.rfq_crud._classify_rfq_items_sync", _fake,
        )
        return calls

    async def test_requests_web_validation_for_the_whole_rfq(self, rfq, monkeypatch):
        calls = self._stub(monkeypatch, self._result())

        await rfq_actions.on_rfq_identify_items(action(rfq_id="RFQ-1"), rfq)

        assert len(calls) == 1
        call = calls[0]
        assert call["rfq_id"] == "RFQ-1"
        assert call["user_id"] == "tester@example.com"
        assert call["search_db"] is True
        assert call["validate_web"] is True
        # No payload item list is sent — the server picks the items.
        assert call["lines"] is None
        assert callable(call["should_cancel"])

    async def test_scopes_to_a_single_line(self, rfq, monkeypatch):
        calls = self._stub(monkeypatch, self._result(targets=[3]))

        await rfq_actions.on_rfq_identify_items(action(rfq_id="RFQ-1", line=3), rfq)

        assert calls[0]["lines"] == [3]

    async def test_honours_a_legacy_items_payload(self, rfq, monkeypatch):
        """An old client sends items, not line — scope to those lines rather
        than silently widening to the whole RFQ."""
        calls = self._stub(monkeypatch, self._result(targets=[4]))

        await rfq_actions.on_rfq_identify_items(action(
            rfq_id="RFQ-1",
            items=[{"line": 4, "description": "d", "part_number": "P", "brand": "B"}],
        ), rfq)

        assert calls[0]["lines"] == [4]

    async def test_unknown_payload_shape_narrows_to_nothing(self, rfq, monkeypatch):
        """Never widen: a payload we cannot read must not become a full run."""
        calls = self._stub(monkeypatch, self._result(targets=[]))

        await rfq_actions.on_rfq_identify_items(action(
            rfq_id="RFQ-1", items=[{"nope": 1}],
        ), rfq)

        assert calls[0]["lines"] == []

    async def test_validation_error_is_surfaced(self, rfq, monkeypatch):
        self._stub(monkeypatch, self._result(validation={"error": "boom"}))

        await rfq_actions.on_rfq_identify_items(action(rfq_id="RFQ-1"), rfq)

        # The validation outcome is streamed by the orchestrator, so the
        # handler itself has nothing to add here.
        assert rfq.notifications[-1] == ("agent_done", None)

    async def test_nothing_to_classify_is_reported(self, rfq, monkeypatch):
        self._stub(monkeypatch, self._result(targets=[], classified={
            "specific": [], "branded": [], "generic": [],
        }))

        await rfq_actions.on_rfq_identify_items(action(rfq_id="RFQ-1"), rfq)

        assert any("Nothing to classify" in t for t in rfq.texts)

    async def test_error_from_orchestrator_is_surfaced(self, rfq, monkeypatch):
        self._stub(monkeypatch, {"error": "RFQ 'RFQ-1' not found."})

        await rfq_actions.on_rfq_identify_items(action(rfq_id="RFQ-1"), rfq)

        assert any("not found" in t for t in rfq.texts)

    async def test_progress_is_streamed_to_the_chat(self, rfq, monkeypatch):
        """Progress from the worker thread must land as chat messages, before
        the results — that's what keeps a long web search from looking hung."""
        self._stub(monkeypatch, self._result())

        await rfq_actions.on_rfq_identify_items(action(rfq_id="RFQ-1"), rfq)

        assert rfq.texts[0] == "Classifying & validating 2 item(s) in RFQ-1..."
    async def test_missing_rfq_id_is_a_no_op(self, rfq, monkeypatch):
        calls = self._stub(monkeypatch, self._result())

        await rfq_actions.on_rfq_identify_items(action(), rfq)

        assert calls == []
        assert rfq.texts == []

    async def test_badge_is_raised_and_lowered(self, rfq, monkeypatch):
        self._stub(monkeypatch, self._result())

        await rfq_actions.on_rfq_identify_items(action(rfq_id="RFQ-1"), rfq)

        commands = [c for c, _ in rfq.notifications]
        assert commands[0] == "agent_working"
        assert commands[-1] == "agent_done"
        assert commands.count("dashboard_refresh") >= 1

    async def test_badge_is_lowered_even_when_the_pipeline_raises(self, rfq, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("exploded")

        monkeypatch.setattr(
            "includes.tools.rfq_crud._classify_rfq_items_sync", _boom,
        )

        with pytest.raises(RuntimeError):
            await rfq_actions.on_rfq_identify_items(action(rfq_id="RFQ-1"), rfq)

        assert ("agent_done", None) in rfq.notifications


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
