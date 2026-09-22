"""Concurrent work on one thread must not clobber the other's output.

Two regressions this pins, both hit in real use during Phase 1 Step 6 testing:
clicking an RFQ button and then typing a chat message ran two workers at once,
and they fought over a single `active_msg` session key and a single
"agent working" badge.
"""

import asyncio

import pytest

import includes.agent_bridge as bridge


class TestActiveMessageIsPerRun:
    """`active_message` lives on the context, not in shared session state."""

    @pytest.mark.asyncio
    async def test_two_contexts_on_one_thread_keep_separate_messages(self, make_chat_ctx):
        a = make_chat_ctx(thread_id="thread-1")
        b = make_chat_ctx(thread_id="thread-1")

        a.active_message = await a.say("from the button")
        b.active_message = await b.say("from the chat")

        assert a.active_message is not b.active_message
        assert a.active_message.content == "from the button"

        # The second run finishing must not blank the first run's handle.
        b.active_message = None
        assert a.active_message is not None

    @pytest.mark.asyncio
    async def test_streaming_goes_to_the_right_run(self, make_chat_ctx):
        from includes.chat.context import chat_context
        from includes.tools.quote_tools import _stream_to_user

        button = make_chat_ctx(thread_id="thread-1")
        button.active_message = await button.say("")

        chat = make_chat_ctx(thread_id="thread-1")
        chat.active_message = await chat.say("")

        with chat_context(button):
            await _stream_to_user("button output")
        with chat_context(chat):
            await _stream_to_user("chat output")

        assert button.active_message.content == "button output"
        assert chat.active_message.content == "chat output"

    @pytest.mark.asyncio
    async def test_stream_is_a_no_op_when_no_run_is_active(self, chat_ctx):
        from includes.chat.context import chat_context
        from includes.tools.quote_tools import _stream_to_user

        with chat_context(chat_ctx):
            await _stream_to_user("nowhere to go")  # must not raise

        assert chat_ctx.messages == []

    @pytest.mark.asyncio
    async def test_runner_clears_its_own_handle(self, chat_ctx):
        from includes.chat.runner import run_turn

        class _Graph:
            async def aget_state(self, config):
                return None

            def astream_events(self, inputs, config=None, version=None):
                async def _gen():
                    return
                    yield
                return _gen()

        await run_turn("hi", chat_ctx, graph=_Graph())
        assert chat_ctx.active_message is None
