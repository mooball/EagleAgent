"""Characterisation tests for the streaming loop in app.main().

These pin the behaviours that Phase 1 must preserve when the loop moves into
includes/chat/runner.py: buffered-stream discard, the fallback chain, token
accounting, and resilient persistence.

Driven by feeding a scripted astream_events sequence through a FakeGraph, so no
model, socket or database is involved.
"""

import pytest


@pytest.fixture
def run_main(fake_cl, stub_bridge, incoming, make_graph, monkeypatch):
    """Drive app.main() over a scripted event sequence, return the reply message."""
    import app

    async def _run(events, *, state_messages=None, content="hello", fail_update=False):
        graph = make_graph(events, state_messages=state_messages)
        fake_cl.session["thread_id"] = "thread-abc"
        fake_cl.session["user_id"] = "tester@example.com"
        fake_cl.session["active_graph"] = graph
        fake_cl.session["chat_profile"] = "Research Agent"  # skip the supplier-intent default

        monkeypatch.setattr(app, "is_help_request", lambda _t: False)

        before = len(fake_cl.messages)
        await app.main(incoming(content=content))

        # main() sends exactly one reply message; grab it and expose the graph.
        reply = fake_cl.messages[before]
        if fail_update:
            reply.fail_on_update = True
        return reply, graph

    return _run


def _body(reply):
    """Message content minus the token/timing footer main() appends."""
    return reply.content.split("<div")[0].strip()


class TestBufferedStreamDiscard:
    """Text emitted before a tool call is reasoning, not the answer."""

    async def test_text_before_tool_call_is_discarded(self, run_main, ev):
        reply, _ = await run_main([
            ev.chunk("let me think about this first"),
            ev.tool_start(),
            ev.tool_end(),
            ev.chunk("the real answer"),
            ev.model_end("the real answer"),
        ])
        assert _body(reply) == "the real answer"
        assert "let me think" not in reply.content

    async def test_text_without_tool_call_is_flushed(self, run_main, ev):
        reply, _ = await run_main([
            ev.chunk("a complete "),
            ev.chunk("answer"),
            ev.model_end("a complete answer"),
        ])
        assert _body(reply) == "a complete answer"

    async def test_thinking_blocks_never_reach_the_user(self, run_main, ev):
        reply, _ = await run_main([
            ev.chunk([
                {"type": "thinking", "thinking": "secret reasoning"},
                {"type": "text", "text": "visible"},
            ]),
            ev.model_end("visible"),
        ])
        assert "secret reasoning" not in reply.content
        assert "visible" in reply.content


class TestFallbackChain:
    """Three layers, in strict precedence order."""

    async def test_buffer_wins_when_present(self, run_main, ev):
        reply, _ = await run_main([
            ev.chunk("streamed text"),
            ev.model_end("different end text"),
        ])
        assert "streamed text" in reply.content

    async def test_model_end_text_used_when_nothing_streamed(self, run_main, ev):
        """Fires when callbacks don't propagate into sub-graphs."""
        reply, _ = await run_main([ev.model_end("recovered from model end")])
        assert "recovered from model end" in reply.content

    async def test_graph_state_used_when_buffer_and_end_text_empty(self, run_main):
        from langchain_core.messages import AIMessage

        reply, _ = await run_main(
            [],
            state_messages=[AIMessage(content="recovered from checkpoint")],
        )
        assert "recovered from checkpoint" in reply.content

    async def test_no_content_anywhere_does_not_crash(self, run_main):
        reply, _ = await run_main([])
        assert _body(reply) == ""


class TestTokenAccounting:
    async def test_usage_accumulates_across_model_calls(self, run_main, ev):
        reply, _ = await run_main([
            ev.chunk("part one "),
            ev.model_end("part one ", 100, 20, 120),
            ev.chunk("part two"),
            ev.model_end("part two", 200, 30, 230),
        ])
        assert "350" in reply.content       # total tokens
        assert "300" in reply.content       # prompt tokens
        assert "50" in reply.content        # completion tokens

    async def test_footer_lists_tools_used(self, run_main, ev):
        reply, _ = await run_main([
            ev.tool_start("search_products"),
            ev.tool_end("search_products"),
            ev.chunk("done"),
            ev.model_end("done"),
        ])
        assert "Search Products" in reply.content

    async def test_footer_present_without_token_data(self, run_main, ev):
        reply, _ = await run_main([ev.chunk("hi"), ev.model_end("hi")])
        assert "Total:" in reply.content


class TestRepetitionGuardInLoop:
    async def test_degenerate_stream_is_replaced_with_an_apology(self, run_main, ev):
        reply, _ = await run_main([ev.chunk("abcdefghij") for _ in range(60)])
        assert "Sorry, I encountered an issue" in reply.content
        assert "abcdefghijabcdefghij" not in _body(reply)


class TestCheckpointRepairIsApplied:
    """The planner is unit-tested separately; this pins the wiring."""

    async def test_dangling_tool_call_triggers_a_state_update(self, run_main, ev):
        from langchain_core.messages import AIMessage

        corrupt = AIMessage(
            content="",
            id="ai_1",
            tool_calls=[{"name": "lookup", "args": {}, "id": "call_1"}],
        )
        _, graph = await run_main(
            [ev.chunk("ok"), ev.model_end("ok")],
            state_messages=[corrupt],
        )
        assert graph.state_updates, "expected a repair write to the checkpoint"
        repaired = graph.state_updates[0]["messages"]
        assert repaired[0].tool_call_id == "call_1"
        assert "interrupted" in repaired[0].content

    async def test_clean_checkpoint_is_left_alone(self, run_main, ev):
        from langchain_core.messages import AIMessage, ToolMessage

        _, graph = await run_main(
            [ev.chunk("ok"), ev.model_end("ok")],
            state_messages=[
                AIMessage(content="", id="ai_1",
                          tool_calls=[{"name": "lookup", "args": {}, "id": "c1"}]),
                ToolMessage(content="result", tool_call_id="c1"),
            ],
        )
        assert graph.state_updates == []


class TestResilientPersistence:
    """If the socket died, the reply must still reach the data layer."""

    async def test_dead_socket_falls_back_to_the_data_layer(
        self, run_main, fake_cl, fake_data_layer, ev
    ):
        fake_cl.fail_updates = True
        reply, _ = await run_main([ev.chunk("an answer"), ev.model_end("an answer")])

        assert not reply.updated, "update() should have raised"
        assert len(fake_data_layer) == 1
        step = fake_data_layer[0]
        assert step["type"] == "assistant_message"
        assert "an answer" in step["output"]
        assert step["threadId"] == "thread-abc"

    async def test_empty_reply_is_not_persisted(
        self, run_main, fake_cl, fake_data_layer
    ):
        """The `msg.content.strip()` guard — no point storing a blank message."""
        fake_cl.fail_updates = True
        await run_main([])

        assert fake_data_layer == []

    async def test_healthy_socket_does_not_touch_the_data_layer(
        self, run_main, fake_data_layer, ev
    ):
        reply, _ = await run_main([ev.chunk("fine"), ev.model_end("fine")])

        assert reply.updated
        assert fake_data_layer == []
