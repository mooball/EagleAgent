"""SSE ChatContext + message handle event contract."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def queue():
    return asyncio.Queue()


async def _drain(queue, n):
    return [await queue.get() for _ in range(n)]


class TestSseMessageHandle:
    async def test_stream_appends_and_emits_token(self, queue):
        from includes.chat.context_sse import SseMessageHandle

        h = SseMessageHandle("m1", content="", author="EagleAgent", queue=queue)
        await h.stream("Hel")
        await h.stream("lo")
        assert h.content == "Hello"
        events = await _drain(queue, 2)
        assert [e["event"] for e in events] == ["token", "token"]
        assert events[0]["data"] == {"id": "m1", "text": "Hel"}
        assert events[1]["data"] == {"id": "m1", "text": "lo"}

    async def test_update_emits_full_content(self, queue):
        from includes.chat.context_sse import SseMessageHandle

        h = SseMessageHandle("m1", content="old", author="EagleAgent", queue=queue)
        h.content = "new"
        with patch("includes.chat.context_sse.transcript.update_step", new=AsyncMock()):
            await h.update()
        (event,) = await _drain(queue, 1)
        assert event["event"] == "message_update"
        assert event["data"]["content"] == "new"

    async def test_remove_deletes_persisted_step(self, queue):
        from includes.chat.context_sse import SseMessageHandle

        h = SseMessageHandle("m1", content="", author="EagleAgent", queue=queue)
        delete = AsyncMock()
        with patch("includes.chat.context_sse.transcript.delete_step", new=delete):
            await h.remove()
        delete.assert_awaited_once_with("m1")
        (event,) = await _drain(queue, 1)
        assert event["event"] == "message_remove"

    async def test_save_persists_and_emits_end(self, queue):
        from includes.chat.context_sse import SseMessageHandle

        h = SseMessageHandle("m1", content="final", author="EagleAgent", queue=queue)
        update = AsyncMock()
        with patch("includes.chat.context_sse.transcript.update_step", new=update):
            await h.save()
        update.assert_awaited_once_with("m1", "final")
        (event,) = await _drain(queue, 1)
        assert event["event"] == "message_end"
        assert event["data"]["content"] == "final"


class TestSseChatContext:
    def _ctx(self, queue):
        from includes.chat.context_sse import SseChatContext

        return SseChatContext(
            thread_id="t1",
            user_email="tom@eagle-exports.com",
            agent="eagle",
            queue=queue,
            cancel_key="chat-ui:t1",
        )

    async def test_say_persists_then_emits_message_start(self, queue):
        create = AsyncMock(return_value="step-1")
        with patch("includes.chat.context_sse.transcript.create_step", new=create):
            handle = await self._ctx(queue).say("hi", author="EagleAgent")
        create.assert_awaited_once()
        assert create.call_args.kwargs["type_"] == "assistant_message"
        assert create.call_args.kwargs["output"] == "hi"
        (event,) = await _drain(queue, 1)
        assert event["event"] == "message_start"
        assert event["data"]["content"] == "hi"
        assert event["data"]["transient"] is False
        assert handle.id == event["data"]["id"]

    async def test_message_id_is_the_persisted_step_id(self, queue):
        """update/remove must address the row say() created, or the reply is
        never written back and the transcript keeps an empty step."""
        create = AsyncMock(return_value="step-1")
        update = AsyncMock()
        with patch("includes.chat.context_sse.transcript.create_step", new=create), \
             patch("includes.chat.context_sse.transcript.update_step", new=update):
            handle = await self._ctx(queue).say("")
            assert handle.id == "step-1"
            await handle.stream("answer")
            await handle.save()
        update.assert_awaited_with("step-1", "answer")

    async def test_transient_messages_are_not_persisted(self, queue):
        """Tool-progress lines are removed by the runner and must never end up
        in the transcript."""
        create = AsyncMock(return_value="step-1")
        delete = AsyncMock()
        with patch("includes.chat.context_sse.transcript.create_step", new=create), \
             patch("includes.chat.context_sse.transcript.delete_step", new=delete):
            handle = await self._ctx(queue).say("⏳ Using X…", transient=True)
            await handle.remove()
        create.assert_not_awaited()
        delete.assert_not_awaited()
        events = await _drain(queue, 2)
        assert events[0]["data"]["transient"] is True
        assert events[1]["event"] == "message_remove"

    async def test_say_persistence_failure_does_not_break_stream(self, queue):
        create = AsyncMock(side_effect=RuntimeError("db down"))
        with patch("includes.chat.context_sse.transcript.create_step", new=create):
            handle = await self._ctx(queue).say("hi")
        (event,) = await _drain(queue, 1)
        assert event["event"] == "message_start"
        assert handle.id == event["data"]["id"]

    async def test_scratch_state_is_per_context(self, queue):
        ctx = self._ctx(queue)
        assert ctx.get("x") is None
        ctx.set("x", 1)
        assert ctx.get("x") == 1
        other = self._ctx(queue)
        assert other.get("x") is None

    async def test_cancelled_reads_stop_flag(self, queue):
        ctx = self._ctx(queue)
        with patch("includes.agent_bridge.is_stop_requested", return_value=True):
            assert ctx.cancelled is True
        with patch("includes.agent_bridge.is_stop_requested", return_value=False):
            assert ctx.cancelled is False

    async def test_reset_cancel_clears_stop_flag(self, queue):
        ctx = self._ctx(queue)
        clear = MagicMock()
        with patch("includes.agent_bridge.clear_stop", new=clear):
            ctx.reset_cancel()
        clear.assert_called_once_with("chat-ui:t1")
