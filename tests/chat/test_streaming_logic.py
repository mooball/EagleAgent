"""Characterisation tests for the logic extracted from app.py's streaming loop.

These pin behaviour that had no coverage before. Two of them (`TestExtractionIsNoOp`)
compare the extracted functions against verbatim copies of the original inline code,
which is what makes the Phase 0 extraction provably behaviour-preserving.

Thresholds asserted here are load-bearing. If one needs to change, that is a
deliberate decision, not a test fix.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from includes.chat.streaming_logic import (
    INTERRUPTED_TOOL_RESULT,
    detect_repetition,
    extract_ai_text,
    extract_chunk_texts,
    plan_checkpoint_repair,
    plan_resume_backfill,
)


def _ai_with_calls(*ids, content="", msg_id=None):
    return AIMessage(
        content=content,
        id=msg_id,
        tool_calls=[{"name": "lookup", "args": {}, "id": i} for i in ids],
    )


# ---------------------------------------------------------------------------
# 5.1 Checkpoint repair — app.py light/heavy thresholds
# ---------------------------------------------------------------------------

class TestPlanCheckpointRepair:
    def test_empty_history(self):
        assert plan_checkpoint_repair([]).strategy == "none"

    def test_no_tool_calls(self):
        messages = [HumanMessage(content="hi"), AIMessage(content="hello")]
        assert plan_checkpoint_repair(messages).strategy == "none"

    def test_all_tool_calls_answered(self):
        messages = [
            _ai_with_calls("call_1", "call_2", msg_id="ai_1"),
            ToolMessage(content="ok", tool_call_id="call_1"),
            ToolMessage(content="ok", tool_call_id="call_2"),
        ]
        assert plan_checkpoint_repair(messages).strategy == "none"

    def test_one_dangling_injects(self):
        messages = [_ai_with_calls("call_1", msg_id="ai_1")]
        plan = plan_checkpoint_repair(messages)
        assert plan.strategy == "inject"
        assert [tc["id"] for tc in plan.dangling] == ["call_1"]

    def test_two_dangling_injects(self):
        """Boundary: <= 2 is a light repair."""
        messages = [_ai_with_calls("call_1", "call_2", msg_id="ai_1")]
        plan = plan_checkpoint_repair(messages)
        assert plan.strategy == "inject"
        assert len(plan.dangling) == 2

    def test_three_dangling_removes(self):
        """Boundary: > 2 flips to the heavy repair."""
        messages = [_ai_with_calls("call_1", "call_2", "call_3", msg_id="ai_1")]
        plan = plan_checkpoint_repair(messages)
        assert plan.strategy == "remove"
        assert plan.corrupt_message_ids == ["ai_1"]

    def test_dangling_spread_across_messages(self):
        """The threshold counts calls, not messages."""
        messages = [
            _ai_with_calls("call_1", "call_2", msg_id="ai_1"),
            _ai_with_calls("call_3", msg_id="ai_2"),
        ]
        plan = plan_checkpoint_repair(messages)
        assert plan.strategy == "remove"
        assert plan.corrupt_message_ids == ["ai_1", "ai_2"]

    def test_partially_answered_message(self):
        """Only the unanswered calls count."""
        messages = [
            _ai_with_calls("call_1", "call_2", "call_3", msg_id="ai_1"),
            ToolMessage(content="ok", tool_call_id="call_1"),
            ToolMessage(content="ok", tool_call_id="call_2"),
        ]
        plan = plan_checkpoint_repair(messages)
        assert plan.strategy == "inject"
        assert [tc["id"] for tc in plan.dangling] == ["call_3"]

    def test_message_without_id_is_not_removable(self):
        """RemoveMessage needs an id, so id-less messages are excluded."""
        messages = [_ai_with_calls("c1", "c2", "c3", msg_id=None)]
        plan = plan_checkpoint_repair(messages)
        assert plan.strategy == "remove"
        assert plan.corrupt_message_ids == []

    def test_repair_message_text_is_stable(self):
        """The model keys off this wording to decide whether to retry."""
        assert INTERRUPTED_TOOL_RESULT == (
            "[Error: previous operation was interrupted. Please retry if needed.]"
        )


# ---------------------------------------------------------------------------
# 5.2 Repetition guard — all four conditions must hold
# ---------------------------------------------------------------------------

class TestDetectRepetition:
    def test_empty_buffer(self):
        assert detect_repetition([]) is None

    def test_exactly_50_chunks_is_below_threshold(self):
        """Boundary: the guard requires > 50, not >= 50."""
        assert detect_repetition(["abcdefghij"] * 50) is None

    def test_51_repeating_chunks_trips(self):
        assert detect_repetition(["abcdefghij"] * 51) is not None

    def test_short_tail_never_trips(self):
        """Tail must exceed 60 chars even with many chunks."""
        assert detect_repetition(["a"] * 55) is None

    def test_three_repeats_is_below_threshold(self):
        """Boundary: needs >= 4 occurrences in the window."""
        snippet = "0123456789012345678901234567890"  # 31 chars
        buffer = ["x" * 100] + [snippet] * 3 + ["y"] * 60
        assert detect_repetition(buffer) is None

    def test_realistic_repetitive_list_does_not_trip(self):
        """Token-sized chunks of repetitive prose stay under the threshold.

        An agent listing suppliers in a fixed structure is the realistic
        false-positive risk. It does not fire, because the 30-char snippet has to
        recur *exactly* and the varying supplier letter breaks it up.
        """
        chunks = []
        for i in range(12):
            chunks += ["Supplier ", f"{chr(65 + i)} ", "is ", "located ", "in ", "Australia", ". "]
        assert detect_repetition(chunks) is None

    def test_truly_repeating_text_trips_at_any_chunk_size(self):
        """Exact repetition is caught whether streamed in large or small pieces.

        Note the window is 40 *chunks*, so the amount of text actually examined
        varies by roughly 6x between these two cases (~1800 vs ~300 chars). The
        guard's reach therefore depends on how the provider segments the stream.
        """
        sentence = "Sentence with genuinely varied content here. "
        large_chunks = [sentence] * 80
        small_chunks = [w + " " for _ in range(80) for w in sentence.split()]

        assert len("".join(large_chunks[-40:])) > 1500
        assert len("".join(small_chunks[-40:])) < 400
        assert detect_repetition(large_chunks) is not None
        assert detect_repetition(small_chunks) is not None

    def test_returns_the_repeated_snippet(self):
        result = detect_repetition(["abcdefghij"] * 60)
        assert result is not None
        assert len(result) == 30


# ---------------------------------------------------------------------------
# Chunk parsing — Gemini thinking blocks must never reach the user
# ---------------------------------------------------------------------------

class TestExtractChunkTexts:
    def test_plain_string(self):
        assert extract_chunk_texts("hello") == ["hello"]

    def test_thinking_blocks_are_dropped(self):
        content = [
            {"type": "thinking", "thinking": "internal reasoning"},
            {"type": "text", "text": "the answer"},
        ]
        assert extract_chunk_texts(content) == ["the answer"]

    def test_empty_text_parts_are_skipped(self):
        content = [{"type": "text", "text": ""}, {"type": "text", "text": "kept"}]
        assert extract_chunk_texts(content) == ["kept"]

    def test_bare_strings_in_list(self):
        assert extract_chunk_texts(["a", "b"]) == ["a", "b"]

    def test_unknown_part_types_are_ignored(self):
        content = [{"type": "tool_use", "id": "x"}, {"type": "text", "text": "kept"}]
        assert extract_chunk_texts(content) == ["kept"]

    def test_unexpected_type_returns_empty(self):
        assert extract_chunk_texts(None) == []
        assert extract_chunk_texts(42) == []


class TestExtractAiText:
    def test_string_content_is_stripped(self):
        assert extract_ai_text(AIMessage(content="  hi  ")) == "hi"

    def test_list_content_is_joined(self):
        msg = AIMessage(content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
        assert extract_ai_text(msg) == "ab"

    def test_non_text_parts_ignored(self):
        msg = AIMessage(content=[{"type": "tool_use", "id": "x"}, {"type": "text", "text": "a"}])
        assert extract_ai_text(msg) == "a"

    def test_empty_content(self):
        assert extract_ai_text(AIMessage(content="")) == ""


# ---------------------------------------------------------------------------
# 5.6 Resume backfill — checkpoint is the source of truth, steps can lag
# ---------------------------------------------------------------------------

def _step(output="a reply", step_type="assistant_message"):
    return {"type": step_type, "output": output}


class TestPlanResumeBackfill:
    def test_ui_in_sync_backfills_nothing(self):
        ckpt = [AIMessage(content="one"), AIMessage(content="two")]
        steps = [_step("one"), _step("two")]
        assert plan_resume_backfill(ckpt, steps) == []

    def test_gap_of_two_returns_the_last_two(self):
        ckpt = [AIMessage(content="one"), AIMessage(content="two"), AIMessage(content="three")]
        steps = [_step("one")]
        missing = plan_resume_backfill(ckpt, steps)
        assert [m.content for m in missing] == ["two", "three"]

    def test_ui_ahead_of_checkpoint_backfills_nothing(self):
        """Negative gap must short-circuit, not fall through to the slice.

        Sized so that a `gap == 0` guard would slip through and return
        ai_messages[1:] — two messages that are already rendered. A smaller
        example passes either way, because the slice happens to come out empty.
        """
        ckpt = [AIMessage(content="one"), AIMessage(content="two"), AIMessage(content="three")]
        steps = [_step("one"), _step("two"), _step("three"), _step("four")]
        assert plan_resume_backfill(ckpt, steps) == []

    def test_empty_checkpoint(self):
        assert plan_resume_backfill([], [_step("one")]) == []

    def test_empty_ai_messages_are_not_counted(self):
        ckpt = [AIMessage(content=""), AIMessage(content="real")]
        assert [m.content for m in plan_resume_backfill(ckpt, [])] == ["real"]

    def test_human_messages_are_ignored(self):
        ckpt = [HumanMessage(content="question"), AIMessage(content="answer")]
        assert [m.content for m in plan_resume_backfill(ckpt, [])] == ["answer"]

    def test_non_assistant_steps_do_not_count_as_rendered(self):
        ckpt = [AIMessage(content="answer")]
        steps = [_step("question", step_type="user_message")]
        assert len(plan_resume_backfill(ckpt, steps)) == 1

    def test_blank_steps_do_not_count_as_rendered(self):
        ckpt = [AIMessage(content="answer")]
        assert len(plan_resume_backfill(ckpt, [_step("   ")])) == 1

    def test_none_output_raises(self):
        """Characterisation, not endorsement.

        A step with an explicit None output makes this raise. The caller treats
        reconciliation as best-effort and swallows it, so the effect is that
        backfill is silently skipped for that thread.
        """
        with pytest.raises(AttributeError):
            plan_resume_backfill([AIMessage(content="a")], [_step(None)])


# ---------------------------------------------------------------------------
# Differential: extracted functions vs verbatim copies of the original inline code
# ---------------------------------------------------------------------------

def _legacy_detect_repetition(buf):
    """Verbatim copy of the original guard from app.py, pre-extraction."""
    if len(buf) > 50:
        _buf_tail = "".join(buf[-40:])
        if len(_buf_tail) > 60:
            _snippet = _buf_tail[-30:]
            _test_window = _buf_tail[:-30]
            if _snippet and _test_window.count(_snippet) >= 4:
                return _snippet
    return None


def _legacy_extract_chunk_texts(content):
    """Verbatim copy of the original chunk parsing from app.py, pre-extraction."""
    out = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "thinking":
                continue
            if isinstance(part, dict) and part.get("type") == "text":
                chunk_text = part.get("text", "")
                if chunk_text:
                    out.append(chunk_text)
            elif isinstance(part, str):
                out.append(part)
    elif isinstance(content, str):
        out.append(content)
    return out


class TestExtractionIsNoOp:
    """Phase 0 requires the extraction to be provably behaviour-preserving."""

    @pytest.mark.parametrize("buffer", [
        [],
        ["a"] * 50,
        ["a"] * 51,
        ["abcdefghij"] * 60,
        ["x" * 100] + ["repeat me now"] * 3,
        [f"varied content {i} " for i in range(80)],
        ["short"] * 55,
        ["0123456789" * 4] * 51,
    ])
    def test_repetition_matches_legacy(self, buffer):
        assert detect_repetition(buffer) == _legacy_detect_repetition(buffer)

    @pytest.mark.parametrize("content", [
        "plain",
        "",
        [],
        [{"type": "text", "text": "a"}],
        [{"type": "thinking", "thinking": "x"}, {"type": "text", "text": "a"}],
        [{"type": "text", "text": ""}],
        ["bare", {"type": "text", "text": "b"}],
        [{"type": "tool_use", "id": "1"}],
    ])
    def test_chunk_texts_match_legacy(self, content):
        assert extract_chunk_texts(content) == _legacy_extract_chunk_texts(content)
