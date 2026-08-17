"""Transport-neutral chat context.

Business logic talks to the user through a ``ChatContext`` instead of reaching
for ambient ``chainlit`` globals. Two delivery mechanisms are supported:

* **Explicit argument** — action callbacks, dispatchers, and anything whose
  call site we control.
* **ContextVar** (:func:`get_chat_context`) — deep tool calls that cannot be
  given an argument without rewriting every tool signature.

The ContextVar is ours, is set once per run, and is settable in tests. That is
the whole difference from ``cl.context``.

Implementations live in ``includes/chat/context_chainlit.py`` (production) and
``tests/fakes.py`` (``FakeChatContext``).
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ActionSpec",
    "MessageHandle",
    "ChatContext",
    "get_chat_context",
    "try_get_chat_context",
    "bind_chat_context",
    "reset_chat_context",
    "chat_context",
]


@dataclass(frozen=True)
class ActionSpec:
    """Transport-neutral action button.

    ``name`` is the handler key; ``payload`` is round-tripped back to the
    handler when the button is clicked.
    """

    name: str
    label: str
    payload: dict[str, Any] = field(default_factory=dict)
    tooltip: str | None = None


class MessageHandle(Protocol):
    """A message that can still be mutated after being sent."""

    id: str
    content: str
    author: str | None

    async def stream(self, token: str) -> None: ...

    async def update(self) -> None: ...

    async def remove(self) -> None: ...

    async def save(self) -> None:
        """Persist the message, even if the client connection has died."""
        ...


@runtime_checkable
class ChatContext(Protocol):
    """Everything business logic needs in order to talk to one conversation."""

    thread_id: str
    user_email: str
    agent: str  # "eagle" | "research" | "internal"

    async def say(
        self,
        text: str,
        *,
        actions: list[ActionSpec] | None = None,
        author: str | None = None,
        transient: bool = False,
    ) -> MessageHandle: ...

    async def image(self, path: str, *, name: str) -> None: ...

    async def notify_dashboard(self, command: str, payload: dict | None = None) -> None: ...

    async def rename_thread(self, name: str) -> None: ...

    # Per-run scratch state — replaces cl.user_session
    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any) -> None: ...

    @property
    def cancelled(self) -> bool: ...


_current: contextvars.ContextVar["ChatContext | None"] = contextvars.ContextVar(
    "eagle_chat_context", default=None
)


def get_chat_context() -> ChatContext:
    """Return the bound context, raising if there is none."""
    ctx = _current.get()
    if ctx is None:
        raise RuntimeError("No ChatContext bound — call bind_chat_context() first")
    return ctx


def try_get_chat_context() -> ChatContext | None:
    """Return the bound context, or ``None`` outside a run.

    For call sites whose current behaviour is a silent no-op when there is no
    Chainlit session (e.g. retry notifications, ``_stream_to_user``).
    """
    return _current.get()


def bind_chat_context(ctx: ChatContext | None) -> contextvars.Token:
    """Bind ``ctx`` for the current context. Pass the token to :func:`reset_chat_context`."""
    return _current.set(ctx)


def reset_chat_context(token: contextvars.Token) -> None:
    _current.reset(token)


class chat_context:
    """Context manager form of :func:`bind_chat_context`.

    ::

        with chat_context(ctx):
            await run_turn(...)
    """

    def __init__(self, ctx: ChatContext | None) -> None:
        self._ctx = ctx
        self._token: contextvars.Token | None = None

    def __enter__(self) -> "ChatContext | None":
        self._token = bind_chat_context(self._ctx)
        return self._ctx

    def __exit__(self, *exc_info: object) -> None:
        if self._token is not None:
            reset_chat_context(self._token)
            self._token = None
