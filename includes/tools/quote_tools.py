"""
RFQ (Request for Quote) management tools for LangGraph agents.

Provides tools to create, update, and query RFQs stored in the
LangGraph BaseStore under the ("rfqs",) namespace.
"""

import asyncio
import datetime
import logging
from typing import Any, Dict, List, Optional

import chainlit as cl
from langchain_core.tools import tool
from langgraph.store.base import BaseStore

logger = logging.getLogger(__name__)

NAMESPACE = ("rfqs",)

# Per-RFQ lock to prevent concurrent read-modify-write race conditions.
_rfq_locks: Dict[str, asyncio.Lock] = {}

def get_rfq_lock(rfq_id: str) -> asyncio.Lock:
    """Get or create an asyncio.Lock for the given RFQ ID."""
    if rfq_id not in _rfq_locks:
        _rfq_locks[rfq_id] = asyncio.Lock()
    return _rfq_locks[rfq_id]

# Common aliases LLMs use for the "line" parameter
_LINE_ALIASES = ("line", "line_number", "item", "item_number")


def _get_line(data: dict):
    """Extract line number from data, accepting common aliases."""
    for key in _LINE_ALIASES:
        if key in data:
            return data[key]
    return None


def _now_iso() -> str:
    """Return current AEST (UTC+10) timestamp in ISO format."""
    return datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=10))
    ).isoformat(timespec="seconds")


def _today() -> str:
    """Return current AEST date as YYYY-MM-DD."""
    return datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=10))
    ).strftime("%Y-%m-%d")


async def _next_rfq_number(store: BaseStore) -> str:
    """Generate the next sequential RFQ number like RFQ-2026-0042."""
    year = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=10))
    ).year
    prefix = f"RFQ-{year}-"

    existing = await store.asearch(NAMESPACE, limit=1000)
    max_seq = 0
    for item in existing:
        key = item.key
        if key.startswith(prefix):
            try:
                seq = int(key[len(prefix):])
                max_seq = max(max_seq, seq)
            except ValueError:
                pass
    return f"{prefix}{max_seq + 1:04d}"


def _render_rfq_summary(rfq: dict) -> str:
    """Render a single RFQ as a markdown summary block."""
    status_display = rfq.get("status", "draft").replace("_", " ").title()
    assigned = rfq.get("assigned_to", "Unassigned")
    customer = rfq.get("customer", "Unknown")
    created = rfq.get("created_date", "")
    rfq_id = rfq.get("id", "???")
    notes = rfq.get("notes", "")
    ref = rfq.get("reference", "")
    netsuite = rfq.get("netsuite_opportunity", "")
    hubspot = rfq.get("hubspot_deal", "")

    items = rfq.get("items", [])
    total = len(items)
    confirmed = sum(1 for i in items if i.get("status") == "confirmed")
    identified = sum(1 for i in items if i.get("status") == "identified")
    unidentified = sum(1 for i in items if i.get("status") == "unidentified")
    with_suppliers = sum(1 for i in items if i.get("suppliers"))

    lines = [f"## 📋 {rfq_id} — {customer}"]
    meta = [f"**Status:** {status_display}", f"**Assigned to:** {assigned}"]
    if created:
        meta.append(f"**Created:** {created}")
    lines.append(" | ".join(meta))

    if ref:
        lines.append(f"**Reference:** {ref}")
    ext_links = []
    if netsuite:
        ext_links.append(f"NetSuite: {netsuite}")
    if hubspot:
        ext_links.append(f"HubSpot: {hubspot}")
    if ext_links:
        lines.append(" | ".join(ext_links))

    contact = rfq.get("customer_contact")
    if contact:
        parts = []
        if contact.get("name"):
            parts.append(contact["name"])
        if contact.get("email"):
            parts.append(contact["email"])
        if contact.get("phone"):
            parts.append(contact["phone"])
        if parts:
            lines.append(f"**Contact:** {' · '.join(parts)}")

    if notes:
        lines.append(f"**Notes:** {notes}")

    lines.append("")

    if items:
        status_icons = {
            "confirmed": "✅ Confirmed",
            "identified": "🔵 Identified",
            "unidentified": "⚠️ Unidentified",
        }
        lines.append("| # | Description | Part Number | Brand | Qty | Status | Suppliers |")
        lines.append("|---|------------|-------------|-------|-----|--------|-----------|")
        for item in items:
            line_num = item.get("line", "")
            desc = item.get("input_description", "")
            pn = item.get("part_number") or item.get("input_code") or "—"
            brand = item.get("brand") or "—"
            qty = item.get("quantity", "")
            uom = item.get("uom", "")
            qty_str = f"{qty} {uom}".strip() if qty else "—"
            status = status_icons.get(item.get("status", ""), item.get("status", ""))
            suppliers = item.get("suppliers", [])
            if suppliers:
                sup_parts = []
                _status_labels = {
                    "estimated": "est",
                    "previous_purchase": "prev",
                    "previous_quote": "prev",
                    "quoted": "quoted",
                }
                for s in suppliers:
                    name = s.get("name", "?")
                    price = s.get("price")
                    st = s.get("status", "candidate")
                    price_type = s.get("price_type", "")
                    if st == "dropped":
                        sup_parts.append(f"~~{name}~~")
                    elif price is not None:
                        label = _status_labels.get(price_type, "")
                        try:
                            price_str = f"${float(price):,.2f}"
                        except (ValueError, TypeError):
                            price_str = str(price)
                        if label:
                            sup_parts.append(f"{name} ({price_str} {label})")
                        else:
                            sup_parts.append(f"{name} ({price_str})")
                    else:
                        sup_parts.append(name)
                sup_str = "<br>".join(sup_parts)
            else:
                sup_str = "—"
            lines.append(
                f"| {line_num} | {desc} | {pn} | {brand} | {qty_str} | {status} | {sup_str} |"
            )

        lines.append("")
        counts = []
        if confirmed:
            counts.append(f"{confirmed} confirmed")
        if identified:
            counts.append(f"{identified} identified")
        if unidentified:
            counts.append(f"{unidentified} unidentified")
        lines.append(
            f"**{total} items** | {', '.join(counts) if counts else 'none'} | "
            f"{with_suppliers} with suppliers"
        )
    else:
        lines.append("*No items yet.*")

    return "\n".join(lines)


async def _notify_rfq_updated() -> None:
    """Notify the dashboard to refresh after RFQ data changes."""
    from includes.agent_bridge import notify_dashboard
    await notify_dashboard("dashboard_refresh")


def _render_rfq_list(rfqs: list[dict]) -> str:
    """Render a summary table of multiple RFQs."""
    if not rfqs:
        return "No RFQs found."

    lines = ["## 📋 RFQ List", ""]
    lines.append("| RFQ | Customer | Status | Items | Assigned | Created |")
    lines.append("|-----|----------|--------|-------|----------|---------|")
    for rfq in rfqs:
        rfq_id = rfq.get("id", "???")
        customer = rfq.get("customer", "—")
        status = rfq.get("status", "draft").replace("_", " ").title()
        items = rfq.get("items", [])
        total = len(items)
        confirmed = sum(1 for i in items if i.get("status") == "confirmed")
        assigned = rfq.get("assigned_to", "—")
        created = rfq.get("created_date", "")
        lines.append(
            f"| [{rfq_id}](/rfqs/{rfq_id}) | {customer} | {status} | {confirmed}/{total} confirmed | {assigned} | {created} |"
        )
    lines.append("")
    lines.append(f"**{len(rfqs)} RFQs total**")
    return "\n".join(lines)


def create_quote_tools(store: BaseStore, user_id: str) -> list:
    """Create RFQ management tools bound to a store and user.

    Args:
        store: The BaseStore instance for persistent storage
        user_id: The current user's identifier (email)

    Returns:
        List of tools [manage_rfq, get_rfq]
    """

    @tool
    async def manage_rfq(
        action: str,
        rfq_id: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Create or update an RFQ (Request for Quote).

        Actions:
          create        — Create a new RFQ. data keys: customer (required),
                          customer_contact ({name, email, phone}), reference,
                          netsuite_opportunity, hubspot_deal, notes,
                          items ([{input_description, input_code, quantity, uom}])
          update        — Update top-level RFQ properties. data keys: any of
                          customer, customer_contact, reference, notes,
                          netsuite_opportunity, hubspot_deal, assigned_to
          update_item   — Update an RFQ line item. data keys: line (required, int),
                          plus any of: input_description, input_code, part_number,
                          brand, product_id, quantity, uom, status
          add_supplier  — Add supplier candidate(s) to a line item. data keys:
                          line (required), EITHER name (required) for a single
                          supplier with optional supplier_id, contacts, status,
                          price, price_type, lead_time, notes, purchase_ref;
                          OR suppliers (list of dicts with those same keys)
                          to add multiple at once.
                          Supplier status values: candidate (default),
                          shortlisted, selected, dropped.
                          Price type values: estimated (price from web search),
                          previous_purchase (price from purchase history),
                          previous_quote (price from past quote), quoted
                          (new quote received). Omit if no price.
                          purchase_ref: optional dict {doc_number, date,
                          order_count} linking to the latest purchase record.
          update_supplier — Update a supplier on a line item. data keys:
                          line (required), name (required), plus any of: status,
                          price, price_type, lead_time, notes, contacts,
                          purchase_ref
          clear_suppliers — Remove all suppliers from line item(s). data keys:
                          line (optional, int — if omitted clears ALL lines)
          assign        — Reassign the RFQ. data keys: assigned_to (required)
          update_status — Change RFQ status. data keys: status (required, one of
                          draft/in_progress/awaiting_quotes/completed/cancelled)
          add_note      — Append a note. data keys: note (required)
          link_external — Set external IDs. data keys: netsuite_opportunity
                          and/or hubspot_deal

        Args:
            action: The mutation to perform (see above).
            rfq_id: The RFQ identifier (required for all actions except create).
            data: Action-specific payload (see above).
        """
        data = data or {}

        # ---- CREATE ----
        if action == "create":
            customer = data.get("customer")
            if not customer:
                return "Error: 'customer' is required in data when creating an RFQ."

            new_id = await _next_rfq_number(store)
            now = _now_iso()
            rfq: Dict[str, Any] = {
                "id": new_id,
                "customer": customer,
                "customer_contact": data.get("customer_contact"),
                "reference": data.get("reference"),
                "netsuite_opportunity": data.get("netsuite_opportunity"),
                "hubspot_deal": data.get("hubspot_deal"),
                "created_by": user_id,
                "created_date": _today(),
                "assigned_to": data.get("assigned_to", user_id),
                "thread_id": data.get("thread_id"),
                "status": "draft",
                "notes": data.get("notes", ""),
                "items": [],
                "history": [
                    {"date": now, "user": user_id, "action": "Created RFQ"},
                ],
            }

            raw_items = data.get("items", [])
            for idx, raw in enumerate(raw_items, start=1):
                rfq["items"].append({
                    "line": idx,
                    "input_description": raw.get("input_description", ""),
                    "input_code": raw.get("input_code", ""),
                    "part_number": raw.get("part_number"),
                    "brand": raw.get("brand"),
                    "product_id": raw.get("product_id"),
                    "quantity": raw.get("quantity"),
                    "uom": raw.get("uom", "ea"),
                    "status": raw.get("status", "unidentified"),
                    "suppliers": [],
                })

            if raw_items:
                rfq["history"][0]["action"] = f"Created RFQ with {len(raw_items)} items"

            await store.aput(NAMESPACE, new_id, rfq)
            logger.info(f"Created {new_id} for {customer} with {len(raw_items)} items")
            await _notify_rfq_updated()
            return _render_rfq_summary(rfq)

        # ---- All other actions require rfq_id ----
        if not rfq_id:
            return "Error: rfq_id is required for this action."

        async with get_rfq_lock(rfq_id):
            return await _manage_rfq_locked(store, rfq_id, action, data, user_id)

    async def _manage_rfq_locked(store, rfq_id, action, data, user_id):
        """Execute the RFQ mutation while holding the per-RFQ lock."""
        item = await store.aget(NAMESPACE, rfq_id)
        if not item:
            return f"Error: RFQ '{rfq_id}' not found."
        rfq = item.value
        now = _now_iso()

        if action == "update":
            updatable = [
                "customer", "customer_contact", "reference", "notes",
                "netsuite_opportunity", "hubspot_deal", "assigned_to",
            ]
            changes = []
            for key in updatable:
                if key in data:
                    rfq[key] = data[key]
                    changes.append(key)
            if not changes:
                return f"Error: provide at least one of {', '.join(updatable)} to update."
            rfq["history"].append({
                "date": now, "user": user_id,
                "action": f"Updated RFQ: {', '.join(changes)}",
            })

        elif action == "update_item":
            line_num = _get_line(data)
            if line_num is None:
                return "Error: 'line' is required in data for update_item."
            line_item = next((i for i in rfq["items"] if i["line"] == line_num), None)
            if not line_item:
                return f"Error: line {line_num} not found in {rfq_id}."
            updatable = [
                "input_description", "input_code", "part_number", "brand",
                "product_id", "quantity", "uom", "status",
            ]
            changes = []
            for key in updatable:
                if key in data:
                    line_item[key] = data[key]
                    changes.append(key)
            rfq["history"].append({
                "date": now, "user": user_id,
                "action": f"Updated line {line_num}: {', '.join(changes)}",
            })

        elif action == "add_supplier":
            line_num = _get_line(data)
            if line_num is None:
                return "Error: 'line' is required for add_supplier."
            line_item = next((i for i in rfq["items"] if i["line"] == line_num), None)
            if not line_item:
                return f"Error: line {line_num} not found in {rfq_id}."

            # Accept a single supplier (name=...) or a list (suppliers=[...])
            suppliers_list = data.get("suppliers", [])
            if not suppliers_list and data.get("name"):
                suppliers_list = [data]
            if not suppliers_list:
                return "Error: 'name' or 'suppliers' list is required for add_supplier."

            # Filter out useless suppliers: those without a DB link must have
            # a real name and at least one contact detail (email, phone, or url).
            def _has_contact_info(sup):
                contacts = sup.get("contacts") or []
                if not isinstance(contacts, list):
                    return False
                return any(
                    (c.get("email") or c.get("phone") or c.get("url") or c.get("value"))
                    if isinstance(c, dict) else bool(c)
                    for c in contacts
                )

            _bad_names = {"unknown", ""}
            skipped_names = []
            valid_suppliers = []
            for sup in suppliers_list:
                name = (sup.get("name") or "").strip()
                has_db_link = bool(sup.get("supplier_id"))
                if not has_db_link and (name.lower() in _bad_names or not _has_contact_info(sup)):
                    skipped_names.append(name or "Unknown")
                else:
                    valid_suppliers.append(sup)

            added_names = []
            updated_names = []
            existing_by_name = {
                s["name"].lower(): s for s in line_item.get("suppliers", [])
            }
            for sup in valid_suppliers:
                name = sup.get("name", "Unknown")
                existing = existing_by_name.get(name.lower())
                if existing:
                    # Merge into existing supplier — update non-empty fields
                    for key in ["supplier_id", "contacts", "price", "price_type",
                                "lead_time", "notes", "purchase_ref"]:
                        val = sup.get(key)
                        if val is not None and val != "" and val != []:
                            existing[key] = val
                    # Don't overwrite a user-set status (shortlisted/selected)
                    # with a default candidate status
                    new_status = sup.get("status", "candidate")
                    if new_status != "candidate" or existing.get("status") == "candidate":
                        existing["status"] = new_status
                    updated_names.append(name)
                else:
                    supplier_entry = {
                        "supplier_id": sup.get("supplier_id"),
                        "name": name,
                        "contacts": sup.get("contacts", []),
                        "status": sup.get("status", "candidate"),
                        "price": sup.get("price"),
                        "price_type": sup.get("price_type"),
                        "lead_time": sup.get("lead_time"),
                        "notes": sup.get("notes", ""),
                        "purchase_ref": sup.get("purchase_ref"),
                    }
                    line_item["suppliers"].append(supplier_entry)
                    existing_by_name[name.lower()] = supplier_entry
                    added_names.append(name)
            action_parts = []
            if added_names:
                action_parts.append(f"Added {len(added_names)} supplier(s) to line {line_num}: {', '.join(added_names)}")
            if updated_names:
                action_parts.append(f"Updated {len(updated_names)} existing supplier(s) on line {line_num}: {', '.join(updated_names)}")
            if skipped_names:
                action_parts.append(f"Skipped {len(skipped_names)} supplier(s) (missing name or contact info): {', '.join(skipped_names)}")
            rfq["history"].append({
                "date": now, "user": user_id,
                "action": " | ".join(action_parts) or f"No changes to suppliers on line {line_num}",
            })

        elif action == "update_supplier":
            line_num = _get_line(data)
            name = data.get("name")
            if line_num is None or not name:
                return "Error: 'line' and 'name' are required for update_supplier."
            line_item = next((i for i in rfq["items"] if i["line"] == line_num), None)
            if not line_item:
                return f"Error: line {line_num} not found in {rfq_id}."
            supplier = next(
                (s for s in line_item.get("suppliers", []) if s["name"] == name),
                None,
            )
            if not supplier:
                return f"Error: supplier '{name}' not found on line {line_num}."
            updatable = ["status", "price", "price_type", "lead_time", "notes", "contacts", "purchase_ref"]
            changes = []
            for key in updatable:
                if key in data:
                    supplier[key] = data[key]
                    changes.append(key)
            rfq["history"].append({
                "date": now, "user": user_id,
                "action": f"Updated supplier '{name}' on line {line_num}: {', '.join(changes)}",
            })

        elif action == "clear_suppliers":
            line_num = data.get("line")  # optional — None means all lines
            cleared = []
            for item in rfq["items"]:
                if line_num is not None and item["line"] != line_num:
                    continue
                count = len(item.get("suppliers", []))
                if count:
                    item["suppliers"] = []
                    cleared.append(f"line {item['line']} ({count})")
            if not cleared:
                scope = f"line {line_num}" if line_num else "any line"
                return f"No suppliers to clear on {scope}."
            rfq["history"].append({
                "date": now, "user": user_id,
                "action": f"Cleared suppliers from {', '.join(cleared)}",
            })

        elif action == "assign":
            assigned_to = data.get("assigned_to")
            if not assigned_to:
                return "Error: 'assigned_to' is required for assign."
            rfq["assigned_to"] = assigned_to
            rfq["history"].append({
                "date": now, "user": user_id,
                "action": f"Assigned to {assigned_to}",
            })

        elif action == "update_status":
            new_status = data.get("status")
            valid = {"draft", "in_progress", "awaiting_quotes", "completed", "cancelled"}
            if new_status not in valid:
                return f"Error: status must be one of {', '.join(sorted(valid))}."
            rfq["status"] = new_status
            rfq["history"].append({
                "date": now, "user": user_id,
                "action": f"Status changed to {new_status}",
            })

        elif action == "add_note":
            note = data.get("note", "")
            if not note:
                return "Error: 'note' is required for add_note."
            existing = rfq.get("notes", "")
            rfq["notes"] = f"{existing}\n{note}".strip() if existing else note
            rfq["history"].append({
                "date": now, "user": user_id,
                "action": f"Added note: {note[:80]}",
            })

        elif action == "link_external":
            linked = []
            if "netsuite_opportunity" in data:
                rfq["netsuite_opportunity"] = data["netsuite_opportunity"]
                linked.append(f"NetSuite: {data['netsuite_opportunity']}")
            if "hubspot_deal" in data:
                rfq["hubspot_deal"] = data["hubspot_deal"]
                linked.append(f"HubSpot: {data['hubspot_deal']}")
            if not linked:
                return "Error: provide netsuite_opportunity and/or hubspot_deal."
            rfq["history"].append({
                "date": now, "user": user_id,
                "action": f"Linked {', '.join(linked)}",
            })

        else:
            return (
                f"Error: unknown action '{action}'. Valid actions: create, "
                "update, update_item, add_supplier, update_supplier, "
                "clear_suppliers, assign, update_status, add_note, link_external."
            )

        rfq["updated_date"] = _today()
        await store.aput(NAMESPACE, rfq["id"], rfq)
        await _notify_rfq_updated()
        return _render_rfq_summary(rfq)

    @tool
    async def get_rfq(
        rfq_id: Optional[str] = None,
        list_all: bool = False,
        assigned_to: Optional[str] = None,
        status: Optional[str] = None,
    ) -> str:
        """Retrieve RFQ details or list RFQs.

        Usage:
          get_rfq(rfq_id="RFQ-2026-0042")       — full detail of one RFQ
          get_rfq(list_all=True)                  — summary list of all RFQs
          get_rfq(assigned_to="tom@eagle.com.au") — RFQs assigned to a user
          get_rfq(status="in_progress")           — filter by status

        Args:
            rfq_id: Specific RFQ identifier to retrieve.
            list_all: If True, return a summary of all RFQs.
            assigned_to: Filter RFQs by assignee email.
            status: Filter RFQs by status.
        """
        # Single RFQ lookup
        if rfq_id:
            item = await store.aget(NAMESPACE, rfq_id)
            if not item:
                return f"RFQ '{rfq_id}' not found."
            await _notify_rfq_updated()
            return _render_rfq_summary(item.value)

        # List / filter mode
        filters: Dict[str, Any] = {}
        if assigned_to:
            filters["assigned_to"] = assigned_to
        if status:
            filters["status"] = status

        results = await store.asearch(
            NAMESPACE, filter=filters if filters else None, limit=200
        )
        rfqs = [r.value for r in results]

        # Sort by created_date descending
        rfqs.sort(key=lambda r: r.get("created_date", ""), reverse=True)

        if not list_all and not assigned_to and not status:
            # Default: show current user's RFQs
            rfqs = [r for r in rfqs if r.get("assigned_to") == user_id]
            if not rfqs:
                return "You have no RFQs assigned. Use `get_rfq(list_all=True)` to see all."

        return _render_rfq_list(rfqs)

    return [manage_rfq, get_rfq]
