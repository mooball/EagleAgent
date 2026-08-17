"""Chainlit implementation of :class:`~includes.chat.context.ChatContext`.

This is an adapter: it is one of the few modules allowed to import ``chainlit``.
It reproduces today's behaviour, including the thread-pinning that
``rfq_actions._send_pinned`` does — except the pinning now lives here, keyed off
an immutable ``thread_id`` captured at construction, rather than being sprinkled
through business logic.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import chainlit as cl

from includes.chat.context import ActionSpec

logger = logging.getLogger(__name__)

# Chat profile label -> agent key. Superseded by includes/agents/registry.py.
_PROFILE_TO_AGENT = {
    "Eagle Agent": "eagle",
    "EagleAgent": "eagle",
    "System Admin": "eagle",
    "Research Agent": "research",
    "Internal Agent": "internal",
}


class ChainlitMessageHandle:
    """Wraps a ``cl.Message`` behind the ``MessageHandle`` protocol."""

    def __init__(self, message: Any) -> None:
        self._message = message

    @property
    def id(self) -> str:
        return getattr(self._message, "id", "")

    @property
    def content(self) -> str:
        return getattr(self._message, "content", "")

    @property
    def raw(self) -> Any:
        """The underlying ``cl.Message``, for code not yet migrated."""
        return self._message

    async def stream(self, token: str) -> None:
        await self._message.stream_token(token)

    async def update(self) -> None:
        await self._message.update()

    async def remove(self) -> None:
        await self._message.remove()


class ChainlitChatContext:
    """A ``ChatContext`` backed by the current Chainlit session."""

    def __init__(
        self,
        *,
        thread_id: str,
        user_email: str,
        agent: str,
        session: Any,
    ) -> None:
        self.thread_id = thread_id
        self.user_email = user_email
        self.agent = agent
        self._session = session

    @classmethod
    def from_session(cls) -> "ChainlitChatContext":
        """Build a context from the ambient Chainlit session.

        Call this at the boundary only — lifecycle hooks, action-callback
        adapters, and the agent bridge.
        """
        session = cl.context.session
        thread_id = cl.user_session.get("thread_id") or getattr(session, "thread_id", "") or ""
        profile = cl.user_session.get("chat_profile")
        return cls(
            thread_id=thread_id,
            user_email=cl.user_session.get("user_id", "") or "",
            agent=_PROFILE_TO_AGENT.get(profile, "eagle"),
            session=session,
        )

    # -- messaging ---------------------------------------------------------

    @contextlib.contextmanager
    def _pinned(self):
        """Point the session at this context's thread for the duration.

        A no-op in the common case. It matters when ``on_chat_resume`` has
        moved the session to another thread while a long callback is in flight.
        """
        current = cl.user_session.get("thread_id")
        if current == self.thread_id:
            yield
            return

        prev_session_thread = getattr(self._session, "thread_id", None)
        cl.user_session.set("thread_id", self.thread_id)
        with contextlib.suppress(AttributeError):
            self._session.thread_id = self.thread_id
        try:
            yield
        finally:
            cl.user_session.set("thread_id", current)
            with contextlib.suppress(AttributeError):
                self._session.thread_id = prev_session_thread

    async def say(
        self,
        text: str,
        *,
        actions: list[ActionSpec] | None = None,
        author: str | None = None,
        transient: bool = False,
    ) -> ChainlitMessageHandle:
        """Send a message. ``transient`` is ignored on Chainlit (no such concept)."""
        cl_actions = [
            cl.Action(
                name=a.name,
                payload=a.payload,
                label=a.label,
                description=a.tooltip or "",
            )
            for a in (actions or [])
        ]
        kwargs: dict[str, Any] = {"content": text}
        if author is not None:
            kwargs["author"] = author
        if cl_actions:
            kwargs["actions"] = cl_actions

        with self._pinned():
            message = cl.Message(**kwargs)
            await message.send()
        return ChainlitMessageHandle(message)

    async def image(self, path: str, *, name: str) -> None:
        element = cl.Image(path=path, name=name, display="inline")
        with self._pinned():
            await cl.Message(content="📸", elements=[element]).send()

    # -- side channels -----------------------------------------------------

    async def notify_dashboard(self, command: str, payload: dict | None = None) -> None:
        from includes.agent_bridge import notify_dashboard

        await notify_dashboard(command, payload)

    async def rename_thread(self, name: str) -> None:
        try:
            data_layer = cl.data._data_layer
            if data_layer:
                await data_layer.update_thread(thread_id=self.thread_id, name=name)
        except Exception as e:
            logger.warning(f"Failed to name thread: {e}")

    # -- scratch state -----------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        return cl.user_session.get(key, default)

    def set(self, key: str, value: Any) -> None:
        cl.user_session.set(key, value)

    # -- cancellation ------------------------------------------------------

    @property
    def cancelled(self) -> bool:
        from includes.agent_bridge import is_stop_requested

        try:
            return is_stop_requested(self._session.id)
        except Exception:
            return False
