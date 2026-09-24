"""Tests for the transport-neutral SSE action dispatch (final-leg P1).

Covers ``dispatch_action_to_thread`` (ownership, unknown action, busy,
happy path, handler errors) and the ``chat_ui`` branch of
``handle_bridge_request`` (hint + allowlist routing, fall-through).
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import includes.agent_bridge as bridge
import includes.dashboard.routes.chat_ui as chat_ui


@pytest.fixture(autouse=True)
def clear_runs():
    chat_ui._active_runs.clear()
    yield
    chat_ui._active_runs.clear()


USER = {"email": "tom@eagle-exports.com", "name": "Tom"}


def _patch_graph(monkeypatch):
    monkeypatch.setattr("includes.graph.setup_globals", AsyncMock())
    graph = MagicMock()
    spec = MagicMock()
    spec.graph.return_value = graph
    monkeypatch.setattr("includes.dashboard.routes.chat_ui.resolve", lambda key: spec)
    return graph


def _patch_scratch(monkeypatch):
    """Keep dispatch runs from touching the real thread metadata store."""
    monkeypatch.setattr(
        "includes.chat.transcript.get_thread_scratch", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        "includes.chat.transcript.save_thread_scratch", AsyncMock()
    )


class TestDispatchActionToThread:
    @patch("includes.chat.transcript.get_thread")
    async def test_unknown_thread(self, mock_get_thread):
        mock_get_thread.return_value = None
        result = await chat_ui.dispatch_action_to_thread(
            USER, "t1", "rfq_find_suppliers", {}
        )
        assert result["error"] == "Thread not found"
        assert result["status_code"] == 404

    @patch("includes.chat.transcript.get_thread")
    async def test_unknown_action(self, mock_get_thread):
        mock_get_thread.return_value = {"id": "t1"}
        result = await chat_ui.dispatch_action_to_thread(
            USER, "t1", "not_a_real_action", {}
        )
        assert result["status_code"] == 422

    @patch("includes.chat.transcript.get_thread")
    async def test_busy_thread_rejected(self, mock_get_thread):
        mock_get_thread.return_value = {"id": "t1"}
        task = MagicMock()
        task.done.return_value = False
        chat_ui._active_runs["t1"] = {"queue": MagicMock(), "task": task}
        result = await chat_ui.dispatch_action_to_thread(
            USER, "t1", "rfq_find_suppliers", {}
        )
        assert result["status_code"] == 409

    async def test_happy_path_runs_handler_and_streams_done(self, monkeypatch):
        monkeypatch.setattr(
            "includes.chat.transcript.get_thread",
            AsyncMock(return_value={"id": "t1"}),
        )
        _patch_scratch(monkeypatch)
        graph = _patch_graph(monkeypatch)

        calls = []

        async def fake_handler(payload, ctx):
            calls.append((payload, ctx.thread_id, ctx.get("active_graph")))

        monkeypatch.setattr(
            chat_ui, "_action_handler", lambda name: (fake_handler, "rfq")
        )

        result = await chat_ui.dispatch_action_to_thread(
            USER, "t1", "x", {"rfq_id": "RFQ-1"}
        )
        assert result == {"started": True, "thread_id": "t1"}

        run = chat_ui._active_runs.get("t1")
        assert run is not None
        await run["task"]

        # Handler ran with the payload, thread id and a seeded active_graph.
        assert calls == [({"rfq_id": "RFQ-1"}, "t1", graph)]
        assert run["queue"].get_nowait()["event"] == "done"
        # The record is deliberately retained so a late stream can still drain
        # this run (see _finish_run) — but it must no longer count as live.
        assert chat_ui._active_runs.get("t1") is run
        assert run["task"].done()
        assert await chat_ui.live_threads_for_user(USER) == []

    async def test_handler_exception_emits_error_then_done(self, monkeypatch):
        monkeypatch.setattr(
            "includes.chat.transcript.get_thread",
            AsyncMock(return_value={"id": "t1"}),
        )
        _patch_scratch(monkeypatch)
        _patch_graph(monkeypatch)

        async def boom(payload, ctx):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            chat_ui, "_action_handler", lambda name: (boom, "rfq")
        )

        result = await chat_ui.dispatch_action_to_thread(USER, "t1", "x", {})
        assert result["started"] is True
        run = chat_ui._active_runs["t1"]
        await run["task"]

        # Belt-and-braces: the dashboard badge clears (agent_done) before the
        # error toast, so a failed action can never leave the badge spinning.
        first = run["queue"].get_nowait()
        assert first["event"] == "dashboard"
        assert first["data"]["command"] == "agent_done"
        assert run["queue"].get_nowait()["event"] == "error"
        assert run["queue"].get_nowait()["event"] == "done"
        # Retained for a late stream, but no longer live.
        assert run["task"].done()
        assert await chat_ui.live_threads_for_user(USER) == []

    async def test_finished_run_stays_drainable_for_a_late_stream(self, monkeypatch):
        """A late-attaching stream must still collect a finished run's events.

        Popping the record at completion lost them forever — that is how a
        dashboard action lost its ``dashboard_refresh`` (stale RFQ page) and
        ``agent_done`` (badge left spinning until its timeout).
        """
        monkeypatch.setattr(
            "includes.chat.transcript.get_thread",
            AsyncMock(return_value={"id": "t1"}),
        )
        _patch_scratch(monkeypatch)
        _patch_graph(monkeypatch)

        async def fake_handler(payload, ctx):
            await ctx.notify_dashboard("dashboard_refresh")

        monkeypatch.setattr(
            chat_ui, "_action_handler", lambda name: (fake_handler, "rfq")
        )

        await chat_ui.dispatch_action_to_thread(USER, "t1", "x", {})
        run = chat_ui._active_runs["t1"]
        await run["task"]  # the run finishes BEFORE any stream attaches

        # The record survives, so the queued events are still collectable.
        assert chat_ui._active_runs.get("t1") is run
        drained = []
        while not run["queue"].empty():
            drained.append(run["queue"].get_nowait()["event"])
        assert drained == ["dashboard", "done"]

    async def test_finished_run_is_pruned_once_the_grace_period_elapses(
        self, monkeypatch
    ):
        """Retention is bounded — a stale record is dropped on the next access."""
        monkeypatch.setattr(
            "includes.chat.transcript.get_thread",
            AsyncMock(return_value={"id": "t1"}),
        )
        _patch_scratch(monkeypatch)
        _patch_graph(monkeypatch)

        async def fake_handler(payload, ctx):
            return None

        monkeypatch.setattr(
            chat_ui, "_action_handler", lambda name: (fake_handler, "rfq")
        )

        await chat_ui.dispatch_action_to_thread(USER, "t1", "x", {})
        run = chat_ui._active_runs["t1"]
        await run["task"]

        # Pretend the grace period has elapsed.
        run["finished_at"] = time.monotonic() - chat_ui._FINISHED_RUN_GRACE_SECONDS - 1
        chat_ui._prune_finished_runs()

        assert "t1" not in chat_ui._active_runs

    async def test_registry_action_goes_through_permission_check(self, monkeypatch):
        """Registry actions (new_conversation, cancel_job…) must not skip the
        admin_only gate the Chainlit path enforces."""
        monkeypatch.setattr(
            "includes.chat.transcript.get_thread",
            AsyncMock(return_value={"id": "t1"}),
        )
        _patch_scratch(monkeypatch)
        _patch_graph(monkeypatch)

        dispatched = AsyncMock()
        monkeypatch.setattr("includes.chat.actions.dispatch_action", dispatched)

        result = await chat_ui.dispatch_action_to_thread(
            USER, "t1", "cancel_job", {"job_id": "j1"}
        )
        assert result["started"] is True
        run = chat_ui._active_runs["t1"]
        await run["task"]

        dispatched.assert_awaited_once()
        assert dispatched.await_args.args[0] == "cancel_job"
        assert dispatched.await_args.kwargs["payload"] == {"job_id": "j1"}
        assert run["queue"].get_nowait()["event"] == "done"

    async def test_admin_only_action_denied_for_non_admin(self, monkeypatch):
        monkeypatch.setattr(
            "includes.chat.transcript.get_thread",
            AsyncMock(return_value={"id": "t1"}),
        )
        _patch_scratch(monkeypatch)
        _patch_graph(monkeypatch)
        monkeypatch.setattr(
            "includes.chat.actions.config.get_admin_emails",
            lambda: ["admin@eagle-exports.com"],
        )

        result = await chat_ui.dispatch_action_to_thread(
            USER, "t1", "research_product_info", {}
        )
        assert result["started"] is True
        run = chat_ui._active_runs["t1"]
        await run["task"]

        events = []
        while not run["queue"].empty():
            events.append(run["queue"].get_nowait())
        said = [e for e in events if e["event"] == "message_start"]
        assert said and "permission" in said[0]["data"]["content"].lower()

    async def test_dispatch_uses_the_thread_agent(self, monkeypatch):
        """An action on a research thread must not re-enter the eagle graph."""
        monkeypatch.setattr(
            "includes.chat.transcript.get_thread",
            AsyncMock(return_value={"id": "t1", "metadata": {"agent": "research"}}),
        )
        _patch_scratch(monkeypatch)
        monkeypatch.setattr("includes.graph.setup_globals", AsyncMock())
        seen = []

        def fake_resolve(key):
            seen.append(key)
            spec = MagicMock()
            spec.key = key or "eagle"
            spec.graph.return_value = MagicMock()
            return spec

        monkeypatch.setattr(
            "includes.dashboard.routes.chat_ui.resolve", fake_resolve
        )

        captured = {}

        async def fake_handler(payload, ctx):
            captured["agent"] = ctx.agent

        monkeypatch.setattr(
            chat_ui, "_action_handler", lambda name: (fake_handler, "rfq")
        )

        await chat_ui.dispatch_action_to_thread(USER, "t1", "x", {})
        await chat_ui._active_runs["t1"]["task"]
        assert "research" in seen
        assert captured["agent"] == "research"
