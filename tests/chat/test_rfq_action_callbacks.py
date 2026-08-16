"""Characterisation tests for a representative sample of the 28 action callbacks.

Not all 28 — one per distinct shape, per the Phase 0 plan. Phase 1 converts these
to take an explicit ChatContext and deletes the thread-pinning helpers, so what
each handler *does* needs pinning first: which messages it sends, which session
keys it touches, and which dashboard notifications it fires.

Deliberately asserts structure and side effects rather than exact prose, so
copy changes do not create false failures.
"""

import pytest

import includes.chat.rfq_actions as rfq_actions


@pytest.fixture
def rfq(patch_cl, monkeypatch):
    """Patch rfq_actions' chainlit surface and its I/O helpers."""
    cl = patch_cl(rfq_actions)

    notifications: list[tuple] = []
    supplier_updates: list[tuple] = []
    resumes: list[tuple] = []
    reentries: list[tuple] = []

    async def _notify(command, payload=None):
        notifications.append((command, payload))

    def _update_supplier(rfq_id, supplier, user_id):
        supplier_updates.append((rfq_id, supplier, user_id))

    async def _resume(rfq_id, user_id, stage):
        resumes.append((rfq_id, user_id, stage))

    async def _main_pinned(msg, thread_id):
        reentries.append((msg.content, thread_id))

    monkeypatch.setattr(rfq_actions, "notify_dashboard", _notify)
    monkeypatch.setattr(rfq_actions, "_update_supplier_sync", _update_supplier)
    monkeypatch.setattr(rfq_actions, "_resume_pipeline_from", _resume)
    monkeypatch.setattr(rfq_actions, "_main_pinned", _main_pinned)

    cl.notifications = notifications
    cl.supplier_updates = supplier_updates
    cl.resumes = resumes
    cl.reentries = reentries
    return cl


def action(**payload):
    """A cl.Action as the callbacks receive it."""
    class _A:
        def __init__(self, p):
            self.payload = p
    return _A(payload)


class TestRfqRefresh:
    """Simplest shape: notify only."""

    async def test_notifies_the_dashboard(self, rfq):
        await rfq_actions.on_rfq_refresh(action(rfq_id="RFQ-1"))
        assert rfq.notifications == [("dashboard_refresh", None)]

    async def test_missing_rfq_id_is_a_no_op(self, rfq):
        await rfq_actions.on_rfq_refresh(action())
        assert rfq.notifications == []


class TestRfqUpdateSupplier:
    """Dashboard-initiated write, then refresh and confirm."""

    async def test_writes_then_refreshes_then_confirms(self, rfq):
        rfq.session["user_id"] = "tester@example.com"

        await rfq_actions.on_rfq_update_supplier(action(
            rfq_id="RFQ-1", line=3, supplier_name="Acme", status="quote_received",
        ))

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

        await rfq_actions.on_rfq_update_supplier(action(**payload))

        assert rfq.supplier_updates == []
        assert rfq.messages == []

    async def test_unknown_user_falls_back_to_a_placeholder(self, rfq):
        await rfq_actions.on_rfq_update_supplier(action(
            rfq_id="RFQ-1", line=1, supplier_name="Acme", status="sent",
        ))
        assert rfq.supplier_updates[0][2] == "unknown"


class TestPipelineCounterHandlers:
    """These read and write the per-RFQ pipeline_fixes_{id} session counter."""

    async def test_skip_validation_resets_the_counter(self, rfq):
        rfq.session["pipeline_fixes_RFQ-1"] = 4

        await rfq_actions.on_rfq_pipeline_skip_validation(action(rfq_id="RFQ-1"))

        assert rfq.session["pipeline_fixes_RFQ-1"] == 0

    async def test_skip_validation_resumes_at_the_group_stage(self, rfq):
        rfq.session["user_id"] = "tester@example.com"

        await rfq_actions.on_rfq_pipeline_skip_validation(action(rfq_id="RFQ-1"))

        assert rfq.resumes == [("RFQ-1", "tester@example.com", "group")]

    async def test_payload_user_id_wins_over_the_session(self, rfq):
        rfq.session["user_id"] = "session-user"

        await rfq_actions.on_rfq_pipeline_skip_validation(
            action(rfq_id="RFQ-1", user_id="payload-user")
        )

        assert rfq.resumes[0][1] == "payload-user"

    async def test_missing_rfq_id_errors_without_resuming(self, rfq):
        await rfq_actions.on_rfq_pipeline_skip_validation(action())

        assert rfq.resumes == []
        assert len(rfq.messages) == 1
        assert "Error" in rfq.messages[0].content

    async def test_retry_validation_resumes_at_the_validate_stage(self, rfq):
        rfq.session["user_id"] = "tester@example.com"

        await rfq_actions.on_rfq_pipeline_retry_validation(action(rfq_id="RFQ-1"))

        assert rfq.resumes == [("RFQ-1", "tester@example.com", "validate")]

    async def test_retry_validation_leaves_the_counter_alone(self, rfq):
        rfq.session["pipeline_fixes_RFQ-1"] = 2

        await rfq_actions.on_rfq_pipeline_retry_validation(action(rfq_id="RFQ-1"))

        assert rfq.session["pipeline_fixes_RFQ-1"] == 2


class TestRfqFindAllSuppliers:
    """The only callback that re-enters the graph.

    Phase 1 replaces the synthetic-message + _main_pinned round trip with a
    direct run_turn(..., on_busy="wait") call, so the observable contract is what
    matters: a prompt naming the RFQ, run against the pinned thread.
    """

    async def test_reenters_the_graph_with_a_prompt_naming_the_rfq(self, rfq):
        await rfq_actions.on_rfq_find_all_suppliers(action(rfq_id="RFQ-2026-0042"))

        assert len(rfq.reentries) == 1
        prompt, thread_id = rfq.reentries[0]
        assert "RFQ-2026-0042" in prompt
        assert thread_id == "thread-abc"

    async def test_missing_rfq_id_still_reenters_with_a_placeholder(self, rfq):
        """Characterisation, not endorsement.

        Unlike its siblings there is no guard, so a missing rfq_id sends the
        agent a prompt containing '???'. Worth revisiting during Phase 1.
        """
        await rfq_actions.on_rfq_find_all_suppliers(action())

        assert len(rfq.reentries) == 1
        assert "???" in rfq.reentries[0][0]
