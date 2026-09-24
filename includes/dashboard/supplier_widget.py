"""The "Add new supplier" chat widget.

Two jobs in one card, because they are one thought to the user: *create* a
supplier we don't have, and — when the conversation is about an RFQ — *put it on
the lines in front of them*. Splitting those into two steps is what makes people
add the supplier and then forget to use it.

Scope notes (decided with the user, 2026-09-24):

* **Local only.** No NetSuite fields, no vendor creation. The RFQ quotation tab's
  NS+ flow already promotes a local row, and ``_rfq_sync_readiness`` treats "not
  in NetSuite" as a warning rather than a blocker, so nothing downstream waits on
  it. See ``includes/dashboard/supplier_create.py``.
* **Quick add.** Name and email are required; everything else is optional and
  sits behind a disclosure. Most of the time this is a two-field form.
* **Not agent-initiated yet.** The agent asking "is this a new supplier?" and
  then opening this card is the planned phase 2; the framework already supports
  it (``ctx.widget()``), only the agent-side prompt does not exist.
"""

from __future__ import annotations

import logging
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
    validate,
)

logger = logging.getLogger(__name__)

WIDGET_NAME = "add_supplier"

_ACTION_SUBMIT = "submit"
_ACTION_CANCEL = "cancel"

_LINE_ALL = "all"
_LINE_SPECIFIC = "specific"
_LINE_NONE = "none"


# ---------------------------------------------------------------------------
# RFQ line targets
# ---------------------------------------------------------------------------

def rfq_lines(rfq_id: str) -> list[dict[str, Any]]:
    """The RFQ's line items, for the "which lines?" picker.

    Read live at render time rather than cached with the widget: a form can sit
    open while lines are added, edited or removed on the RFQ page.
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
            "description": (item.get("input_description") or "").strip()[:60],
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
    return {
        "fields": SUPPLIER_FIELDS,
        "options": field_options(),
        "rfq": {"id": rfq_id, "lines": rfq_lines(rfq_id)} if rfq_id else None,
        "action_field": ACTION_FIELD,
        "selected_lines": _selected_lines(state),
    }


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


def _link_to_lines(
    state: dict[str, Any], supplier: Any, data: dict[str, Any], user_email: str
) -> dict[str, Any]:
    """Put the new supplier on the chosen RFQ lines.

    Runs *after* the supplier is committed and never rolls it back — the row is
    what the user asked for, and losing it because a line number went stale would
    be a worse outcome than a half-done link. The result reports the two
    separately so the card can say so.

    Entries carry the supplier's id, which routes them through the ``db_linked``
    branch of ``_add_suppliers_to_line_core``: no name matching, no web search,
    and no contact URL required (an unknown name is what gets skipped there).
    """
    rfq_id = state.get("rfq_id")
    mode = str(data.get("line_mode") or _LINE_NONE).strip().lower()
    if not rfq_id or mode == _LINE_NONE:
        return {"lines": [], "error": ""}

    available = [line["line"] for line in rfq_lines(rfq_id)]
    if not available:
        return {"lines": [], "error": f"{rfq_id} has no line items."}

    if mode == _LINE_ALL:
        targets = available
    else:
        wanted = set(_line_numbers(data.get("lines")))
        targets = [line for line in available if line in wanted]
        if not targets:
            return {"lines": [], "error": "No matching lines were selected."}

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
        return {"lines": [], "error": str(exc)}
    if isinstance(outcome, str):          # error string, not the RFQ dict
        return {"lines": [], "error": outcome}

    # Read the result back rather than trusting the return value: the helper
    # returns the RFQ dict whether or not it accepted anything, so a rejected
    # entry is indistinguishable from a success by return value alone. Claiming
    # success we have not verified is exactly the bug above.
    linked = _lines_carrying_supplier(rfq_id, supplier.id, targets)
    if linked is None:
        # Could not read the RFQ back. The write did not error, so report what
        # was asked for rather than inventing a failure.
        return {"lines": targets, "error": ""}
    if not linked:
        plural = "s" if len(targets) > 1 else ""
        joined = ", ".join(str(line) for line in targets)
        return {
            "lines": [],
            "error": f"{rfq_id} did not accept the supplier on line{plural} {joined}.",
        }
    return {"lines": linked, "error": ""}


def _notice(result: dict[str, Any]) -> str:
    """One line for the transcript, so the conversation reflects what happened.

    The card alone would leave the agent unaware — this step is what a later
    turn (and phase 2's agent-initiated flow) actually reads.
    """
    parts = [f"✅ Created supplier **{result['name']}**."]
    if result["lines"]:
        plural = "s" if len(result["lines"]) > 1 else ""
        joined = ", ".join(str(line) for line in result["lines"])
        parts.append(f"Added it as a candidate on line{plural} {joined}.")
    if result["line_error"]:
        parts.append(f"Could not add it to the RFQ: {result['line_error']}")
    return " ".join(parts)


def _submit(
    data: dict[str, Any], state: dict[str, Any], user_email: str
) -> WidgetOutcome:
    action = str(data.get(ACTION_FIELD) or _ACTION_SUBMIT).strip().lower()
    if action == _ACTION_CANCEL:
        return WidgetOutcome(status="cancelled", data=data)

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
        "line_error": link["error"],
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
    label="Add new supplier",
    description="Create a supplier we don't have yet",
    template="chat_ui/widgets/_add_supplier.html",
    submit=_submit,
    context=_context,
    icon="➕",
)
