"""The supplier chat widget — find one we already have, or add a new one.

One card, because it is one thought to the user: *get this supplier onto the
RFQ*. Splitting it into "find" and "create" is what makes people add a supplier
and then forget to use it.

The card opens in one of three views (``mode``):

* ``search`` — company-name typeahead over existing suppliers (no RFQ bound →
  there is nothing to attach an existing supplier to, so the card skips straight
  to ``create`` instead of offering a dead end),
* ``chosen`` — a picked existing supplier plus the line picker,
* ``create`` — the quick-add form for one we do not have.

Scope notes (decided with the user):

* **Local only.** No NetSuite fields, no vendor creation. The RFQ quotation
  tab's NS+ flow already promotes a local row, and ``_rfq_sync_readiness``
  treats "not in NetSuite" as a warning rather than a blocker. See
  ``includes/dashboard/supplier_create.py``.
* **Quick add.** Name and email are required on the create path; everything else
  is optional and behind a disclosure.
* **Not agent-initiated yet.** The agent asking "is this a new supplier?" and
  then opening this card is phase 2; the framework already supports it
  (``ctx.widget()``), only the agent-side prompt does not exist.
* **Contact selection** (which of a supplier's contacts to use) is phase 3.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from includes.chat.widgets import (
    ACTION_FIELD,
    WidgetOutcome,
    register_widget,
)
from includes.dashboard.supplier_create import (
    SUPPLIER_FIELDS,
    create_local_supplier,
    field_options,
    find_duplicates,
    lookup_suppliers,
    primary_email,
    validate,
)

logger = logging.getLogger(__name__)

WIDGET_NAME = "add_supplier"

_ACTION_SUBMIT = "submit"
_ACTION_CANCEL = "cancel"

_LINE_ALL = "all"
_LINE_SPECIFIC = "specific"
_LINE_NONE = "none"

#: The card's views. ``mode`` is a form field, so a re-render after a validation
#: error lands in the view the user was actually in.
_MODE_SEARCH = "search"
_MODE_CHOSEN = "chosen"
_MODE_CREATE = "create"
_MODES = (_MODE_SEARCH, _MODE_CHOSEN, _MODE_CREATE)


@dataclass(frozen=True)
class _ExistingSupplier:
    """The parts of a Supplier row the link path needs.

    A detached copy, so the link (which opens its own sessions and can run long)
    never touches a row from a closed session.
    """

    id: str
    name: str


# ---------------------------------------------------------------------------
# RFQ line targets
# ---------------------------------------------------------------------------

def rfq_lines(rfq_id: str) -> list[dict[str, Any]]:
    """The RFQ's line items, for the "which lines?" picker.

    Read live at render time rather than cached with the widget: a form can sit
    open while lines are added, edited or removed on the RFQ page.

    Each line carries its current supplier ids, so the submit path can tell
    "added to line 1" from "was already on line 1" without a second read.

    The brand is included because it is what tells lines apart on a multi-brand
    RFQ (line 1 Bahco, lines 2-6 Milwaukee): a part number and a truncated
    description do not, and picking the wrong line links a supplier to the wrong
    hardware. About half of all lines have one, so it is optional.
    """
    from includes.tools.quote_tools import _get_rfq_dict_sync

    try:
        rfq = _get_rfq_dict_sync(rfq_id) or {}
    except Exception:
        logger.exception("[add-supplier] could not read %s for line targets", rfq_id)
        return []

    lines: list[dict[str, Any]] = []
    for item in rfq.get("items") or []:
        line = item.get("line")
        if line is None:
            continue
        lines.append({
            "line": int(line),
            "part_number": (item.get("part_number") or "").strip(),
            "brand": (item.get("brand") or "").strip(),
            "description": (item.get("input_description") or "").strip()[:60],
            "supplier_ids": [
                str(sup.get("supplier_id"))
                for sup in (item.get("suppliers") or [])
                if sup.get("supplier_id")
            ],
        })
    return sorted(lines, key=lambda entry: entry["line"])


def _line_numbers(raw: Any) -> list[int]:
    """Line numbers out of a form value, which may be one string or a list."""
    values = raw if isinstance(raw, list) else ([raw] if raw else [])
    out: list[int] = []
    for value in values:
        try:
            out.append(int(str(value).strip()))
        except (TypeError, ValueError):
            continue
    return out


def _selected_lines(state: dict[str, Any]) -> list[int]:
    """Line numbers ticked on a previous submission (so a re-render keeps them)."""
    return _line_numbers((state.get("data") or {}).get("lines"))


def _context(state: dict[str, Any]) -> dict[str, Any]:
    """Extra template context — the framework stays generic, the widget supplies
    its own field spec, option lists and line targets."""
    rfq_id = state.get("rfq_id")
    data = state.get("data") or {}
    # A submitted value wins, so a validation error or duplicate confirm
    # re-renders the view the user was in rather than snapping back to search.
    mode = str(data.get("mode") or "").strip()
    if mode not in _MODES:
        # Without an RFQ there is nothing to attach an existing supplier to, so
        # the lookup would be a dead end — open in the create form instead.
        mode = _MODE_SEARCH if rfq_id else _MODE_CREATE
    return {
        "fields": SUPPLIER_FIELDS,
        "options": field_options(),
        "rfq": {"id": rfq_id, "lines": rfq_lines(rfq_id)} if rfq_id else None,
        "action_field": ACTION_FIELD,
        "selected_lines": _selected_lines(state),
        "mode": mode,
    }


def _lookup(query: str, state: dict[str, Any], user_email: str) -> dict[str, Any]:
    """Typeahead for the search view — see ``supplier_create.lookup_suppliers``."""
    return {"results": lookup_suppliers(query)}


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

def _truthy(value: Any) -> bool:
    """Checkbox semantics: an unticked box is absent from the form entirely."""
    return str(value or "").strip().lower() not in ("", "0", "false", "no", "off")


def _lines_carrying_supplier(
    rfq_id: str, supplier_id: Any, lines: list[int]
) -> list[int] | None:
    """Which of ``lines`` now hold this supplier, read back from the RFQ.

    ``None`` means the read itself failed — the caller must not turn that into a
    failure report, because the write may well have landed.
    """
    from includes.tools.quote_tools import _get_rfq_dict_sync

    try:
        rfq = _get_rfq_dict_sync(rfq_id) or {}
    except Exception:
        logger.exception("[add-supplier] could not verify the write to %s", rfq_id)
        return None

    wanted = set(lines)
    found: list[int] = []
    for item in rfq.get("items") or []:
        line = item.get("line")
        if line is None or int(line) not in wanted:
            continue
        for entry in item.get("suppliers") or []:
            if str(entry.get("supplier_id") or "") == str(supplier_id):
                found.append(int(line))
                break
    return sorted(found)


def _lines_already_on(
    lines_now: list[dict[str, Any]], supplier_id: Any, targets: list[int]
) -> list[int]:
    """Target lines that already carry this supplier, from a prior line read."""
    wanted = set(targets)
    return sorted(
        line["line"]
        for line in lines_now
        if line["line"] in wanted
        and str(supplier_id) in (line.get("supplier_ids") or ())
    )


def _existing_or_none(supplier_id: str) -> Any:
    """The supplier row for a client-supplied id, or ``None``.

    Merged (``use_instead``) and inactive rows count as missing: attaching one to
    an RFQ is exactly what the linking rule in ``supplier_lookup`` exists to
    prevent, so the widget must not do it through the back door either.
    """
    from includes.dashboard.database import get_session
    from includes.dashboard.models import Supplier

    try:
        wanted = uuid.UUID(supplier_id)
    except (ValueError, AttributeError, TypeError):
        return None

    session = get_session()
    try:
        row = session.query(Supplier).filter(Supplier.id == wanted).first()
        if row is None or row.isinactive or row.use_instead is not None:
            return None
        return {
            "supplier": _ExistingSupplier(id=str(row.id), name=row.name),
            "email": primary_email(row, session),
        }
    finally:
        session.close()


def _link_to_lines(
    state: dict[str, Any], supplier: Any, data: dict[str, Any], user_email: str
) -> dict[str, Any]:
    """Put a supplier on the chosen RFQ lines.

    Runs *after* the supplier exists and never rolls it back — the row is what
    the user asked for, and losing it because a line number went stale would be a
    worse outcome than a half-done link. The result reports the two separately so
    the card can say so.

    Entries carry the supplier's id, which routes them through the ``db_linked``
    branch of ``_add_suppliers_to_line_core``: no name matching, no web search,
    and no contact URL required (an unknown name is what gets skipped there).

    Every path returns ``lines`` (newly linked), ``already_on`` (were already
    there) and ``error``.
    """
    rfq_id = state.get("rfq_id")
    mode = str(data.get("line_mode") or _LINE_NONE).strip().lower()
    if not rfq_id or mode == _LINE_NONE:
        return {"lines": [], "already_on": [], "error": ""}

    lines_now = rfq_lines(rfq_id)
    available = [line["line"] for line in lines_now]
    if not available:
        return {"lines": [], "already_on": [], "error": f"{rfq_id} has no line items."}

    if mode == _LINE_ALL:
        targets = available
    else:
        wanted = set(_line_numbers(data.get("lines")))
        targets = [line for line in available if line in wanted]
        if not targets:
            return {
                "lines": [], "already_on": [],
                "error": "No matching lines were selected.",
            }

    # A supplier already on a line is merged into that entry rather than added,
    # so "Added to line 1" for one that was already there would be a lie. Read the
    # pre-write state from the same fetch that produced the line list.
    before = _lines_already_on(lines_now, supplier.id, targets)

    from includes.tools.rfq_crud import _add_suppliers_bulk_sync

    # Flat supplier dicts carrying a "line" key — that is the shape
    # _add_suppliers_bulk_sync expects. Nesting the supplier under "suppliers"
    # here made every entry arrive nameless and be rejected on the quiet
    # ("Line 1 (rejected 1: Unknown)") while the call still returned the RFQ
    # looking like a success, so the card claimed a line link that never
    # happened (RFQ-2026-1231, 2026-09-24).
    entries = [
        {"line": line, "supplier_id": str(supplier.id), "name": supplier.name}
        for line in targets
    ]
    try:
        outcome = _add_suppliers_bulk_sync(rfq_id, {"entries": entries}, user_email)
    except Exception as exc:
        logger.exception("[add-supplier] line link failed for %s", rfq_id)
        return {"lines": [], "already_on": [], "error": str(exc)}
    if isinstance(outcome, str):          # error string, not the RFQ dict
        return {"lines": [], "already_on": [], "error": outcome}

    # Read the result back rather than trusting the return value: the helper
    # returns the RFQ dict whether or not it accepted anything, so a rejected
    # entry is indistinguishable from a success by return value alone. Claiming
    # success we have not verified is exactly the bug above.
    linked = _lines_carrying_supplier(rfq_id, supplier.id, targets)
    if linked is None:
        # Could not read the RFQ back. The write did not error, so report what
        # was asked for rather than inventing a failure.
        return {
            "lines": [line for line in targets if line not in before],
            "already_on": before,
            "error": "",
        }
    if not linked:
        plural = "s" if len(targets) > 1 else ""
        joined = ", ".join(str(line) for line in targets)
        return {
            "lines": [], "already_on": [],
            "error": f"{rfq_id} did not accept the supplier on line{plural} {joined}.",
        }
    return {
        "lines": [line for line in linked if line not in before],
        "already_on": [line for line in linked if line in before],
        "error": "",
    }


def _csv(values: list[Any]) -> str:
    return ", ".join(str(value) for value in values)


def _line_word(lines: list[Any]) -> str:
    return "line" if len(lines) == 1 else "lines"


def _notice(result: dict[str, Any]) -> str:
    """One line for the transcript, so the conversation reflects what happened.

    The card alone would leave the agent unaware — this step is what a later
    turn (and phase 2's agent-initiated flow) actually reads.
    """
    name = result["name"]
    lines = list(result.get("lines") or [])
    already = list(result.get("already_on") or [])

    if result.get("existing"):
        if lines:
            head = f"✅ Added **{name}** as a candidate on {_line_word(lines)} {_csv(lines)}."
        elif already:
            head = (
                f"✅ **{name}** was already a candidate on "
                f"{_line_word(already)} {_csv(already)}."
            )
        else:
            head = f"✅ Selected **{name}**."
    else:
        head = f"✅ Created supplier **{name}**."
        if lines:
            head += f" Added it as a candidate on {_line_word(lines)} {_csv(lines)}."

    parts = [head]
    if lines and already:
        parts.append(f"(already on {_line_word(already)} {_csv(already)})")
    if result["line_error"]:
        parts.append(f"Could not add it to the RFQ: {result['line_error']}")
    return " ".join(parts)


def _submit(
    data: dict[str, Any], state: dict[str, Any], user_email: str
) -> WidgetOutcome:
    """Cancel, or one of the two paths.

    The path is decided by whether a supplier was *picked*, not by the client's
    ``mode`` label: a picked id is the thing that changes the behaviour, and a
    client-supplied label is not something to trust with a write.
    """
    action = str(data.get(ACTION_FIELD) or _ACTION_SUBMIT).strip().lower()
    if action == _ACTION_CANCEL:
        return WidgetOutcome(status="cancelled", data=data)

    supplier_id = str(data.get("supplier_id") or "").strip()
    if supplier_id:
        return _submit_existing(supplier_id, data, state, user_email)
    return _submit_new(data, state, user_email)


def _submit_existing(
    supplier_id: str, data: dict[str, Any], state: dict[str, Any], user_email: str
) -> WidgetOutcome:
    """Add a supplier we already have to the RFQ's lines.

    Nothing is created and no duplicate check runs — the only write is the line
    link. The id arrives from the client, so it is re-validated rather than
    trusted: a stale pick (the user edited the name after selecting one) would
    otherwise put the *wrong supplier* on an RFQ.
    """
    from includes.dashboard.supplier_matching import normalize_supplier_name

    found = _existing_or_none(supplier_id)
    if found is None:
        return WidgetOutcome(
            status="pending",
            data={**data, "mode": _MODE_SEARCH, "supplier_id": ""},
            error="That supplier is no longer available — please search again.",
        )

    supplier = found["supplier"]
    submitted_name = str(data.get("name") or "").strip()
    if not submitted_name or (
        normalize_supplier_name(submitted_name)
        != normalize_supplier_name(supplier.name)
    ):
        # The name and the id disagree: a stale selection, not a submission.
        return WidgetOutcome(
            status="pending",
            data={**data, "mode": _MODE_SEARCH, "supplier_id": ""},
            error="That selection went stale — please pick the supplier again.",
        )

    link = _link_to_lines(state, supplier, data, user_email)
    result = {
        "supplier_id": supplier.id,
        "name": supplier.name,
        "email": found["email"],
        "lines": link["lines"],
        "already_on": link["already_on"],
        "line_error": link["error"],
        "existing": True,
    }
    # Only a real change leaves the page behind the panel stale.
    changed_rfq = state.get("rfq_id") if link["lines"] else None
    dashboard = (
        {"command": "dashboard_refresh", "payload": {"rfq_id": changed_rfq}}
        if changed_rfq
        else None
    )
    return WidgetOutcome(
        status="submitted", result=result, notice=_notice(result), dashboard=dashboard
    )


def _submit_new(
    data: dict[str, Any], state: dict[str, Any], user_email: str
) -> WidgetOutcome:
    """Create the supplier, then put it on the chosen lines."""
    clean, errors = validate(data)
    if errors:
        return WidgetOutcome(
            status="pending", data=data, errors=errors,
            error="Please fix the highlighted fields.",
        )

    # Check before creating, not after: the confirm step is only useful while
    # nothing has been written yet.
    report = find_duplicates(
        clean["name"], url=clean.get("url") or None, country=clean.get("country") or None
    )
    if report.display and not _truthy(data.get("duplicate_checked")):
        return WidgetOutcome(status="pending", data=data, duplicates=report.display)

    try:
        supplier = create_local_supplier(
            clean, user_email=user_email, near_misses=report.near_misses
        )
    except Exception as exc:
        logger.exception("[add-supplier] could not create supplier")
        return WidgetOutcome(
            status="pending", data=data,
            error=f"Could not save the supplier: {exc}",
        )

    link = _link_to_lines(state, supplier, data, user_email)
    result = {
        "supplier_id": str(supplier.id),
        "name": supplier.name,
        "email": clean.get("email") or "",
        "contact_name": clean.get("contact_name") or "",
        "country": clean.get("country") or "",
        "currency": clean.get("currency") or "",
        "lines": link["lines"],
        "already_on": link["already_on"],
        "line_error": link["error"],
        "existing": False,
    }
    # The RFQ page behind the panel is now stale (a new supplier is on its
    # lines), and nothing else will tell it: this ran outside an agent turn, so
    # there is no run to emit a refresh. Ask the shell directly.
    changed_rfq = state.get("rfq_id") if link["lines"] else None
    dashboard = (
        {"command": "dashboard_refresh", "payload": {"rfq_id": changed_rfq}}
        if changed_rfq
        else None
    )
    return WidgetOutcome(
        status="submitted", result=result, notice=_notice(result), dashboard=dashboard
    )


register_widget(
    WIDGET_NAME,
    label="Add supplier",
    description="Find a supplier we already have, or add a new one",
    template="chat_ui/widgets/_add_supplier.html",
    submit=_submit,
    context=_context,
    lookup=_lookup,
    icon="➕",
)
