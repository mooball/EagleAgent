"""
Dashboard context store.

Holds the current dashboard view context per user so the agent can
inject it into its system prompt.  Updated from the Chainlit iframe
via ``POST /api/dashboard-context`` and read by ``app.py`` at message
time.

The store is in-process memory — perfectly fine for a single-worker
deployment.  If we later move to multiple workers, switch to Redis or
the database.
"""

from typing import Any, Dict, Optional
import threading

_lock = threading.Lock()
_store: Dict[str, Dict[str, Any]] = {}


def set_context(user_email: str, context: Dict[str, Any]) -> None:
    with _lock:
        _store[user_email] = context


def get_context(user_email: str) -> Optional[Dict[str, Any]]:
    with _lock:
        return _store.get(user_email)


def format_context_for_prompt(user_email: str) -> str:
    """Return a prompt fragment describing the user's current dashboard view.

    Returns an empty string if no context is set.
    """
    ctx = get_context(user_email)
    if not ctx or not ctx.get("view"):
        return ""

    parts = [f"[Dashboard Context] The user is currently viewing: {ctx['view']}"]
    if ctx.get("entity"):
        parts.append(f"Entity type: {ctx['entity']}")
    if ctx.get("id"):
        parts.append(f"ID: {ctx['id']}")
    if ctx.get("params"):
        parts.append(f"Parameters: {ctx['params']}")
    if ctx.get("breadcrumb"):
        parts.append(f"Breadcrumb: {' > '.join(ctx['breadcrumb'])}")

    return " | ".join(parts)
