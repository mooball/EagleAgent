"""
Agent Bridge: bidirectional communication between the FastAPI dashboard and
the Chainlit agent running inside the iframe.

Dashboard → Agent:
    The dashboard calls POST /api/agent-bridge with an action name and payload.
    The server reads the Chainlit session cookie, initialises the Chainlit
    context for that session, and dispatches the registered @cl.action_callback.

Agent → Dashboard:
    Server-side code calls ``notify_dashboard()`` which uses Chainlit's
    built-in ``cl.send_window_message()`` to push a socket event to the
    iframe.  Chainlit's frontend automatically forwards it via
    ``window.parent.postMessage()``, where base.html handles it.

    Supported commands:
        - ``dashboard_refresh``  – re-fetch the current partial view
        - ``agent_navigate``     – navigate to a specific dashboard route

See docs/AGENT_BRIDGE.md for the full architecture.
"""

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Per-session lock: serializes concurrent action dispatches on the same
# Chainlit session so that thread_id pinning cannot race.
_session_locks: Dict[str, asyncio.Lock] = {}

# ---------------------------------------------------------------------------
# Cooperative cancellation via per-session Events
# ---------------------------------------------------------------------------
# Each session gets an asyncio.Event that is SET when a stop is requested.
# Long-running action callbacks check this flag at natural break points
# and exit early if set. A dedicated /api/stop-agent endpoint sets the flag
# (bypassing the session lock) and optionally cancels tracked asyncio Tasks.

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
    """Clear the cancel flag so the session can accept new work."""
    ev = _cancel_events.get(session_id)
    if ev:
        ev.clear()


# ---------------------------------------------------------------------------
# Agent → Dashboard helpers
# ---------------------------------------------------------------------------

# Concurrent workers on one session share a single "agent working" badge, so
# the badge is reference-counted: the first worker turns it on, the last turns
# it off. Without this, whichever finishes first clears it for everyone.
_working_depth: Dict[str, int] = {}


def _badge_should_emit(command: str) -> bool:
    """Track agent_working/agent_done depth; emit only on the 0↔1 transitions."""
    if command not in ("agent_working", "agent_done"):
        return True
    try:
        import chainlit as cl
        key = cl.context.session.id
    except Exception:
        return True

    if command == "agent_working":
        depth = _working_depth.get(key, 0)
        _working_depth[key] = depth + 1
        return depth == 0

    depth = _working_depth.get(key, 0) - 1
    if depth <= 0:
        _working_depth.pop(key, None)
        return True
    _working_depth[key] = depth
    return False


async def notify_dashboard(command: str, payload: dict | None = None) -> None:
    """Send a command to the dashboard via the Chainlit iframe.

    Uses Chainlit's built-in ``send_window_message`` which emits a
    ``window_message`` socket event.  The Chainlit frontend forwards it
    to ``window.parent.postMessage()``, where base.html picks it up.

    Args:
        command: The message type, e.g. ``"dashboard_refresh"`` or
                 ``"agent_navigate"``.
        payload: Optional dict merged into the message.

    Example::

        await notify_dashboard("dashboard_refresh")
        await notify_dashboard("agent_navigate", {"url": "/rfqs/RFQ-123"})
    """
    import chainlit as cl

    if not _badge_should_emit(command):
        return

    data: dict = {"type": command}
    if payload:
        data["payload"] = payload
    try:
        await cl.send_window_message(data)
    except Exception:
        logger.debug("notify_dashboard: not in Chainlit context, skipping")


async def dispatch_action(
    session_id: str,
    action_name: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Dispatch a Chainlit action callback within the given session context.

    Args:
        session_id: The Chainlit websocket session ID (from the cookie).
        action_name: Name of the registered @cl.action_callback.
        payload: Dict of parameters to pass to the action.

    Returns:
        {"success": True} on success, or {"error": "..."} on failure.
    """
    from chainlit.action import Action
    from chainlit.config import config
    from chainlit.context import init_ws_context
    from chainlit.session import WebsocketSession

    # Acquire a per-session lock so that concurrent bridge requests on the
    # same session are serialized.  This prevents two actions from racing
    # to set session.thread_id and corrupting each other's context.
    if session_id not in _session_locks:
        _session_locks[session_id] = asyncio.Lock()
    lock = _session_locks[session_id]

    async with lock:
        session = WebsocketSession.get_by_id(session_id)
        if not session:
            logger.warning(f"[agent_bridge] Session not found: {session_id}")
            return {"error": "Chainlit session not found. Please reload the page."}

        # Set the Chainlit context so cl.user_session, cl.Message etc. work
        init_ws_context(session)

        # If the payload includes a _thread_id (injected by the dashboard),
        # pin the session to that thread BEFORE the callback runs.  This
        # prevents cross-thread contamination when the user has navigated the
        # chat iframe to a different RFQ thread after clicking the button.
        target_thread_id = payload.get("_thread_id")
        if target_thread_id:
            logger.info(f"[agent_bridge] Pinning session to thread {target_thread_id} (from payload)")
            session.thread_id = target_thread_id
            import chainlit as cl
            cl.user_session.set("thread_id", target_thread_id)

        callback = config.code.action_callbacks.get(action_name)
        if callback:
            # Native @cl.action_callback
            try:
                action = Action(name=action_name, payload=payload)
                await callback(action)
                return {"success": True}
            except Exception as e:
                logger.exception(f"[agent_bridge] Action {action_name} failed")
                return {"error": str(e)}

        # Fall back to custom action registry (includes/chat/actions.py)
        from includes.chat.actions import dispatch_action as dispatch_custom_action, get_action
        if get_action(action_name):
            try:
                from includes.chat.context_chainlit import ChainlitChatContext
                await dispatch_custom_action(
                    action_name, ChainlitChatContext.from_session(), **payload
                )
                return {"success": True}
            except Exception as e:
                logger.exception(f"[agent_bridge] Action {action_name} failed")
                return {"error": str(e)}

        logger.warning(f"[agent_bridge] No callback for action: {action_name}")
        return {"error": f"Unknown action: {action_name}"}


async def handle_bridge_request(request: Request) -> Response:
    """FastAPI handler for POST /api/agent-bridge.

    Expected JSON body::

        {
            "action": {
                "name": "rfq_find_suppliers",
                "payload": { ... }
            }
        }

    The Chainlit session ID is read from the ``X-Chainlit-Session-id``
    cookie which the Chainlit frontend sets automatically.
    """
    # Check dashboard auth
    from main import get_current_user

    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    # Read Chainlit session ID from cookie
    session_id = request.cookies.get("X-Chainlit-Session-id")
    if not session_id:
        return JSONResponse(
            {"error": "No Chainlit session. Please open the chat panel first."},
            status_code=400,
        )

    # Parse the action
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    action_data = body.get("action", {})
    action_name = action_data.get("name")
    if not action_name:
        return JSONResponse({"error": "Missing action name"}, status_code=400)

    payload = action_data.get("payload", {})
    logger.info(f"[agent_bridge] {user['email']} → {action_name}")

    result = await dispatch_action(session_id, action_name, payload)

    if "error" in result:
        return JSONResponse(result, status_code=422)
    return JSONResponse(result)
