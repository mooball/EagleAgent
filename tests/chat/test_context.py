"""ChainlitChatContext maps the protocol onto cl.*; FakeChatContext records it."""

import pytest

from includes.chat.context import ActionSpec, ChatContext
from includes.chat.context_chainlit import ChainlitChatContext


@pytest.fixture
def cl_ctx(patch_cl):
    """A ChainlitChatContext wired to a fake `cl`. Returns (ctx, recorder)."""
    import includes.chat.context_chainlit as mod

    recorder = patch_cl(mod)
    recorder.session["thread_id"] = "thread-abc"
    recorder.session["user_id"] = "tester@example.com"
    recorder.session["chat_profile"] = "Eagle Agent"
    return ChainlitChatContext.from_session(), recorder


# --- protocol conformance -------------------------------------------------

def test_both_implementations_satisfy_the_protocol(cl_ctx, chat_ctx):
    ctx, _ = cl_ctx
    assert isinstance(ctx, ChatContext)
    assert isinstance(chat_ctx, ChatContext)


def test_from_session_reads_identity_off_the_session(cl_ctx):
    ctx, _ = cl_ctx
    assert ctx.thread_id == "thread-abc"
    assert ctx.user_email == "tester@example.com"
    assert ctx.agent == "eagle"


@pytest.mark.parametrize(
    "profile,expected",
    [
        ("Eagle Agent", "eagle"),
        ("EagleAgent", "eagle"),
        ("System Admin", "eagle"),
        ("Research Agent", "research"),
        ("Internal Agent", "internal"),
        (None, "eagle"),
        ("Something Else", "eagle"),
    ],
)
def test_chat_profile_maps_to_an_agent_key(patch_cl, profile, expected):
    import includes.chat.context_chainlit as mod

    recorder = patch_cl(mod)
    recorder.session["chat_profile"] = profile
    assert ChainlitChatContext.from_session().agent == expected


# --- say ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_say_sends_a_message(cl_ctx):
    ctx, recorder = cl_ctx
    handle = await ctx.say("hello", author="EagleAgent")

    assert len(recorder.messages) == 1
    sent = recorder.messages[0]
    assert sent.content == "hello"
    assert sent.author == "EagleAgent"
    assert sent.sent is True
    assert handle.content == "hello"
    assert handle.id == sent.id


@pytest.mark.asyncio
async def test_say_converts_action_specs_to_cl_actions(cl_ctx):
    ctx, recorder = cl_ctx
    await ctx.say(
        "pick one",
        actions=[
            ActionSpec(name="rfq_refresh", label="Refresh", payload={"rfq_id": "R-1"}),
            ActionSpec(name="rfq_dismiss", label="Dismiss", tooltip="Hide this"),
        ],
    )

    actions = recorder.messages[0].actions
    assert [a.name for a in actions] == ["rfq_refresh", "rfq_dismiss"]
    assert [a.label for a in actions] == ["Refresh", "Dismiss"]
    assert actions[0].payload == {"rfq_id": "R-1"}
    assert actions[0].description == ""
    assert actions[1].description == "Hide this"


@pytest.mark.asyncio
async def test_say_without_author_or_actions_omits_them(cl_ctx):
    ctx, recorder = cl_ctx
    await ctx.say("plain")

    assert recorder.messages[0].author is None
    assert recorder.messages[0].actions == []


@pytest.mark.asyncio
async def test_message_handle_streams_and_updates(cl_ctx):
    ctx, recorder = cl_ctx
    handle = await ctx.say("")

    await handle.stream("ab")
    await handle.stream("cd")
    await handle.update()

    assert recorder.messages[0].tokens == ["ab", "cd"]
    assert handle.content == "abcd"
    assert recorder.messages[0].updated is True

    await handle.remove()
    assert recorder.messages[0].removed is True


# --- thread pinning -------------------------------------------------------

@pytest.mark.asyncio
async def test_say_does_not_touch_the_session_when_threads_match(cl_ctx):
    ctx, recorder = cl_ctx
    await ctx.say("hi")
    assert recorder.session["thread_id"] == "thread-abc"
    assert recorder.module.context.session.thread_id == "thread-abc"


@pytest.mark.asyncio
async def test_say_pins_back_when_the_session_moved_to_another_thread(cl_ctx):
    ctx, recorder = cl_ctx
    # Simulate on_chat_resume navigating away mid-callback.
    recorder.session["thread_id"] = "thread-other"
    recorder.module.context.session.thread_id = "thread-other"

    observed = {}

    original = recorder.module.Message

    def _spy(**kwargs):
        observed["thread_id"] = recorder.session["thread_id"]
        observed["session_thread_id"] = recorder.module.context.session.thread_id
        return original(**kwargs)

    recorder.module.Message = _spy

    await ctx.say("late reply")

    assert observed == {"thread_id": "thread-abc", "session_thread_id": "thread-abc"}
    # ...and the session is restored afterwards.
    assert recorder.session["thread_id"] == "thread-other"
    assert recorder.module.context.session.thread_id == "thread-other"


@pytest.mark.asyncio
async def test_pinning_is_restored_even_if_the_send_raises(cl_ctx):
    ctx, recorder = cl_ctx
    recorder.session["thread_id"] = "thread-other"
    recorder.module.context.session.thread_id = "thread-other"

    def _boom(**kwargs):
        raise RuntimeError("send failed")

    recorder.module.Message = _boom

    with pytest.raises(RuntimeError):
        await ctx.say("nope")

    assert recorder.session["thread_id"] == "thread-other"
    assert recorder.module.context.session.thread_id == "thread-other"


# --- image ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_image_sends_an_inline_element(cl_ctx):
    ctx, recorder = cl_ctx
    await ctx.image("/tmp/shot.png", name="Browser Screenshot")

    msg = recorder.messages[0]
    assert msg.content == "📸"
    assert len(msg.elements) == 1
    assert msg.elements[0].path == "/tmp/shot.png"
    assert msg.elements[0].name == "Browser Screenshot"
    assert msg.elements[0].display == "inline"


# --- side channels --------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_dashboard_delegates_to_the_bridge(cl_ctx, monkeypatch):
    ctx, _ = cl_ctx
    calls = []

    async def _notify(command, payload=None):
        calls.append((command, payload))

    monkeypatch.setattr("includes.agent_bridge.notify_dashboard", _notify)
    await ctx.notify_dashboard("dashboard_refresh")
    await ctx.notify_dashboard("agent_working", {"label": "Finding suppliers"})

    assert calls == [
        ("dashboard_refresh", None),
        ("agent_working", {"label": "Finding suppliers"}),
    ]


@pytest.mark.asyncio
async def test_rename_thread_uses_the_pinned_thread_id(cl_ctx):
    ctx, recorder = cl_ctx
    renames = []

    class _DL:
        async def update_thread(self, thread_id, name):
            renames.append((thread_id, name))

    recorder.module.data._data_layer = _DL()
    # The session has since moved on; the rename must still target thread-abc.
    recorder.session["thread_id"] = "thread-other"

    await ctx.rename_thread("RFQ-1 — Acme")
    assert renames == [("thread-abc", "RFQ-1 — Acme")]


@pytest.mark.asyncio
async def test_rename_thread_is_a_no_op_without_a_data_layer(cl_ctx):
    ctx, recorder = cl_ctx
    recorder.module.data._data_layer = None
    await ctx.rename_thread("whatever")  # must not raise


@pytest.mark.asyncio
async def test_rename_thread_swallows_data_layer_errors(cl_ctx):
    ctx, recorder = cl_ctx

    class _DL:
        async def update_thread(self, thread_id, name):
            raise RuntimeError("db down")

    recorder.module.data._data_layer = _DL()
    await ctx.rename_thread("whatever")  # must not raise


# --- scratch state --------------------------------------------------------

def test_get_and_set_go_through_the_user_session(cl_ctx):
    ctx, recorder = cl_ctx
    assert ctx.get("total_tokens_used", 0) == 0
    ctx.set("total_tokens_used", 42)
    assert recorder.session["total_tokens_used"] == 42
    assert ctx.get("total_tokens_used") == 42


# --- cancellation ---------------------------------------------------------

def test_cancelled_reflects_the_bridge_stop_flag(cl_ctx, monkeypatch):
    ctx, _ = cl_ctx
    assert ctx.cancelled is False

    monkeypatch.setattr("includes.agent_bridge.is_stop_requested", lambda sid: True)
    assert ctx.cancelled is True


def test_cancelled_is_false_when_the_session_has_no_id(patch_cl, monkeypatch):
    import includes.chat.context_chainlit as mod

    patch_cl(mod)
    ctx = ChainlitChatContext.from_session()

    def _boom(session_id):
        raise RuntimeError("no session")

    monkeypatch.setattr("includes.agent_bridge.is_stop_requested", _boom)
    assert ctx.cancelled is False


# --- the fake -------------------------------------------------------------

@pytest.mark.asyncio
async def test_fake_records_faithfully(make_chat_ctx):
    ctx = make_chat_ctx(thread_id="t1", user_email="a@b.c", agent="research")

    handle = await ctx.say("one", author="System")
    await ctx.say("two", actions=[ActionSpec(name="go", label="Go")])
    await handle.stream("!")
    await ctx.image("/tmp/x.png", name="X")
    await ctx.notify_dashboard("dashboard_refresh")
    await ctx.rename_thread("New name")
    ctx.set("k", "v")

    assert ctx.texts == ["one!", "two"]
    assert ctx.messages[0].author == "System"
    assert ctx.action_names == ["go"]
    assert ctx.images == [("/tmp/x.png", "X")]
    assert ctx.dashboard_calls == [("dashboard_refresh", None)]
    assert ctx.thread_names == ["New name"]
    assert ctx.get("k") == "v"
    assert ctx.get("missing", "dflt") == "dflt"
    assert ctx.cancelled is False


@pytest.mark.asyncio
async def test_fake_message_handle_can_simulate_a_dead_socket(chat_ctx):
    handle = await chat_ctx.say("x")
    handle.fail_on_update = True
    with pytest.raises(RuntimeError):
        await handle.update()


def test_fake_can_be_put_into_the_cancelled_state(chat_ctx):
    assert chat_ctx.cancelled is False
    chat_ctx.request_stop()
    assert chat_ctx.cancelled is True
