"""
Dashboard context store.

Holds the current dashboard view context per user so the agent can
inject it into its system prompt.  Updated from the Chainlit iframe
via ``POST /api/dashboard-context`` and read by ``app.py`` at message
time.

The store is in-process memory — perfectly fine for a single-worker
deployment.  If we later move to multiple workers, switch to Redis or
the database.

Each entry has a 30-minute TTL (configurable via ``DASHBOARD_CONTEXT_TTL``
env var).  Expired entries are lazily evicted on read and periodically
cleaned up on write.
"""

import os
import time
from typing import Any, Dict, Optional
import threading

_lock = threading.Lock()
_store: Dict[str, tuple[Dict[str, Any], float]] = {}  # email → (context, timestamp)

_CONTEXT_TTL = int(os.getenv("DASHBOARD_CONTEXT_TTL", "1800"))  # 30 minutes


def set_context(user_email: str, context: Dict[str, Any]) -> None:
    """Store context for a user and clean up expired entries."""
    with _lock:
        _store[user_email] = (context, time.time())
        # Opportunistic cleanup of expired entries
        _cleanup_expired_locked()


def get_context(user_email: str) -> Optional[Dict[str, Any]]:
    """Return the user's context, or None if missing or expired."""
    with _lock:
        entry = _store.get(user_email)
        if entry is None:
            return None
        ctx, ts = entry
        if time.time() - ts > _CONTEXT_TTL:
            del _store[user_email]
            return None
        return ctx


def _cleanup_expired_locked() -> None:
    """Remove all expired entries.  Must be called while holding _lock."""
    now = time.time()
    expired = [email for email, (_, ts) in _store.items() if now - ts > _CONTEXT_TTL]
    for email in expired:
        del _store[email]


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

    # RFQ-specific summary for rfq_detail view
    if ctx.get("view") == "rfq_detail":
        if ctx.get("customer"):
            parts.append(f"Customer: {ctx['customer']}")
        if ctx.get("status"):
            parts.append(f"Status: {ctx['status']}")
        if ctx.get("item_count") is not None:
            identified = ctx.get("identified_count", 0)
            parts.append(f"Items: {ctx['item_count']} ({identified} identified)")
        if ctx.get("assigned_to"):
            parts.append(f"Assigned to: {ctx['assigned_to']}")

    return " | ".join(parts)
