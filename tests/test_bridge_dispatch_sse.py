"""Tests for the transport-neutral SSE action dispatch (final-leg P1).

Covers ``dispatch_action_to_thread`` (ownership, unknown action, busy,
happy path, handler errors) and the ``chat_ui`` branch of
``handle_bridge_request`` (hint + allowlist routing, fall-through).
"""

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
        assert "t1" not in chat_ui._active_runs

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

        assert run["queue"].get_nowait()["event"] == "error"
        assert run["queue"].get_nowait()["event"] == "done"
        assert "t1" not in chat_ui._active_runs

    async def test_registry_handler_receives_ctx_first(self, monkeypatch):
        """Registry actions (new_conversation, cancel_job…) have the
        (ctx, payload=…) shape — dispatch must not call them RFQ-style."""
        monkeypatch.setattr(
            "includes.chat.transcript.get_thread",
            AsyncMock(return_value={"id": "t1"}),
        )
        _patch_scratch(monkeypatch)
        _patch_graph(monkeypatch)

        calls = []

        async def registry_handler(ctx, payload=None):
            calls.append((ctx.thread_id, payload))

        monkeypatch.setattr(
            chat_ui,
            "_action_handler",
            lambda name: (registry_handler, "registry"),
        )

        result = await chat_ui.dispatch_action_to_thread(
            USER, "t1", "cancel_job", {"job_id": "j1"}
        )
        assert result["started"] is True
        run = chat_ui._active_runs["t1"]
        await run["task"]
        assert calls == [("t1", {"job_id": "j1"})]
        assert run["queue"].get_nowait()["event"] == "done"


class TestHandleBridgeRequestRouting:
    @patch("main.get_current_user", return_value={"email": "tom@eagle-exports.com"})
    async def test_chat_ui_hint_routes_to_sse(self, mock_user, monkeypatch):
        monkeypatch.setattr(
            "config.settings.Config.CHAT_UI_BETA_USERS", "tom@eagle-exports.com"
        )
        dispatched = AsyncMock(return_value={"started": True, "thread_id": "t9"})
        monkeypatch.setattr(
            "includes.dashboard.routes.chat_ui.dispatch_action_to_thread", dispatched
        )
        request = AsyncMock()
        request.cookies = {}
        request.json = AsyncMock(
            return_value={
                "chat_ui": True,
                "action": {
                    "name": "rfq_find_suppliers",
                    "payload": {"_thread_id": "t9", "rfq_id": "RFQ-1"},
                },
            }
        )

        response = await bridge.handle_bridge_request(request)

        assert response.status_code == 200
        dispatched.assert_awaited_once()
        args = dispatched.await_args.args
        assert args[0]["email"] == "tom@eagle-exports.com"
        assert args[1] == "t9"
        assert args[2] == "rfq_find_suppliers"

    @patch("main.get_current_user", return_value={"email": "other@eagle-exports.com"})
    async def test_chat_ui_hint_non_allowlisted_falls_through(
        self, mock_user, monkeypatch
    ):
        monkeypatch.setattr(
            "config.settings.Config.CHAT_UI_BETA_USERS", "tom@eagle-exports.com"
        )
        request = AsyncMock()
        request.cookies = {}
        request.json = AsyncMock(
            return_value={
                "chat_ui": True,
                "action": {"name": "x", "payload": {"_thread_id": "t9"}},
            }
        )

        response = await bridge.handle_bridge_request(request)

        # No cookie → the unchanged Chainlit path 400s.
        assert response.status_code == 400
        assert "Chainlit session" in response.body.decode()

    @patch("main.get_current_user", return_value={"email": "tom@eagle-exports.com"})
    async def test_chat_ui_hint_without_thread_falls_through(
        self, mock_user, monkeypatch
    ):
        monkeypatch.setattr(
            "config.settings.Config.CHAT_UI_BETA_USERS", "tom@eagle-exports.com"
        )
        request = AsyncMock()
        request.cookies = {}
        request.json = AsyncMock(
            return_value={
                "chat_ui": True,
                "action": {"name": "x", "payload": {"rfq_id": "RFQ-1"}},
            }
        )

        response = await bridge.handle_bridge_request(request)

        assert response.status_code == 400

    @patch("includes.agent_bridge.dispatch_action")
    @patch("main.get_current_user", return_value={"email": "tom@eagle-exports.com"})
    async def test_no_hint_uses_chainlit_path(self, mock_user, mock_dispatch):
        mock_dispatch.return_value = {"success": True}
        request = AsyncMock()
        request.cookies = {"X-Chainlit-Session-id": "session-123"}
        request.json = AsyncMock(
            return_value={
                "action": {
                    "name": "rfq_find_suppliers",
                    "payload": {"rfq_id": "RFQ-1"},
                }
            }
        )

        response = await bridge.handle_bridge_request(request)

        assert response.status_code == 200
        mock_dispatch.assert_awaited_once_with(
            "session-123", "rfq_find_suppliers", {"rfq_id": "RFQ-1"}
        )
