"""
Agent Bridge: dashboard → agent action dispatch, plus stop signalling.

Dashboard → Agent:
    The dashboard calls POST /api/agent-bridge with an action name and payload.
    The handler resolves the RFQ's bound thread and dispatches into the SSE
    chat via ``chat_ui.dispatch_action_to_thread`` — the same path a chat
    action button takes. There is one dispatch mechanism; Chainlit is gone.

Agent → Dashboard:
    Handled entirely by the SSE transport: ``SseChatContext.notify_dashboard``
    queues a ``dashboard`` event that the embed forwards to the dashboard shell
    as a DOM CustomEvent. No server-side bridge is involved.

See docs/AGENT_BRIDGE.md for the full architecture.
"""

import asyncio
import logging
from typing import Dict

from fastapi import Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cooperative cancellation via per-session Events
# ---------------------------------------------------------------------------
# Each session gets an asyncio.Event that is SET when a stop is requested.
# Long-running action callbacks check this flag at natural break points
# and exit early if set. A dedicated /api/stop-agent endpoint sets the flag
# and optionally cancels tracked asyncio Tasks.

_cancel_events: Dict[str, asyncio.Event] = {}
_running_tasks: Dict[str, set[asyncio.Task]] = {}


def _get_cancel_event(session_id: str) -> asyncio.Event:
    """Get or create the cancellation event for a session."""
    if session_id not in _cancel_events:
        _cancel_events[session_id] = asyncio.Event()
    return _cancel_events[session_id]


def is_stop_requested(session_id: str) -> bool:
    """Check if stop has been requested for this session. Non-blocking."""
    ev = _cancel_events.get(session_id)
    return ev.is_set() if ev else False


def register_task(task: asyncio.Task, session_id: str) -> None:
    """Register a running agent task so it can be cancelled via stop."""
    if session_id not in _running_tasks:
        _running_tasks[session_id] = set()
    _running_tasks[session_id].add(task)


def unregister_task(task: asyncio.Task, session_id: str) -> None:
    """Remove a completed/cancelled task from the registry."""
    tasks = _running_tasks.get(session_id)
    if tasks:
        tasks.discard(task)


async def request_stop(session_id: str) -> int:
    """Request cancellation of all agent work for a session.

    Sets the cooperative cancel flag AND cancels tracked asyncio Tasks.
    Returns the number of tasks cancelled.
    """
    # Set cooperative flag — callbacks will check this at break points
    ev = _get_cancel_event(session_id)
    ev.set()

    # Also cancel tracked asyncio tasks (effective for astream_events loops)
    cancelled = 0
    tasks = list(_running_tasks.get(session_id, set()))
    for t in tasks:
        if not t.done():
            t.cancel()
            cancelled += 1
    _running_tasks.pop(session_id, None)

    if cancelled:
        logger.info(f"[agent-bridge] Cancelled {cancelled} task(s) for session {session_id[:8]}")
    else:
        logger.info(f"[agent-bridge] Stop requested for session {session_id[:8]} (cooperative flag set)")
    return cancelled


def clear_stop(session_id: str) -> None:
    """Clear the cancel flag so the session can accept new work.

    The event is dropped rather than just unset: a cleared entry left in the
    dict would accumulate one entry per thread/session that was ever stopped,
    for the lifetime of the process. ``_get_cancel_event`` recreates it on
    demand, so removal is safe.
    """
    ev = _cancel_events.pop(session_id, None)
    if ev:
        ev.clear()


# ---------------------------------------------------------------------------
# Agent → Dashboard
# ---------------------------------------------------------------------------
# There is nothing to do here: ``SseChatContext.notify_dashboard`` queues the
# event on the thread's SSE stream and the embed forwards it to the dashboard
# shell. The previous Chainlit implementation (``send_window_message`` plus a
# reference-counted "agent working" badge keyed on the Chainlit session) has
# been removed along with Chainlit.


async def handle_bridge_request(request: Request) -> Response:
    """FastAPI handler for POST /api/agent-bridge.

    Expected JSON body::

        {
            "action": {"name": "rfq_find_suppliers", "payload": { ... }}
        }

    The action is dispatched into the SSE chat on the thread the RFQ is bound
    to — the same path a chat action button takes. The dashboard injects
    ``payload["_thread_id"]``; without it there is nowhere to route the action,
    so the caller is told to open the chat panel.
    """
    from main import get_current_user

    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    action_data = body.get("action", {}) or {}
    action_name = action_data.get("name")
    if not action_name:
        return JSONResponse({"error": "Missing action name"}, status_code=400)

    raw_payload = action_data.get("payload", {})
    payload = raw_payload if isinstance(raw_payload, dict) else {}

    rfq_id = payload.get("rfq_id")
    thread_id = payload.get("_thread_id")

    if not thread_id:
        # Self-heal: the dashboard had no bound thread to offer. That happens on
        # a brand-new RFQ, when a button is clicked before the client's bind has
        # finished, or when the bound thread was deleted. Resolve the RFQ's OWN
        # thread — creating and binding a fresh one if needed — so the action can
        # never run against whatever unrelated conversation happened to be open.
        if not rfq_id:
            return JSONResponse(
                {"error": "This action needs an RFQ and a chat thread."},
                status_code=400,
            )
        from includes.dashboard.routes.api import _lookup_rfq_thread_id

        thread_id = await asyncio.to_thread(
            _lookup_rfq_thread_id, str(rfq_id), user["email"],
        )
        if not thread_id:
            return JSONResponse(
                {"error": "Could not open a chat thread for this RFQ."},
                status_code=500,
            )
        logger.info(
            "[agent_bridge] no thread hint for %s — resolved/created %s",
            rfq_id, str(thread_id)[:8],
        )

    logger.info(
        "[agent_bridge] %s → %s (thread %s)",
        user["email"], action_name, str(thread_id)[:8],
    )

    from includes.dashboard.routes.chat_ui import dispatch_action_to_thread

    result = await dispatch_action_to_thread(
        user, str(thread_id), action_name, payload,
    )
    status_code = result.pop("status_code", 200)
    return JSONResponse(result, status_code=status_code)
