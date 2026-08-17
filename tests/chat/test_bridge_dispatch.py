"""Characterisation tests for agent_bridge.dispatch_action.

This is the dashboard -> chat RPC path, and it was the largest untested surface
in the bridge: tests/test_agent_bridge.py covers handle_bridge_request but stops
short of the session lookup, context init, thread pinning and locking.

Phase 5 deletes all of this. Until then it is load-bearing, and Phase 1 moves the
handlers it dispatches to, so the dispatch contract needs pinning first.
"""

import asyncio
from types import SimpleNamespace

import pytest

import includes.agent_bridge as bridge


@pytest.fixture(autouse=True)
def clear_locks():
    """_session_locks is module state; stop it leaking between tests."""
    bridge._session_locks.clear()
    yield
    bridge._session_locks.clear()


@pytest.fixture
def fake_chainlit(monkeypatch):
    """Patch the chainlit internals dispatch_action imports inline.

    `chainlit.context` and `chainlit.user_session` resolve to LazyProxy objects
    that shadow their submodules, and touching an attribute on the proxy resolves
    it against a live context. Go through sys.modules to reach the real modules.
    """
    import sys

    import chainlit.config
    import chainlit.session

    import includes.chat.rfq_actions as rfq_module

    context_mod = sys.modules["chainlit.context"]
    user_session_mod = sys.modules["chainlit.user_session"]

    sessions: dict = {}
    callbacks: dict = {}
    rfq_handlers: dict = {}
    session_writes: dict = {}
    contexts: list = []

    def _init_ws_context(s):
        contexts.append(s)
        # The real one sets the context var; do the same so cl.context.session
        # resolves for anything the handler builds. Scoped to the test's own
        # task context, so it does not leak between tests.
        context_mod.context_var.set(SimpleNamespace(session=s))

    monkeypatch.setattr(
        chainlit.session.WebsocketSession, "get_by_id",
        staticmethod(lambda sid: sessions.get(sid)),
    )
    monkeypatch.setattr(context_mod, "init_ws_context", _init_ws_context)
    monkeypatch.setattr(chainlit.config.config.code, "action_callbacks", callbacks)
    # Empty by default so each test decides which path it is exercising.
    monkeypatch.setattr(rfq_module, "RFQ_ACTIONS", rfq_handlers)
    monkeypatch.setattr(
        user_session_mod.UserSession, "set",
        lambda self, k, v: session_writes.__setitem__(k, v),
    )
    monkeypatch.setattr(
        user_session_mod.UserSession, "get",
        lambda self, k, default=None: session_writes.get(k, default),
    )

    def add_session(sid="sess-1", thread_id="thread-original"):
        sessions[sid] = SimpleNamespace(id=sid, thread_id=thread_id)
        return sessions[sid]

    yield SimpleNamespace(
        sessions=sessions,
        callbacks=callbacks,
        rfq_handlers=rfq_handlers,
        session_writes=session_writes,
        contexts=contexts,
        add_session=add_session,
    )


class TestSessionLookup:
    async def test_missing_session_returns_a_reload_hint(self, fake_chainlit):
        result = await bridge.dispatch_action("nope", "rfq_refresh", {})
        assert "error" in result
        assert "reload" in result["error"].lower()

    async def test_found_session_initialises_the_chainlit_context(self, fake_chainlit):
        session = fake_chainlit.add_session()

        async def _cb(action):
            return None

        fake_chainlit.callbacks["rfq_refresh"] = _cb

        result = await bridge.dispatch_action("sess-1", "rfq_refresh", {})
        assert result == {"success": True}
        assert fake_chainlit.contexts == [session]


class TestThreadPinning:
    async def test_thread_id_in_payload_pins_the_session(self, fake_chainlit):
        session = fake_chainlit.add_session(thread_id="thread-original")
        seen = {}

        async def _cb(action):
            seen["thread_id"] = session.thread_id

        fake_chainlit.callbacks["rfq_find_suppliers"] = _cb

        await bridge.dispatch_action(
            "sess-1", "rfq_find_suppliers", {"_thread_id": "thread-target"},
        )

        assert seen["thread_id"] == "thread-target"
        assert fake_chainlit.session_writes["thread_id"] == "thread-target"

    async def test_no_thread_id_leaves_the_session_alone(self, fake_chainlit):
        session = fake_chainlit.add_session(thread_id="thread-original")

        async def _cb(action):
            return None

        fake_chainlit.callbacks["rfq_refresh"] = _cb

        await bridge.dispatch_action("sess-1", "rfq_refresh", {})

        assert session.thread_id == "thread-original"
        assert "thread_id" not in fake_chainlit.session_writes


class TestCallbackDispatch:
    async def test_payload_reaches_the_callback(self, fake_chainlit):
        fake_chainlit.add_session()
        received = {}

        async def _cb(action):
            received["name"] = action.name
            received["payload"] = action.payload

        fake_chainlit.callbacks["rfq_identify_items"] = _cb

        await bridge.dispatch_action("sess-1", "rfq_identify_items", {"rfq_id": "RFQ-1"})

        assert received["name"] == "rfq_identify_items"
        assert received["payload"]["rfq_id"] == "RFQ-1"

    async def test_callback_exception_becomes_an_error_result(self, fake_chainlit):
        fake_chainlit.add_session()

        async def _cb(action):
            raise ValueError("supplier lookup exploded")

        fake_chainlit.callbacks["rfq_find_suppliers"] = _cb

        result = await bridge.dispatch_action("sess-1", "rfq_find_suppliers", {})

        assert "error" in result
        assert "supplier lookup exploded" in result["error"]

    async def test_unknown_action_is_reported(self, fake_chainlit):
        fake_chainlit.add_session()
        result = await bridge.dispatch_action("sess-1", "no_such_action", {})
        assert "Unknown action" in result["error"]


class TestRfqHandlerDispatch:
    """Phase 1 Step 8: RFQ handlers are called directly, not via Chainlit."""

    async def test_handler_receives_the_payload_and_a_context(self, fake_chainlit):
        fake_chainlit.add_session()
        received = {}

        async def _handler(payload, ctx):
            received["payload"] = payload
            received["thread_id"] = ctx.thread_id

        fake_chainlit.rfq_handlers["rfq_refresh"] = _handler

        result = await bridge.dispatch_action(
            "sess-1", "rfq_refresh", {"rfq_id": "RFQ-1", "_thread_id": "thread-target"}
        )

        assert result == {"success": True}
        assert received["payload"]["rfq_id"] == "RFQ-1"
        assert received["thread_id"] == "thread-target"

    async def test_the_context_var_is_bound_for_deep_tool_calls(self, fake_chainlit):
        """Tools below the handler read the ContextVar rather than an argument."""
        from includes.chat.context import try_get_chat_context

        fake_chainlit.add_session()
        seen = {}

        async def _handler(payload, ctx):
            seen["bound"] = try_get_chat_context() is ctx

        fake_chainlit.rfq_handlers["rfq_refresh"] = _handler

        await bridge.dispatch_action("sess-1", "rfq_refresh", {})
        assert seen["bound"] is True
        assert try_get_chat_context() is None, "the bind must not leak"

    async def test_handler_exception_becomes_an_error_result(self, fake_chainlit):
        fake_chainlit.add_session()

        async def _handler(payload, ctx):
            raise ValueError("handler exploded")

        fake_chainlit.rfq_handlers["rfq_find_suppliers"] = _handler

        result = await bridge.dispatch_action("sess-1", "rfq_find_suppliers", {})
        assert "handler exploded" in result["error"]

    async def test_the_bind_unwinds_even_when_the_handler_raises(self, fake_chainlit):
        from includes.chat.context import try_get_chat_context

        fake_chainlit.add_session()

        async def _handler(payload, ctx):
            raise ValueError("boom")

        fake_chainlit.rfq_handlers["rfq_refresh"] = _handler

        await bridge.dispatch_action("sess-1", "rfq_refresh", {})
        assert try_get_chat_context() is None

    async def test_rfq_handlers_take_precedence_over_chainlit_callbacks(self, fake_chainlit):
        """Both registries hold the name; the direct handler must win."""
        fake_chainlit.add_session()
        called: list[str] = []

        async def _handler(payload, ctx):
            called.append("handler")

        async def _callback(action):
            called.append("callback")

        fake_chainlit.rfq_handlers["rfq_refresh"] = _handler
        fake_chainlit.callbacks["rfq_refresh"] = _callback

        await bridge.dispatch_action("sess-1", "rfq_refresh", {})
        assert called == ["handler"]

    async def test_non_rfq_actions_still_reach_the_chainlit_callback(self, fake_chainlit):
        """Lifecycle actions (cancel_job, stop_agent, ...) stay in app.py."""
        fake_chainlit.add_session()
        called: list[str] = []

        async def _callback(action):
            called.append(action.name)

        fake_chainlit.callbacks["cancel_job"] = _callback

        result = await bridge.dispatch_action("sess-1", "cancel_job", {"job_id": "j1"})
        assert result == {"success": True}
        assert called == ["cancel_job"]


class TestSessionLock:
    async def test_actions_on_one_session_do_not_interleave(self, fake_chainlit):
        """The lock exists so concurrent actions cannot race on thread pinning."""
        fake_chainlit.add_session()
        order: list[str] = []

        async def _cb(action):
            order.append(f"enter-{action.payload['n']}")
            await asyncio.sleep(0.01)
            order.append(f"exit-{action.payload['n']}")

        fake_chainlit.callbacks["slow"] = _cb

        await asyncio.gather(
            bridge.dispatch_action("sess-1", "slow", {"n": 1}),
            bridge.dispatch_action("sess-1", "slow", {"n": 2}),
        )

        # Whichever runs first must finish before the other starts.
        assert order in (
            ["enter-1", "exit-1", "enter-2", "exit-2"],
            ["enter-2", "exit-2", "enter-1", "exit-1"],
        )

    async def test_different_sessions_run_concurrently(self, fake_chainlit):
        fake_chainlit.add_session("sess-1")
        fake_chainlit.add_session("sess-2")
        order: list[str] = []

        async def _cb(action):
            order.append(f"enter-{action.payload['n']}")
            await asyncio.sleep(0.01)
            order.append(f"exit-{action.payload['n']}")

        fake_chainlit.callbacks["slow"] = _cb

        await asyncio.gather(
            bridge.dispatch_action("sess-1", "slow", {"n": 1}),
            bridge.dispatch_action("sess-2", "slow", {"n": 2}),
        )

        assert order[:2] == ["enter-1", "enter-2"] or order[:2] == ["enter-2", "enter-1"]
