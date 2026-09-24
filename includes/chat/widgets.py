"""In-chat widgets — interactive cards a conversation can open.

An **action** (``includes/chat/actions.py``) is a button: a name, a label and a
payload that comes straight back to a handler. A **widget** is a *form*: the
user types into it, and the values come back as a submission.

That difference is the whole reason this module exists. It could not be folded
into the action registry because:

* the transport is different — a widget needs a state machine (pending →
  submitted/cancelled) and its own submit endpoint, not a one-shot click;
* the payload is user-authored — it must be validated, and it must survive a
  reload mid-typing, so state is persisted in the step's metadata;
* the markup cannot travel as message text — ``sanitizeHtml()`` in ``embed.html``
  deliberately strips ``form``/``input``/``button`` from anything that arrives
  through the markdown channel, so widget HTML is rendered server-side and
  fetched separately.

## The contract

A widget is a :class:`WidgetSpec`: a name, a template, a ``submit`` handler and
an optional ``context`` builder. The handler is **synchronous** — the caller
runs it in a worker thread — and returns a :class:`WidgetOutcome` describing the
next state. Handlers never touch HTTP or the DOM: they receive plain data and
return plain data, which is what makes them testable without a browser.

State lives in the *step* that renders the widget, under the ``widget`` key::

    {"id": "...", "name": "add_supplier", "status": "pending",
     "data": {...}, "rfq_id": "RFQ-2026-1234", "error": "",
     "duplicates": [], "duplicate_checked": False, "result": null}

Storing it on the step rather than in a table of its own means the state and the
thing being rendered cannot drift apart, and a transcript reload re-renders the
same widget in the same position with no extra bookkeeping.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

__all__ = [
    "ACTION_FIELD",
    "WIDGET_META_KEY",
    "WidgetOutcome",
    "WidgetSpec",
    "apply_outcome",
    "dispatch_widget",
    "form_to_data",
    "get_widget",
    "list_widgets",
    "new_widget_state",
    "open_widget_step",
    "register_widget",
    "rfq_id_for_thread",
    "widget_state_from_metadata",
]

logger = logging.getLogger(__name__)

#: Metadata key under which a step carries its widget state.
WIDGET_META_KEY = "widget"

#: Form field naming the button that submitted the form. Widget templates pair
#: it with a value (``submit``, ``cancel``), so the dispatcher learns what was
#: pressed from the submission itself instead of needing a JS handler per button.
ACTION_FIELD = "__widget_action"

WidgetStatus = Literal["pending", "submitted", "cancelled"]

#: Statuses that accept no further submissions. A widget in one of these states
#: is finished — re-submitting is a client bug (or a double click) and must not
#: create a second supplier.
_CLOSED_STATUSES = ("submitted", "cancelled")


@dataclass
class WidgetOutcome:
    """The result of a submission, in the shape the state machine stores."""

    status: WidgetStatus = "pending"
    data: dict[str, Any] = field(default_factory=dict)
    #: Submit-level failure (banner). Per-field problems belong in ``errors``.
    error: str = ""
    #: ``{field: message}`` — lets the form mark the offending input instead of
    #: making the user re-read a paragraph to find it.
    errors: dict[str, str] = field(default_factory=dict)
    duplicates: list[dict] = field(default_factory=list)
    duplicate_checked: bool = False
    result: Optional[dict[str, Any]] = None
    notice: str = ""
    #: Shell command to fire when this outcome lands, e.g.
    #: ``{"command": "dashboard_refresh", "payload": {"rfq_id": ...}}``.
    #:
    #: A widget runs on its own HTTP request, not inside an agent turn, so it has
    #: no SSE queue to send ``ctx.notify_dashboard`` through. Returning the command
    #: instead lets the client raise the identical DOM event the run path raises —
    #: the shell cannot tell the two apart, so a widget-driven change refreshes the
    #: page exactly like a tool-driven one.
    dashboard: Optional[dict[str, Any]] = None

    @property
    def closed(self) -> bool:
        return self.status in _CLOSED_STATUSES


@dataclass(frozen=True)
class WidgetSpec:
    """Everything the transport needs to know about one widget."""

    name: str
    label: str
    description: str
    template: str
    #: (data, state, user_email) -> WidgetOutcome. Synchronous; run in a thread.
    submit: Callable[[dict[str, Any], dict[str, Any], str], WidgetOutcome]
    #: (state) -> extra template context (field options, RFQ lines, ...).
    context: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None
    icon: str = "⚡"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_registry: dict[str, WidgetSpec] = {}

#: Widgets live beside the feature they belong to (``includes/dashboard/``), so
#: they cannot be imported here at module level without a cycle. They are
#: imported on first lookup instead — registration stays local to the feature
#: and the cost is paid once.
_BUILTIN_MODULES = ("includes.dashboard.supplier_widget",)
_builtins_loaded = False


def _ensure_builtins() -> None:
    global _builtins_loaded
    if _builtins_loaded:
        return
    _builtins_loaded = True
    for module in _BUILTIN_MODULES:
        try:
            importlib.import_module(module)
        except Exception:
            # A widget that cannot import must not take the chat UI with it —
            # the dropdown simply offers fewer entries.
            logger.exception("[widgets] could not import %s", module)


def register_widget(
    name: str,
    label: str,
    description: str,
    template: str,
    submit: Callable[[dict[str, Any], dict[str, Any], str], WidgetOutcome],
    *,
    context: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    icon: str = "⚡",
) -> WidgetSpec:
    """Register a widget. Raises on a duplicate name — silent replacement
    would make two widgets fight over one entry in the Tools menu."""
    if name in _registry:
        raise ValueError(f"Widget already registered: {name}")
    spec = WidgetSpec(
        name=name,
        label=label,
        description=description,
        template=template,
        submit=submit,
        context=context,
        icon=icon,
    )
    _registry[name] = spec
    return spec


def get_widget(name: str) -> Optional[WidgetSpec]:
    _ensure_builtins()
    return _registry.get(name)


def list_widgets() -> list[WidgetSpec]:
    """Registered widgets, in a stable order for the Tools menu."""
    _ensure_builtins()
    return sorted(_registry.values(), key=lambda spec: spec.label)


def dispatch_widget(
    name: str, data: dict[str, Any], state: dict[str, Any], user_email: str
) -> WidgetOutcome:
    """Run a widget's submit handler.

    Raises ``KeyError`` for an unknown widget — the caller turns that into a 404
    rather than a 500, because an unknown name is a stale client, not a bug in
    the handler.
    """
    spec = get_widget(name)
    if spec is None:
        raise KeyError(name)
    return spec.submit(data, state, user_email)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def new_widget_state(
    name: str,
    *,
    data: Optional[dict[str, Any]] = None,
    rfq_id: Optional[str] = None,
    widget_id: Optional[str] = None,
) -> dict[str, Any]:
    """A fresh pending widget state.

    ``rfq_id`` is captured when the widget opens rather than read at submit
    time: the user may navigate away from the RFQ (or the panel may lose its
    lock) between opening the form and filling it in, and the submission still
    needs to know which RFQ it was about.

    ``widget_id`` should be the id of the step that will render this state. The
    card's own action URL is built from ``state["id"]``, so the render and
    submit routes resolve that id — if the two ever differ, every request the
    card makes 404s.
    """
    return {
        "id": widget_id or str(uuid.uuid4()),
        "name": name,
        "status": "pending",
        "data": dict(data or {}),
        "rfq_id": rfq_id,
        "error": "",
        "errors": {},
        "duplicates": [],
        "duplicate_checked": False,
        "result": None,
        "notice": "",
    }


def apply_outcome(state: dict[str, Any], outcome: WidgetOutcome) -> dict[str, Any]:
    """Fold a handler's outcome into the stored state (returns a new dict)."""
    updated = dict(state)
    updated["status"] = outcome.status
    if outcome.data:
        updated["data"] = dict(outcome.data)
    updated["error"] = outcome.error
    updated["errors"] = dict(outcome.errors)
    updated["duplicates"] = list(outcome.duplicates)
    updated["duplicate_checked"] = outcome.duplicate_checked
    if outcome.result is not None:
        updated["result"] = outcome.result
    if outcome.notice:
        updated["notice"] = outcome.notice
    return updated


def widget_state_from_metadata(metadata: Any) -> Optional[dict[str, Any]]:
    """The widget state on a step's metadata, or ``None``.

    Shape-checked because metadata is JSONB that predates this feature — older
    steps carry ``actions`` and nothing else.
    """
    if not isinstance(metadata, dict):
        return None
    state = metadata.get(WIDGET_META_KEY)
    if not isinstance(state, dict):
        return None
    if not state.get("id") or not state.get("name"):
        return None
    return state


def form_to_data(form: Any) -> dict[str, Any]:
    """A submitted form as ``{field: str | list[str]}``.

    Repeated keys (checkbox groups, radio-with-list) collapse into a list so a
    handler never has to care whether the browser sent one value or several.
    Non-string values (uploads) are skipped: no widget reads files yet, and
    silently passing an ``UploadFile`` into a DB column is worse than dropping
    it.
    """
    out: dict[str, Any] = {}
    for key, value in form.multi_items():
        if not isinstance(value, str):
            continue
        if key in out:
            existing = out[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                out[key] = [existing, value]
        else:
            out[key] = value
    return out


def rfq_id_for_thread(thread_id: str) -> Optional[str]:
    """The RFQ number this thread is bound to, if any.

    Read from ``rfq_threads`` rather than accepted from the client: the panel's
    lock is client-side UI state, and a widget must link to the RFQ its *thread*
    belongs to even if the user has since navigated elsewhere.
    """
    try:
        from includes.dashboard.database import get_session
        from includes.dashboard.models import RFQThread

        session = get_session()
        try:
            row = (
                session.query(RFQThread)
                .filter(RFQThread.thread_id == thread_id)
                .first()
            )
            return row.rfq_number if row else None
        finally:
            session.close()
    except Exception as exc:
        # A widget without its RFQ is still useful (the supplier gets created).
        logger.warning("[widgets] RFQ binding lookup failed for %s: %s", thread_id, exc)
        return None


async def open_widget_step(
    thread_id: str,
    name: str,
    *,
    data: Optional[dict[str, Any]] = None,
    rfq_id: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    """Persist a widget as its own step. Returns ``(step_id, state)``.

    Shared by both entry points — the composer's Tools menu (no agent run, so
    the route calls this directly) and ``ctx.widget()`` (mid-run, where the
    client is told over SSE). Keeping it in one place is what stops the two
    paths from producing subtly different steps.
    """
    from includes.chat import transcript

    if rfq_id is None:
        rfq_id = await asyncio.to_thread(rfq_id_for_thread, thread_id)
    # One id, not two. The card is rendered from this state and its action URL is
    # built from state["id"], so that id has to be the one the render and submit
    # routes look up. Generating a separate id per layer made every request the
    # card made 404 (they addressed a row that did not exist).
    widget_id = str(uuid.uuid4())
    state = new_widget_state(name, data=data, rfq_id=rfq_id, widget_id=widget_id)
    step_id = await transcript.create_step(
        thread_id,
        type_="widget",
        name="EagleAgent",
        output="",
        metadata={WIDGET_META_KEY: state},
        step_id=widget_id,
    )
    return step_id, state
