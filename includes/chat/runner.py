"""One agent turn, with no Chainlit and no synthetic messages.

``run_turn()`` is the single choke point into the graph. ``app.py``'s
``on_message`` is a thin adapter over it, action callbacks call it directly,
and (from Phase 2) so do the HTTP endpoints. Because every path goes through
here, the per-``thread_id`` lock below protects them all against each other.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Literal

from langchain_core.messages import HumanMessage

from config import config
from includes.chat.context import ChatContext
from includes.chat.document_processing import create_multimodal_content
from includes.chat.streaming_logic import (
    INTERRUPTED_TOOL_RESULT,
    REPETITION_ABORT_MESSAGE,
    detect_repetition,
    extract_chunk_texts,
    plan_checkpoint_repair,
)

logger = logging.getLogger(__name__)

_AGENT_NODE_NAMES = ("GeneralAgent", "ProcurementAgent", "SysAdminAgent", "ResearchAgent")


class RunInProgress(Exception):
    """A run is already active on this thread."""

    def __init__(self, thread_id: str) -> None:
        super().__init__(f"A run is already in progress on thread {thread_id}")
        self.thread_id = thread_id


# ---------------------------------------------------------------------------
# Per-thread run lock
# ---------------------------------------------------------------------------
# Two concurrent runs on one thread_id corrupt the checkpoint — which is what
# the dangling-tool_call repair below exists to clean up. Single-process only;
# scaling past one replica means moving this to a Postgres advisory lock.

_run_locks: dict[str, asyncio.Lock] = {}


def _has_waiters(lock: asyncio.Lock) -> bool:
    waiters = getattr(lock, "_waiters", None)
    return bool(waiters)


class _RunLock:
    """Acquire the thread's lock per the busy policy, releasing on exit."""

    def __init__(self, thread_id: str, on_busy: str, busy_timeout: float) -> None:
        self._thread_id = thread_id
        self._on_busy = on_busy
        self._busy_timeout = busy_timeout
        self._lock: asyncio.Lock | None = None

    async def __aenter__(self) -> "_RunLock":
        lock = _run_locks.setdefault(self._thread_id, asyncio.Lock())
        if lock.locked() and self._on_busy == "reject":
            raise RunInProgress(self._thread_id)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=self._busy_timeout)
        except (asyncio.TimeoutError, TimeoutError):
            raise RunInProgress(self._thread_id) from None
        self._lock = lock
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._lock is None:
            return
        self._lock.release()
        if not self._lock.locked() and not _has_waiters(self._lock):
            _run_locks.pop(self._thread_id, None)
        self._lock = None


# ---------------------------------------------------------------------------
# The turn
# ---------------------------------------------------------------------------

async def run_turn(
    text: str,
    ctx: ChatContext,
    *,
    graph: Any,
    files: list[dict] | None = None,
    file_metadata: list[dict] | None = None,
    intent_context: str = "",
    dashboard_context: str = "",
    on_busy: Literal["reject", "wait"] = "reject",
    busy_timeout: float = 120.0,
) -> None:
    """Run one agent turn and stream the reply into ``ctx``."""
    async with _RunLock(ctx.thread_id, on_busy, busy_timeout):
        await _run_turn_locked(
            text,
            ctx,
            graph=graph,
            files=files,
            file_metadata=file_metadata,
            intent_context=intent_context,
            dashboard_context=dashboard_context,
        )


async def _run_turn_locked(
    text: str,
    ctx: ChatContext,
    *,
    graph: Any,
    files: list[dict] | None,
    file_metadata: list[dict] | None,
    intent_context: str,
    dashboard_context: str,
) -> None:
    graph_config = {
        "configurable": {"thread_id": ctx.thread_id},
        "recursion_limit": config.GRAPH_RECURSION_LIMIT,
    }

    message_content = create_multimodal_content(text, files or [])

    # Prepend the dashboard context so it travels with the user turn and is
    # visible to the supervisor and every agent.
    if dashboard_context:
        logger.info(f"Dashboard context for {ctx.user_email}: {dashboard_context}")
        if isinstance(message_content, list):
            message_content = [{"type": "text", "text": dashboard_context + "\n\n"}] + message_content
        else:
            message_content = dashboard_context + "\n\n" + message_content

    inputs: dict[str, Any] = {
        "messages": [HumanMessage(content=message_content)],
        "user_id": ctx.user_email,
        # Always present, so a stale intent cannot survive in the checkpoint.
        "intent_context": intent_context or "",
    }
    if file_metadata:
        inputs["file_attachments"] = file_metadata

    await ctx.notify_dashboard("agent_working", {"label": "Agent working..."})

    msg = await ctx.say("")
    ctx.set("active_msg", msg)

    request_start = time.monotonic()
    active_agent = "GeneralAgent"
    supervisor_done_at = None

    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_all_tokens = 0

    active_step = None
    last_tool_name = None
    tool_call_count = 0
    tool_names_used: list[str] = []

    # Fallback for model calls that never emit stream events.
    last_ai_text = ""

    # Text emitted before a tool call is reasoning, not the answer — buffer it
    # and only flush once we know no tool call follows.
    _stream_buffer: list[str] = []

    await _repair_checkpoint(graph, graph_config, ctx.thread_id)

    last_event_time = request_start
    try:
        async for event in graph.astream_events(inputs, config=graph_config, version="v2"):
            if ctx.cancelled:
                logger.info(f"[stop-agent] Cooperative stop in astream_events for thread {ctx.thread_id[:8]}")
                await msg.stream("\n\n⏹ *Stopped by user.*")
                if active_step:
                    await active_step.remove()
                    active_step = None
                break

            kind = event["event"]
            name = event.get("name", "")
            tags = event.get("tags", [])

            if kind in (
                "on_chain_start", "on_chain_end", "on_tool_start",
                "on_tool_end", "on_chat_model_start", "on_chat_model_end",
            ):
                now = time.monotonic()
                gap = now - last_event_time
                if gap > 0.5:  # Only log gaps > 500ms to reduce noise
                    logger.info(f"[TIMING] {kind} '{name}' at T+{now - request_start:.1f}s (gap: {gap:.1f}s)")
                last_event_time = now

            if kind == "on_tool_start":
                tool_input = event.get("data", {}).get("input", "")
                logger.info(f"[TOOL] calling '{name}' with: {str(tool_input)[:200]}")

            if kind == "on_chain_start" and name in _AGENT_NODE_NAMES:
                # Flush the previous agent's buffer before starting a new one
                if _stream_buffer:
                    prev_text = "".join(_stream_buffer)
                    if prev_text.strip():
                        await msg.stream(prev_text)
                    _stream_buffer.clear()
                active_agent = name
                if supervisor_done_at is None:
                    supervisor_done_at = time.monotonic()
                    logger.info(f"Supervisor routing took {supervisor_done_at - request_start:.1f}s → {name}")

            if "supervisor_routing" in tags:
                continue

            if kind == "on_chat_model_stream":
                # Tool sequence is over — clean up the status indicator
                if active_step:
                    await active_step.remove()
                    active_step = None
                    last_tool_name = None
                content = event["data"]["chunk"].content
                if content:
                    _stream_buffer.extend(extract_chunk_texts(content))

                    _snippet = detect_repetition(_stream_buffer)
                    if _snippet:
                        logger.warning(
                            f"[repetition-guard] Detected degenerate repetition in stream buffer "
                            f"(repeated: {repr(_snippet[:40])}). Aborting stream."
                        )
                        _stream_buffer.clear()
                        _stream_buffer.append(REPETITION_ABORT_MESSAGE)
                        break

            elif kind == "on_tool_start":
                # Buffered text was reasoning, not the answer — drop it
                _stream_buffer.clear()
                friendly = name.replace("_", " ").title()
                if friendly not in tool_names_used:
                    tool_names_used.append(friendly)
                if name == last_tool_name and active_step:
                    tool_call_count += 1
                    active_step.content = f"⏳ Using {friendly} (x{tool_call_count})…"
                    await active_step.update()
                else:
                    if active_step:
                        await active_step.remove()
                    last_tool_name = name
                    tool_call_count = 1
                    active_step = await ctx.say(
                        f"⏳ Using {friendly}…", author="EagleAgent", transient=True
                    )

            elif kind == "on_tool_end":
                # Keep the status visible until the model starts streaming
                pass

            elif kind == "on_chat_model_end":
                output = event.get("data", {}).get("output")

                _ai_text = _extract_output_text(output)
                if _ai_text:
                    last_ai_text = _ai_text

                usage = _extract_usage(output)
                if usage:
                    total_prompt_tokens += usage.get("input_tokens", usage.get("prompt_tokens", 0))
                    total_completion_tokens += usage.get("output_tokens", usage.get("completion_tokens", 0))
                    total_all_tokens += usage.get("total_tokens", 0)
                    ctx.set("total_tokens_used", ctx.get("total_tokens_used", 0) + usage.get("total_tokens", 0))
    except asyncio.CancelledError:
        logger.info(f"[stop-agent] Graph stream cancelled for thread {ctx.thread_id[:8]}")
        await msg.stream("\n\n⏹ *Stopped by user.*")
        if active_step:
            await active_step.remove()
            active_step = None
    except Exception as e:
        logger.error(f"Graph execution error: {e}", exc_info=True)
        error_text = str(e)
        if any(code in error_text for code in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED")):
            await msg.stream("\n\nSorry, the AI model is temporarily overloaded. Please try again in a moment.")
        else:
            await msg.stream("\n\nSorry, an unexpected error occurred. Please try again.")

    # Flush the tail of the buffer — no tool call followed, so it is the answer
    if _stream_buffer:
        final_text = "".join(_stream_buffer).rstrip()
        if final_text:
            await msg.stream(final_text)
        _stream_buffer.clear()

    # Fallback: the model responded but never emitted stream events
    if not msg.content.strip() and last_ai_text.strip():
        logger.warning("No streaming output captured — using fallback response text")
        await msg.stream(last_ai_text)

    # Second fallback: read the last message back out of the checkpoint
    if not msg.content.strip():
        try:
            final_state = await graph.aget_state(graph_config)
            if final_state and final_state.values.get("messages"):
                last_msg = final_state.values["messages"][-1]
                _fallback2 = _extract_output_text(last_msg)
                if _fallback2.strip():
                    logger.warning(
                        f"No streaming output — using fallback from graph state "
                        f"(last msg type={type(last_msg).__name__})"
                    )
                    await msg.stream(_fallback2)
        except Exception as fb_err:
            logger.debug(f"State fallback failed: {fb_err}")

    ctx.set("active_msg", None)

    # Strip trailing whitespace so the footer sits cleanly against content
    if msg.content:
        msg.content = msg.content.rstrip()

    if active_step:
        await active_step.remove()
        active_step = None

    if total_all_tokens > 0 or msg.content.strip() or last_ai_text.strip():
        await msg.stream(_build_footer(
            active_agent=active_agent,
            request_start=request_start,
            supervisor_done_at=supervisor_done_at,
            tool_names_used=tool_names_used,
            total_all_tokens=total_all_tokens,
            total_prompt_tokens=total_prompt_tokens,
            total_completion_tokens=total_completion_tokens,
        ))

    await msg.save()

    # Single-use intent — the next message must not inherit the old button's
    ctx.set("intent_context", None)

    try:
        await ctx.notify_dashboard("agent_done")
    except Exception:
        pass  # Best-effort; a dead session must not crash the turn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _repair_checkpoint(graph: Any, graph_config: dict, thread_id: str) -> None:
    """Fix dangling tool_calls left by an interrupted run.

    LangGraph refuses to proceed unless every AIMessage tool_call has a
    matching ToolMessage. A few get synthetic error results; many mean the
    history is badly corrupted and the AIMessages are removed outright, since
    dozens of synthetic errors just push the model into empty-response loops.
    """
    try:
        checkpoint_state = await graph.aget_state(graph_config)
        if not (checkpoint_state and checkpoint_state.values.get("messages")):
            return

        from langchain_core.messages import RemoveMessage, ToolMessage as LCToolMessage

        plan = plan_checkpoint_repair(checkpoint_state.values["messages"])

        if plan.strategy == "inject":
            logger.warning(
                f"[checkpoint-repair] Found {len(plan.dangling)} dangling tool_call(s) "
                f"in thread {thread_id[:8]}... — injecting synthetic error ToolMessages"
            )
            repair_messages = [
                LCToolMessage(content=INTERRUPTED_TOOL_RESULT, tool_call_id=tc["id"])
                for tc in plan.dangling
            ]
            await graph.aupdate_state(graph_config, {"messages": repair_messages})
            logger.info(f"[checkpoint-repair] Injected {len(repair_messages)} repair message(s)")
        elif plan.strategy == "remove":
            logger.warning(
                f"[checkpoint-repair] Found {len(plan.dangling)} dangling tool_call(s) across "
                f"{len(plan.corrupt_message_ids)} AIMessage(s) in thread {thread_id[:8]}... — "
                f"removing corrupted messages (too many to patch)"
            )
            remove_ops = [RemoveMessage(id=mid) for mid in plan.corrupt_message_ids]
            if remove_ops:
                await graph.aupdate_state(graph_config, {"messages": remove_ops})
                logger.info(f"[checkpoint-repair] Removed {len(remove_ops)} corrupted AIMessage(s)")
    except Exception as e:
        logger.warning(f"[checkpoint-repair] Failed to check/repair checkpoint: {e}")


def _parts_to_text(parts: Any) -> str:
    if isinstance(parts, str):
        return parts
    if isinstance(parts, list):
        return "".join(
            p.get("text", "") if isinstance(p, dict) and p.get("type") == "text"
            else p if isinstance(p, str) else ""
            for p in parts
        )
    return ""


def _extract_output_text(output: Any) -> str:
    """Pull plain text out of an AIMessage, ChatResult or LLMResult."""
    text = ""
    if hasattr(output, "content"):
        text = _parts_to_text(output.content)

    if not text and hasattr(output, "generations"):
        for gen_list in output.generations:
            for gen in (gen_list if isinstance(gen_list, list) else [gen_list]):
                gen_msg = getattr(gen, "message", None)
                if gen_msg is not None and hasattr(gen_msg, "content"):
                    candidate = _parts_to_text(gen_msg.content)
                    if candidate.strip() or isinstance(gen_msg.content, list):
                        text = candidate
                if text:
                    break
            if text:
                break
    return text


def _extract_usage(output: Any) -> dict | None:
    usage = None
    if hasattr(output, "usage_metadata") and output.usage_metadata:
        usage = output.usage_metadata
    elif isinstance(output, dict):
        if "usage_metadata" in output:
            usage = output["usage_metadata"]
        elif output.get("generations") and output["generations"][0]:
            gen = output["generations"][0][0]
            if isinstance(gen, dict) and "message" in gen:
                msg_obj = gen["message"]
                if hasattr(msg_obj, "usage_metadata") and msg_obj.usage_metadata:
                    usage = msg_obj.usage_metadata
    if not usage and hasattr(output, "response_metadata") and output.response_metadata:
        usage = output.response_metadata.get("usage_metadata") or output.response_metadata.get("token_usage")
    return usage


def _build_footer(
    *,
    active_agent: str,
    request_start: float,
    supervisor_done_at: float | None,
    tool_names_used: list[str],
    total_all_tokens: int,
    total_prompt_tokens: int,
    total_completion_tokens: int,
) -> str:
    total_elapsed = time.monotonic() - request_start
    routing_part = ""
    if supervisor_done_at is not None:
        routing_part = f" | Routing: {supervisor_done_at - request_start:.1f}s"
    tools_part = ""
    if tool_names_used:
        tools_part = " | Used " + ", ".join(tool_names_used)

    style = "margin-top:20px; font-size:0.8em; color:#a1a1aa; font-style:italic;"
    if total_all_tokens > 0:
        body = (
            f"Agent: {active_agent} | Tokens: {total_all_tokens:,} "
            f"(Context: {total_prompt_tokens:,}, Generated: {total_completion_tokens:,})"
            f"{routing_part} | Total: {total_elapsed:.1f}s{tools_part}"
        )
    else:
        body = f"Agent: {active_agent}{routing_part} | Total: {total_elapsed:.1f}s{tools_part}"
    return f"\n\n<div style='{style}'>{body}</div>\n\n"
