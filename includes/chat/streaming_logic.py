"""Pure decision logic extracted from the streaming loop in app.py.

These functions perform no I/O and hold no Chainlit or LangGraph state, so they
can be unit-tested directly. Behaviour is intentionally identical to the inline
code they replaced — thresholds and orderings must not be changed here without a
corresponding decision recorded in the migration plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

# Injected in place of a missing ToolMessage when repairing a light corruption.
INTERRUPTED_TOOL_RESULT = "[Error: previous operation was interrupted. Please retry if needed.]"

# Shown to the user when the model gets stuck emitting the same phrase.
REPETITION_ABORT_MESSAGE = (
    "\n\nSorry, I encountered an issue processing that request. Please try again."
)

# Repetition guard thresholds. A run must clear all four to be treated as
# degenerate, which keeps false positives (and truncated real answers) rare.
_REPETITION_MIN_CHUNKS = 50
_REPETITION_TAIL_CHUNKS = 40
_REPETITION_MIN_TAIL_CHARS = 60
_REPETITION_SNIPPET_CHARS = 30
_REPETITION_MIN_REPEATS = 4

# More than this many dangling tool calls means the history is badly corrupted
# and is cheaper to delete than to patch.
_MAX_DANGLING_TO_PATCH = 2


@dataclass(frozen=True)
class RepairPlan:
    """What to do about tool_calls left without a matching ToolMessage.

    `inject` adds a synthetic error result so the model can retry; `remove`
    deletes the offending AIMessages outright, because injecting many synthetic
    messages confuses the model into empty-response loops.
    """

    strategy: Literal["none", "inject", "remove"]
    dangling: list[dict] = field(default_factory=list)
    corrupt_message_ids: list[str] = field(default_factory=list)


def plan_checkpoint_repair(messages: Sequence[BaseMessage]) -> RepairPlan:
    """Decide how to repair a checkpoint containing unanswered tool calls."""
    answered = {
        m.tool_call_id for m in messages if isinstance(m, ToolMessage)
    }

    corrupted: list[AIMessage] = []
    dangling: list[dict] = []
    for m in messages:
        if isinstance(m, AIMessage) and m.tool_calls:
            unanswered = [tc for tc in m.tool_calls if tc["id"] not in answered]
            if unanswered:
                corrupted.append(m)
                dangling.extend(unanswered)

    if not dangling:
        return RepairPlan(strategy="none")

    if len(dangling) <= _MAX_DANGLING_TO_PATCH:
        return RepairPlan(strategy="inject", dangling=dangling)

    # RemoveMessage requires a valid id, so messages without one cannot be removed.
    return RepairPlan(
        strategy="remove",
        dangling=dangling,
        corrupt_message_ids=[m.id for m in corrupted if m.id],
    )


def detect_repetition(buffer: Sequence[str]) -> str | None:
    """Return the repeated snippet if the buffer looks degenerate, else None."""
    if len(buffer) <= _REPETITION_MIN_CHUNKS:
        return None

    tail = "".join(buffer[-_REPETITION_TAIL_CHUNKS:])
    if len(tail) <= _REPETITION_MIN_TAIL_CHARS:
        return None

    snippet = tail[-_REPETITION_SNIPPET_CHARS:]
    window = tail[:-_REPETITION_SNIPPET_CHARS]
    if snippet and window.count(snippet) >= _REPETITION_MIN_REPEATS:
        return snippet
    return None


def extract_ai_text(ai_msg: Any) -> str:
    """Return the plain text content of an AIMessage, or '' if none."""
    content = ai_msg.content
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        return "".join(parts).strip()
    return ""


def extract_chunk_texts(content: Any) -> list[str]:
    """Return the streamable text parts of a chat model chunk.

    Gemini 2.5+ interleaves `thinking` blocks with the answer; those are dropped
    rather than streamed to the user.
    """
    if isinstance(content, str):
        return [content]

    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "thinking":
                    continue
                if part.get("type") == "text":
                    chunk_text = part.get("text", "")
                    if chunk_text:
                        texts.append(chunk_text)
            elif isinstance(part, str):
                texts.append(part)
        return texts

    return []
