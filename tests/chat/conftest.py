"""Fakes for driving app.main() without Chainlit, a socket, or a real model.

Scoped to tests/chat so the ~800 existing tests are unaffected. The ad-hoc `cl`
mocks in tests/test_actions.py and tests/test_job_tools.py can migrate here once
Phase 1 gives the business logic an explicit ChatContext.
"""

import uuid
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage


class FakeMessage:
    """Stands in for cl.Message, recording everything instead of emitting it."""

    def __init__(self, content="", author=None, elements=None, actions=None, **kwargs):
        self.id = str(uuid.uuid4())
        self.content = content
        self.author = author
        self.elements = elements or []
        self.actions = actions or []
        self.command = None
        self.persisted = False
        self.created_at = None  # cl.Message exposes this; the persist fallback reads it
        self.sent = False
        self.updated = False
        self.removed = False
        self.tokens: list[str] = []
        # Set by tests to simulate a dead socket in the resilient-persist path.
        self.fail_on_update = False

    async def send(self):
        self.sent = True
        return self

    async def update(self):
        if self.fail_on_update:
            raise RuntimeError("socket closed")
        self.updated = True

    async def remove(self):
        self.removed = True

    async def stream_token(self, token):
        self.tokens.append(token)
        self.content += token


class FakeGraph:
    """Yields a scripted astream_events sequence and records state writes."""

    def __init__(self, events, state_messages=None):
        self._events = events
        self.state_messages = list(state_messages or [])
        self.state_updates: list[dict] = []

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": self.state_messages})

    async def aupdate_state(self, config, values):
        self.state_updates.append(values)

    def astream_events(self, inputs, config=None, version=None):
        async def _gen():
            for event in self._events:
                yield event

        return _gen()


# --- event builders -------------------------------------------------------

def stream_chunk(content):
    """An on_chat_model_stream event. `content` may be a str or content parts."""
    return {
        "event": "on_chat_model_stream",
        "name": "model",
        "tags": [],
        "data": {"chunk": SimpleNamespace(content=content)},
    }


def tool_start(name="search_products"):
    return {"event": "on_tool_start", "name": name, "tags": [], "data": {}}


def tool_end(name="search_products"):
    return {"event": "on_tool_end", "name": name, "tags": [], "data": {}}


def model_end(text="", prompt_tokens=0, completion_tokens=0, total_tokens=0):
    usage = None
    if total_tokens:
        usage = {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }
    output = AIMessage(content=text, usage_metadata=usage)
    return {
        "event": "on_chat_model_end",
        "name": "model",
        "tags": [],
        "data": {"output": output},
    }


# --- fixtures -------------------------------------------------------------

@pytest.fixture
def fake_cl(monkeypatch):
    """Replace app.cl with a recorder. Returns the recorder for assertions."""
    import app

    return _install_fake_cl(monkeypatch, app)


@pytest.fixture
def patch_cl(monkeypatch):
    """Install a fake `cl` into any module that does `import chainlit as cl`."""
    def _patch(module):
        return _install_fake_cl(monkeypatch, module)
    return _patch


def _install_fake_cl(monkeypatch, module):
    messages: list[FakeMessage] = []
    session_store: dict = {}
    recorder = SimpleNamespace(messages=messages, session=session_store, fail_updates=False)

    def _message_factory(**kwargs):
        m = FakeMessage(**kwargs)
        # Set at creation so main()'s own reply fails, simulating a dead socket.
        m.fail_on_update = recorder.fail_updates
        messages.append(m)
        return m

    user_session = SimpleNamespace(
        get=lambda k, default=None: session_store.get(k, default),
        set=lambda k, v: session_store.__setitem__(k, v),
    )
    context = SimpleNamespace(
        session=SimpleNamespace(id="session-0123456789", thread_id="thread-abc", user=None),
        emitter=SimpleNamespace(set_commands=_noop_async),
    )

    fake = SimpleNamespace(
        Message=_message_factory,
        user_session=user_session,
        context=context,
        Action=lambda **kw: SimpleNamespace(**kw),
    )
    monkeypatch.setattr(module, "cl", fake)

    recorder.module = fake
    return recorder


@pytest.fixture
def fake_data_layer(monkeypatch):
    """Capture steps written by the resilient-persist fallback."""
    import chainlit.data as cl_data

    written: list[dict] = []

    class _DL:
        async def create_step(self, step_dict):
            written.append(step_dict)

    monkeypatch.setattr(cl_data, "get_data_layer", lambda: _DL())
    return written


async def _noop_async(*args, **kwargs):
    return None


@pytest.fixture
def stub_bridge(monkeypatch):
    """Neutralise agent_bridge, which main() imports inline mid-function."""
    import includes.agent_bridge as bridge

    calls: list[tuple] = []

    async def _notify(command, payload=None):
        calls.append((command, payload))

    monkeypatch.setattr(bridge, "notify_dashboard", _notify)
    monkeypatch.setattr(bridge, "register_task", lambda *a, **k: None)
    monkeypatch.setattr(bridge, "unregister_task", lambda *a, **k: None)
    monkeypatch.setattr(bridge, "clear_stop", lambda *a, **k: None)
    monkeypatch.setattr(bridge, "is_stop_requested", lambda *a, **k: False)
    return calls


@pytest.fixture
def incoming():
    """A user message arriving at main()."""
    def _make(content="hello", command=None, elements=None):
        m = FakeMessage(content=content, elements=elements)
        m.command = command
        return m
    return _make


@pytest.fixture
def ev():
    """Builders for scripted astream_events sequences."""
    return SimpleNamespace(
        chunk=stream_chunk,
        tool_start=tool_start,
        tool_end=tool_end,
        model_end=model_end,
    )


@pytest.fixture
def make_graph():
    """Build a FakeGraph from a scripted event list."""
    return FakeGraph
