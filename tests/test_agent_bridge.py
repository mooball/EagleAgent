"""Tests for includes/agent_bridge.py — dashboard → agent dispatch and stop.

Agent → Dashboard notifications are no longer a server-side concern: the SSE
transport queues them on the thread's stream. So this file covers the two
things the bridge still owns: routing a dashboard action into the bound
thread, and the cooperative-cancellation bookkeeping that the stop button and
long-running handlers rely on.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, patch

from includes.agent_bridge import (
    clear_stop,
    handle_bridge_request,
    is_stop_requested,
    register_task,
    request_stop,
    unregister_task,
)


def _request(body: dict):
    request = AsyncMock()
    request.json = AsyncMock(return_value=body)
    return request


class TestHandleBridgeRequest:
    async def test_requires_authentication(self):
        with patch("main.get_current_user", return_value=None):
            response = await handle_bridge_request(_request({}))
        assert response.status_code == 401

    async def test_invalid_json_body_is_rejected(self):
        request = AsyncMock()
        request.json = AsyncMock(side_effect=ValueError("bad json"))
        with patch("main.get_current_user", return_value={"email": "user@eagle.com"}):
            response = await handle_bridge_request(request)
        assert response.status_code == 400

    async def test_missing_action_name_is_rejected(self):
        with patch("main.get_current_user", return_value={"email": "user@eagle.com"}):
            response = await handle_bridge_request(_request({"action": {}}))
        assert response.status_code == 400

    async def test_missing_rfq_and_thread_is_rejected(self):
        with patch("main.get_current_user", return_value={"email": "user@eagle.com"}):
            response = await handle_bridge_request(
                _request({"action": {"name": "rfq_refresh", "payload": {}}})
            )
        assert response.status_code == 400

    async def test_missing_thread_hint_self_heals_from_the_rfq(self):
        """A brand-new RFQ has no bound thread yet. The server must resolve or
        create one for that RFQ rather than running against whichever
        conversation happened to be open."""
        dispatch = AsyncMock(return_value={"started": True, "thread_id": "new-thread"})
        with patch("main.get_current_user", return_value={"email": "user@eagle.com"}), \
             patch("includes.dashboard.routes.api._lookup_rfq_thread_id",
                   return_value="new-thread") as lookup, \
             patch("includes.dashboard.routes.chat_ui.dispatch_action_to_thread", dispatch):
            response = await handle_bridge_request(
                _request({"action": {"name": "rfq_refresh",
                                     "payload": {"rfq_id": "RFQ-9"}}})
            )

        assert response.status_code == 200
        lookup.assert_called_once_with("RFQ-9", "user@eagle.com")
        # Dispatched into the RFQ's own thread, and the client is told which.
        assert dispatch.await_args.args[1] == "new-thread"
        assert b"new-thread" in response.body

    async def test_self_heal_failure_is_reported(self):
        with patch("main.get_current_user", return_value={"email": "user@eagle.com"}), \
             patch("includes.dashboard.routes.api._lookup_rfq_thread_id",
                   return_value=None):
            response = await handle_bridge_request(
                _request({"action": {"name": "rfq_refresh",
                                     "payload": {"rfq_id": "RFQ-9"}}})
            )
        assert response.status_code == 500

    async def test_dispatches_into_the_bound_thread(self):
        request = _request({
            "action": {
                "name": "rfq_find_suppliers",
                "payload": {"rfq_id": "RFQ-123", "_thread_id": "thread-abc"},
            },
        })
        dispatch = AsyncMock(return_value={"started": True, "thread_id": "thread-abc"})

        with patch("main.get_current_user", return_value={"email": "user@eagle.com"}), \
             patch(
                 "includes.dashboard.routes.chat_ui.dispatch_action_to_thread",
                 dispatch,
             ):
            response = await handle_bridge_request(request)

        assert response.status_code == 200
        user, thread_id, action_name, payload = dispatch.await_args.args
        assert user["email"] == "user@eagle.com"
        assert thread_id == "thread-abc"
        assert action_name == "rfq_find_suppliers"
        assert payload["rfq_id"] == "RFQ-123"

    async def test_dispatcher_error_status_is_propagated(self):
        request = _request({
            "action": {"name": "rfq_refresh", "payload": {"_thread_id": "thread-abc"}},
        })
        dispatch = AsyncMock(return_value={"error": "Unknown action: nope", "status_code": 422})

        with patch("main.get_current_user", return_value={"email": "user@eagle.com"}), \
             patch(
                 "includes.dashboard.routes.chat_ui.dispatch_action_to_thread",
                 dispatch,
             ):
            response = await handle_bridge_request(request)

        assert response.status_code == 422

    async def test_non_dict_payload_does_not_crash(self):
        request = _request({
            "action": {"name": "rfq_refresh", "payload": ["not", "a", "dict"]},
        })
        with patch("main.get_current_user", return_value={"email": "user@eagle.com"}):
            response = await handle_bridge_request(request)
        assert response.status_code == 400


class TestStopAgentTargets:
    """Stop names exactly one thread.

    Runs are isolated per thread (multi-tasking across RFQs is normal), so a
    blanket cancel would kill unrelated work.
    """

    def _request(self, body):
        request = AsyncMock()
        request.json = AsyncMock(return_value=body)
        return request

    async def test_requires_authentication(self):
        from main import stop_agent
        with patch("main.get_current_user", return_value=None):
            response = await stop_agent(self._request({"thread_id": "t1"}))
        assert response.status_code == 401

    async def test_requires_a_thread_id(self):
        from main import stop_agent
        with patch("main.get_current_user", return_value={"email": "u@eagle.com"}):
            response = await stop_agent(self._request({}))
        assert response.status_code == 400
        assert b"thread_id" in response.body

    async def test_refuses_when_nothing_is_running_there(self):
        from main import stop_agent
        with patch("main.get_current_user", return_value={"email": "u@eagle.com"}), \
             patch("includes.dashboard.routes.chat_ui.live_threads_for_user",
                   AsyncMock(return_value=["other-thread"])):
            response = await stop_agent(self._request({"thread_id": "t1"}))
        assert response.status_code == 200
        assert b"not_running" in response.body

    async def test_stops_only_the_named_thread(self):
        from main import stop_agent

        stopped: list[str] = []

        async def _stop(key):
            stopped.append(key)
            return 1

        with patch("main.get_current_user", return_value={"email": "u@eagle.com"}), \
             patch("includes.dashboard.routes.chat_ui.live_threads_for_user",
                   AsyncMock(return_value=["t1", "t2"])), \
             patch("includes.dashboard.routes.chat_ui.cancel_key_for_thread",
                   lambda tid: tid), \
             patch("includes.agent_bridge.request_stop", _stop):
            response = await stop_agent(self._request({"thread_id": "t1"}))

        assert response.status_code == 200
        # t2 must be left running.
        assert stopped == ["t1"]


class TestCooperativeCancellation:
    """The stop flag is keyed per run; handlers poll it at safe break points."""

    async def test_flag_starts_clear(self):
        assert not is_stop_requested("sess-not-stopped")

    async def test_request_stop_sets_the_flag(self):
        await request_stop("sess-1")
        try:
            assert is_stop_requested("sess-1")
        finally:
            clear_stop("sess-1")

    async def test_clear_stop_resets_the_flag(self):
        await request_stop("sess-2")
        clear_stop("sess-2")
        assert not is_stop_requested("sess-2")

    async def test_request_stop_cancels_registered_tasks(self):
        async def _forever():
            await asyncio.sleep(30)

        task = asyncio.create_task(_forever())
        register_task(task, "sess-3")
        try:
            cancelled = await request_stop("sess-3")
            assert cancelled == 1
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            unregister_task(task, "sess-3")
            clear_stop("sess-3")

    async def test_finished_tasks_are_not_counted(self):
        async def _quick():
            return 1

        task = asyncio.create_task(_quick())
        await task
        register_task(task, "sess-4")
        try:
            assert await request_stop("sess-4") == 0
        finally:
            unregister_task(task, "sess-4")
            clear_stop("sess-4")

    async def test_unregister_removes_the_task(self):
        async def _forever():
            await asyncio.sleep(30)

        task = asyncio.create_task(_forever())
        register_task(task, "sess-5")
        unregister_task(task, "sess-5")
        try:
            assert await request_stop("sess-5") == 0
        finally:
            task.cancel()
            clear_stop("sess-5")
