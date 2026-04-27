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

import logging
from typing import Any, Dict, Optional

from fastapi import Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent → Dashboard helpers
# ---------------------------------------------------------------------------

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

    session = WebsocketSession.get_by_id(session_id)
    if not session:
        logger.warning(f"[agent_bridge] Session not found: {session_id}")
        return {"error": "Chainlit session not found. Please reload the page."}

    # Set the Chainlit context so cl.user_session, cl.Message etc. work
    init_ws_context(session)

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
            await dispatch_custom_action(action_name, **payload)
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
