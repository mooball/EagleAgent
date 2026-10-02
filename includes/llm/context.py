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


# ---------------------------------------------------------------------------
# Patient mode — a longer LLM budget for user-triggered retries
# ---------------------------------------------------------------------------
# A manual retry means a human has chosen to wait, so the usual background
# budget can be extended (see ``Config.LLM_PATIENT_MAX_ATTEMPT_SECONDS``). This
# is deliberately separate from the workload: a retry is still background work
# for model/quota purposes, it just gets more time.

_patient: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "llm_patient", default=False
)


def is_patient() -> bool:
    """True when the current work is a user-triggered retry with a longer budget."""
    return _patient.get()


@contextmanager
def patient():
    """Run the enclosed work with the patient LLM budget.

    ``asyncio.to_thread`` copies the context, so setting this around the
    ``to_thread`` call is enough for the sync pipeline that runs inside it.
    """
    token = _patient.set(True)
    try:
        yield
    finally:
        _patient.reset(token)


def enter_patient_mode() -> None:
    """Set patient mode for the current context, with no scope to exit.

    For a raw ``threading.Thread``, where a ``with patient():`` block around
    ``Thread.start()`` would not reach the new thread. Intended for a
    short-lived worker thread that ends with the work; do not call it on a
    pooled thread.
    """
    _patient.set(True)
