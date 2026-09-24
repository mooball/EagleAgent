"""Ambient LLM workload context.

Some pipelines serve both a background trigger and a user-facing one —
``supplier_quote_pipeline``, ``rfq_creation_pipeline`` and ``comms_summary`` are
all reached either way. The pipeline itself cannot tell which, so the *trigger*
declares the workload here and model/tier resolution reads it.

Why a contextvar rather than a parameter: the trigger is known at the top of the
background loop, but the LLM call sits several layers down
(loop -> pipeline -> helper -> llm_call_with_retry). Threading a flag through
every signature would touch every layer for one bit of information, and would
silently break the moment a new code path forgot to pass it.

``asyncio.to_thread`` copies the current context, so a value set in a loop
coroutine reaches the sync worker thread — which is exactly how main.py's
background loops run (see ``tests/test_llm_workload_context.py`` for the proof
that this propagation actually holds).
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager

INTERACTIVE = "interactive"
SYNC = "sync"

_workload: contextvars.ContextVar[str] = contextvars.ContextVar(
    "llm_workload", default=INTERACTIVE
)


def current_workload() -> str:
    """The workload in scope: ``INTERACTIVE`` (default) or ``SYNC``."""
    return _workload.get()


def is_sync() -> bool:
    return _workload.get() == SYNC


@contextmanager
def workload(name: str):
    """Mark the enclosed work as belonging to ``name``."""
    token = _workload.set(name)
    try:
        yield
    finally:
        _workload.reset(token)


def current_service_tier() -> str | None:
    """Vertex service tier to request for the workload in scope.

    Returns None when unconfigured, which omits ``service_tier`` entirely and
    gives Standard behaviour — so this is inert until the env vars are set.
    """
    from config.settings import Config

    if is_sync():
        return Config.SYNC_SERVICE_TIER or None
    return Config.INTERACTIVE_SERVICE_TIER or None
