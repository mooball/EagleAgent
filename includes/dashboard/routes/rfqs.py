"""RFQ routes: list, detail, create, update items/suppliers, price history."""

import asyncio
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from includes.dashboard.models import Supplier, Transaction, EmailTracking, Contact
from . import _helpers
from ._helpers import router, templates, require_user, _render
from .api import _lookup_rfq_thread_id

RFQ_PAGE_SIZE = 25
RFQ_ALLOWED_TABS = {"items", "suppliers", "communications", "quotation"}


# ---------------------------------------------------------------------------
# RFQ helpers
# ---------------------------------------------------------------------------
def _get_store():
    """Lazily import the shared store instance from includes.graph."""
    import includes.graph as _graph_module
    return _graph_module.store


def _normalize_rfq_suppliers(rfq: dict) -> None:
    """Ensure every supplier dict in the RFQ has all expected keys with safe defaults."""
    _SUPPLIER_DEFAULTS = {
        "name": "",
        "price": None,
        "price_type": None,
        "lead_time": None,
        "status": "candidate",
        "notes": None,
        "supplier_id": None,
        "contacts": [],
        "country": None,
        "currency": None,
        "tier": None,
        "category": None,
        "source": None,
        "is_new": False,
    }
    for item in rfq.get("items", []):
        suppliers = item.get("suppliers", [])
        cleaned = []
        for sup in suppliers:
            if isinstance(sup, str):
                cleaned.append({**_SUPPLIER_DEFAULTS, "name": sup})
            elif isinstance(sup, dict):
                for key, default in _SUPPLIER_DEFAULTS.items():
                    sup.setdefault(key, default)
                if not isinstance(sup["contacts"], list):
                    sup["contacts"] = []
                cleaned.append(sup)
        item["suppliers"] = cleaned


def _enrich_rfq_supplier_contacts(rfq: dict) -> None:
    """Back-fill missing supplier contacts, terms, and tier from the DB."""
    from includes.dashboard.database import (
        match_supplier_by_name,
        merge_supplier_contacts,
    )

    _normalize_rfq_suppliers(rfq)

    by_id: dict[str, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    for item in rfq.get("items", []):
        for sup in item.get("suppliers", []):
            sid = sup.get("supplier_id")
            if sid:
                by_id.setdefault(sid, []).append(sup)
            else:
                name = (sup.get("name") or "").strip().lower()
                if name:
                    by_name.setdefault(name, []).append(sup)

    if not by_id and not by_name:
        return

    session = _helpers.get_session()
    try:
        if by_id:
            rows = session.query(
                Supplier.id, Supplier.supply_chain_position, Supplier.terms,
                Supplier.country, Supplier.currency, Supplier.source,
            ).filter(
                Supplier.id.in_(list(by_id.keys()))
            ).all()
            # Fetch contacts from the authoritative Contact table only
            contact_rows = session.query(Contact).filter(
                Contact.supplier_id.in_(list(by_id.keys())),
                Contact.isinactive == False,
            ).all()
            contacts_by_supplier: dict[str, list[dict]] = {}
            for c in contact_rows:
                sid = str(c.supplier_id)
                contacts_by_supplier.setdefault(sid, []).append({
                    "name": c.fullname,
                    "email": c.email,
                    "phone": c.phone,
                    "label": c.label,
                })

            for row in rows:
                sid = str(row.id)
                if sid in by_id:
                    scp = row.supply_chain_position or {}
                    for sup in by_id[sid]:
                        ct_contacts = contacts_by_supplier.get(sid)
                        if ct_contacts:
                            merge_supplier_contacts(sup, ct_contacts)
                        if scp.get("tier") and not sup.get("tier"):
                            sup["tier"] = scp["tier"]
                        if scp.get("category") and not sup.get("category"):
                            sup["category"] = scp["category"]
                        if row.terms and not sup.get("terms"):
                            sup["terms"] = row.terms
                        if row.country and not sup.get("country"):
                            sup["country"] = row.country
                        if row.currency and not sup.get("currency"):
                            sup["currency"] = row.currency
                        if row.source:
                            sup["source"] = row.source

        all_supplier_ids = set(by_id.keys())
        if all_supplier_ids:
            used_ids = {
                str(r[0])
                for r in session.query(Transaction.supplier_id)
                .filter(Transaction.supplier_id.in_(list(all_supplier_ids)))
                .distinct()
                .all()
            }
            for sid, sup_list in by_id.items():
                if sid not in used_ids:
                    for sup in sup_list:
                        sup["is_new"] = True

        if by_name:
            for name_lower, sup_list in by_name.items():
                matched = match_supplier_by_name(name_lower, session=session)
                if matched:
                    scp = matched.supply_chain_position or {}
                    # Fetch authoritative contacts from Contact table
                    matched_ct = session.query(Contact).filter(
                        Contact.supplier_id == matched.id,
                        Contact.isinactive == False,
                    ).all()
                    matched_contacts = [
                        {"name": c.fullname, "email": c.email, "phone": c.phone, "label": c.label}
                        for c in matched_ct
                    ]
                    for sup in sup_list:
                        if matched_contacts:
                            merge_supplier_contacts(sup, matched_contacts)
                        if not sup.get("supplier_id"):
                            sup["supplier_id"] = str(matched.id)
                        if scp.get("tier") and not sup.get("tier"):
                            sup["tier"] = scp["tier"]
                        if scp.get("category") and not sup.get("category"):
                            sup["category"] = scp["category"]
                        if matched.terms and not sup.get("terms"):
                            sup["terms"] = matched.terms
                        if matched.country and not sup.get("country"):
                            sup["country"] = matched.country
                        if matched.currency and not sup.get("currency"):
                            sup["currency"] = matched.currency
                        if matched.source:
                            sup["source"] = matched.source
                        if sup.get("supplier_id"):
                            has_txn = session.query(Transaction.id).filter(
                                Transaction.supplier_id == sup["supplier_id"]
                            ).first()
                            if not has_txn:
                                sup["is_new"] = True

        for item in rfq.get("items", []):
            for sup in item.get("suppliers", []):
                if not sup.get("supplier_id"):
                    sup["is_new"] = True
    finally:
        session.close()


def _normalize_rfq_tab(tab: str | None) -> str:
    tab_norm = (tab or "items").strip().lower()
    return tab_norm if tab_norm in RFQ_ALLOWED_TABS else "items"


def _infer_rfq_tab_from_request(request: Request, default: str = "items") -> str:
    """Infer active RFQ tab from explicit query, or fall back to default.

    Referer-based inference is intentionally NOT used — it caused double-click
    issues where the old tab from the referer would override the requested tab.
    """
    explicit = _normalize_rfq_tab(request.query_params.get("tab"))
    if explicit != "items" or request.query_params.get("tab"):
        return explicit

    return _normalize_rfq_tab(default)


def _resolve_salutation_name(contacts: list[dict], entity_type: str = "supplier") -> str | None:
    """Return a contact's first name for email salutations.

    Contacts are already loaded into the entity dict by the enrichment step.
    This just picks the best name from the list — no DB query needed.

    Args:
        contacts: List of contact dicts from the Contact table.
                  Keys: name, email, phone, label.
        entity_type: 'supplier' or 'customer'.

    Priority:
    1. Preferred label for entity type:
       - supplier  → 'Source'
       - customer  → 'Main'
    2. Any contact with a real name.
    3. None — caller/template uses fallback text.
    """
    if not contacts:
        return None

    def _is_real(name: str) -> bool:
        name = name.strip()
        if not name or len(name) < 2:
            return False
        if "@" in name:       # email stored in the name field (data quality)
            return False
        low = name.lower()
        if low in ("unknown", "n/a", "na", "none", "-", "null", "test", "undefined"):
            return False
        return True

    preferred = "Source" if entity_type == "supplier" else "Main"

    for label_priority in (preferred, None):
        for c in contacts:
            if isinstance(c, dict) and c.get("label") == label_priority:
                full = (c.get("name") or "").strip()
                if not full:
                    continue
                first = full.split()[0]
                if _is_real(first):
                    return first

    return None


def _build_rfq_supplier_email_data(rfq: dict) -> list[dict]:
    """Group shortlisted suppliers with their line items for email template rendering."""
    supplier_map: dict[str, dict] = {}
    for item in rfq.get("items", []):
        for sup in item.get("suppliers", []):
            if sup.get("status") != "shortlisted":
                continue
            name = (sup.get("name") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key not in supplier_map:
                email = None
                contact_name = None
                url = None
                supplier_id = sup.get("supplier_id")
                contacts = sup.get("contacts") or []
                for c in contacts:
                    if isinstance(c, dict):
                        if c.get("email") and not email:
                            email = c["email"]
                        if c.get("name") and not contact_name:
                            contact_name = c["name"]
                        if c.get("url") and not url:
                            url = c["url"]
                supplier_map[key] = {
                    "name": name,
                    "email": email,
                    "contact_name": contact_name,
                    "url": url,
                    "supplier_id": supplier_id,
                    "country": sup.get("country"),
                    "currency": sup.get("currency"),
                    "salutation_name": _resolve_salutation_name(contacts, entity_type="supplier"),
                    "line_items": [],
                }
            supplier_map[key]["line_items"].append({
                "line": item.get("line"),
                "description": item.get("input_description") or "—",
                "part_number": item.get("part_number") or "—",
                "brand": item.get("brand") or "",
                "quantity": item.get("quantity") or "—",
                "uom": item.get("uom") or "",
            })
    
    # Check email_tracking to see which suppliers have been contacted on this RFQ
    _enrich_supplier_contact_status(rfq.get("rfq_number") or rfq.get("id", ""), supplier_map)
    
    return sorted(supplier_map.values(), key=lambda s: s["name"].lower())


def _enrich_supplier_contact_status(rfq_id: str, supplier_map: dict[str, dict]) -> None:
    """Add has_been_emailed flag to each supplier in the map.

    Checks email_tracking for any sent or received email linked to this RFQ
    and matched to the supplier (by supplier_id or email address).
    """
    if not rfq_id or not supplier_map:
        return

    from sqlalchemy import text
    from uuid import UUID

    session = _helpers.get_session()
    try:
        # Build lookup: supplier_id → key, email → key
        id_to_key: dict[str, str] = {}
        email_to_key: dict[str, str] = {}
        for key, sup in supplier_map.items():
            sid = sup.get("supplier_id")
            if sid:
                try:
                    UUID(str(sid))
                    id_to_key[str(sid)] = key
                except (ValueError, TypeError):
                    pass
            if sup.get("email"):
                email_to_key[sup["email"].lower().strip()] = key

        if not id_to_key and not email_to_key:
            return

        # Build OR conditions for supplier_id matches
        conditions = []
        params = {"rfq_id": rfq_id}
        if id_to_key:
            conditions.append("et.supplier_id = ANY(:supplier_ids)")
            params["supplier_ids"] = list(id_to_key.keys())

        rows = session.execute(
            text(f"""
                SELECT DISTINCT
                    et.supplier_id::text AS sid,
                    et.recipient_email,
                    et.sender_email
                FROM email_tracking et
                WHERE (et.rfq_id = :rfq_id OR et.rfq_token = :rfq_id)
                  AND et.direction IN ('sent', 'received', 'draft')
                  AND (
                    {' OR '.join(conditions) if conditions else 'FALSE'}
                  )
            """),
            params,
        ).mappings().all()

        # Mark contacted suppliers
        for row in rows:
            key = None
            if row["sid"] and row["sid"] in id_to_key:
                key = id_to_key[row["sid"]]
            if not key and row["recipient_email"]:
                key = email_to_key.get(row["recipient_email"].lower().strip())
            if not key and row["sender_email"]:
                key = email_to_key.get(row["sender_email"].lower().strip())
            if key and key in supplier_map:
                supplier_map[key]["has_been_emailed"] = True
    finally:
        session.close()


def _get_all_user_emails() -> list[dict]:
    """Return a list of users with email and name from netsuite_employee_mappings."""
    from sqlalchemy import text
    session = _helpers.get_session()
    try:
        rows = session.execute(
            text("SELECT email, name FROM netsuite_employee_mappings WHERE email IS NOT NULL AND is_active = true ORDER BY name")
        ).fetchall()
        return [{"email": r[0], "name": r[1]} for r in rows]
    finally:
        session.close()


def _rfq_detail_context(rfq: dict, user: dict, active_tab: str) -> dict:
    ctx = {
        "user": user,
        "rfq": rfq,
        "rfq_thread_id": _lookup_rfq_thread_id(rfq["id"], user.get("email", "")),
        "active_tab": _normalize_rfq_tab(active_tab),
        "all_users": _get_all_user_emails(),
    }
    if ctx["active_tab"] == "suppliers":
        ctx["suppliers"] = _build_rfq_supplier_email_data(rfq)
    if ctx["active_tab"] == "communications":
        ctx["email_groups"] = _get_rfq_email_events(rfq["id"], rfq.get("rfq_number"))
    return ctx


def _get_rfq_email_events(rfq_id: str, rfq_number: str = None) -> list[dict]:
    """Fetch logged email events for an RFQ, grouped by source then by gmail thread."""
    from sqlalchemy import text
    from datetime import datetime
    from collections import OrderedDict

    session = _helpers.get_session()
    try:
        rows = session.execute(
            text(
                """
                SELECT
                    et.direction,
                    et.email_type,
                    et.subject,
                    et.recipient_email,
                    et.user_email,
                    et.sender_email,
                    et.gmail_thread_id,
                    et.gmail_message_id,
                    et.gmail_draft_id,
                    et.draft_url,
                    et.sent_at,
                    et.created_at,
                    et.supplier_id,
                    et.customer_id,
                    s.name AS supplier_name,
                    c.companyname AS customer_name
                FROM email_tracking et
                LEFT JOIN suppliers s ON s.id = et.supplier_id
                LEFT JOIN customers c ON c.id = et.customer_id
                WHERE et.rfq_id = :rfq_id OR et.rfq_id = :rfq_number OR et.rfq_token = :rfq_number
                ORDER BY COALESCE(et.sent_at, et.created_at) ASC, et.created_at ASC
                LIMIT 200
                """
            ),
            {"rfq_id": rfq_id, "rfq_number": rfq_number or ""},
        ).mappings().all()

        threads: OrderedDict = OrderedDict()
        local_tz = ZoneInfo(_helpers.config.TIMEZONE)
        for row in rows:
            event = dict(row)
            ts = event.get("sent_at") or event.get("created_at")
            if isinstance(ts, datetime):
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                event["display_time"] = ts.astimezone(local_tz).strftime("%Y-%m-%d %H:%M")
            elif isinstance(ts, str):
                event["display_time"] = ts[:16].replace("T", " ") if len(ts) >= 16 else ts
            else:
                event["display_time"] = "—"

            # Determine external party for this message
            event["external_party"] = event.get("recipient_email") or "Unknown"

            tid = event.get("gmail_thread_id") or f"_no_thread_{id(event)}"
            if tid not in threads:
                # Determine source group for this thread
                if event.get("supplier_id"):
                    source_type = "supplier"
                    source_id = str(event["supplier_id"])
                    source_name = event.get("supplier_name") or "Unknown Supplier"
                elif event.get("customer_id"):
                    source_type = "customer"
                    source_id = str(event["customer_id"])
                    source_name = event.get("customer_name") or "Unknown Customer"
                else:
                    source_type = "unknown"
                    source_id = "_unknown"
                    source_name = "Other / Unmatched"

                threads[tid] = {
                    "thread_id": tid,
                    "subject": event.get("subject") or "No subject",
                    "external_party": event["external_party"],
                    "first_time": event["display_time"],
                    "last_time": event["display_time"],
                    "message_count": 0,
                    "has_reply": False,
                    "messages": [],
                    "source_type": source_type,
                    "source_id": source_id,
                    "source_name": source_name,
                }
            thread = threads[tid]
            thread["messages"].append(event)
            thread["message_count"] += 1
            thread["last_time"] = event["display_time"]
            if event.get("direction") == "received":
                thread["has_reply"] = True

        # Group threads by source
        source_groups: OrderedDict = OrderedDict()
        for thread in reversed(list(threads.values())):
            key = f"{thread['source_type']}:{thread['source_id']}"
            if key not in source_groups:
                source_groups[key] = {
                    "source_type": thread["source_type"],
                    "source_id": thread["source_id"],
                    "source_name": thread["source_name"],
                    "threads": [],
                    "total_messages": 0,
                    "has_reply": False,
                }
            group = source_groups[key]
            group["threads"].append(thread)
            group["total_messages"] += thread["message_count"]
            if thread["has_reply"]:
                group["has_reply"] = True

        # Sort: suppliers first, then customers, then unknown
        type_order = {"supplier": 0, "customer": 1, "unknown": 2}
        return sorted(source_groups.values(), key=lambda g: (type_order.get(g["source_type"], 9), g["source_name"].lower()))
    except Exception:
        return []
    finally:
        session.close()


def _render_rfq_detail_partial_response(request: Request, user: dict, rfq: dict, default_tab: str = "items"):
    active_tab = _infer_rfq_tab_from_request(request, default=default_tab)
    response = templates.TemplateResponse(request, "partials/rfq_detail.html", _rfq_detail_context(rfq, user, active_tab))
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Fetch helper
# ---------------------------------------------------------------------------
async def _fetch_rfqs(q: str = "", page: int = 1, mine: str = "", user_email: str = "", status: str = "active"):
    """Fetch RFQs from SQL with optional text search and pagination.
    
    Args:
        status: 'active' (in_progress + awaiting_quotes), 'all' (no filter), or a specific status value.
    """
    from includes.tools.quote_tools import _list_rfqs_sync, _rfq_to_dict

    def _query():
        from includes.dashboard.models import RFQ
        session = _helpers.get_session()
        try:
            query = session.query(RFQ)
            if mine == "1" and user_email:
                query = query.filter(RFQ.assigned_to.ilike(user_email))
            if status == "active":
                query = query.filter(RFQ.status.in_(["in_progress", "awaiting_quotes"]))
            elif status and status != "all":
                query = query.filter(RFQ.status == status)
            query = query.order_by(RFQ.rfq_number.desc())
            return [_rfq_to_dict(r) for r in query.limit(1000).all()]
        finally:
            session.close()

    rfqs = await asyncio.to_thread(_query)

    if q:
        q_lower = q.lower()
        filtered = []
        for r in rfqs:
            searchable = " ".join(filter(None, [
                str(r.get("id", "")),
                r.get("customer", ""),
                r.get("reference", ""),
                r.get("netsuite_opportunity", ""),
                r.get("assigned_to", ""),
                r.get("status", ""),
            ])).lower()
            for item in r.get("items", []):
                searchable += " " + " ".join(filter(None, [
                    item.get("input_description", ""),
                    item.get("part_number", ""),
                    item.get("brand", ""),
                ])).lower()
            if q_lower in searchable:
                filtered.append(r)
        rfqs = filtered

    total = len(rfqs)
    total_pages = max(1, math.ceil(total / RFQ_PAGE_SIZE))
    page = max(1, min(page, total_pages))
    start = (page - 1) * RFQ_PAGE_SIZE
    rfqs_page = rfqs[start:start + RFQ_PAGE_SIZE]
    has_more = page < total_pages

    return rfqs_page, total, has_more, page + 1


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("/rfqs")
async def rfq_list(request: Request, user: dict = Depends(require_user),
                   q: str = "", page: int = 1, mine: str = "1", status: str = "active"):
    rfqs, total, has_more, next_page = await _fetch_rfqs(
        q, page, mine=mine, user_email=user.get("email", ""), status=status)

    ctx = {
        "rfqs": rfqs,
        "q": q,
        "page": page,
        "total": total,
        "has_more": has_more,
        "next_page": next_page,
        "active_nav": "rfqs",
        "mine": mine,
        "status": status,
    }
    return _render(request, "rfqs.html", "partials/rfq_list.html", ctx, user)


@router.post("/rfqs/new")
async def rfq_new(request: Request, user: dict = Depends(require_user)):
    """Create a blank draft RFQ and navigate to its detail view."""
    from includes.tools.quote_tools import _create_rfq_sync

    user_email = user.get("email", user.get("identifier", ""))
    rfq = await asyncio.to_thread(_create_rfq_sync, {"customer": ""}, user_email)

    if isinstance(rfq, dict) and "error" in rfq:
        return HTMLResponse(f"<p>{rfq['error']}</p>", status_code=400)

    _enrich_rfq_supplier_contacts(rfq)
    rfq_id = rfq["id"]

    response = templates.TemplateResponse(request, "partials/rfq_detail.html", {
        "user": user,
        "rfq": rfq,
        "rfq_thread_id": None,
        "active_tab": "items",
        "all_users": _get_all_user_emails(),
        "header_auto_edit": True,
    })
    response.headers["HX-Push-Url"] = f"/rfqs/{rfq_id}"
    return response


@router.delete("/partial/rfqs/{rfq_id}")
async def partial_rfq_discard(request: Request, rfq_id: str,
                              user: dict = Depends(require_user)):
    """Discard an empty draft RFQ (0 items, status=draft)."""
    from includes.dashboard.models import RFQ, RFQItem

    def _delete():
        session = _helpers.get_session()
        try:
            rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_id).first()
            if not rfq:
                return "not_found"
            if rfq.status != "draft":
                return "not_draft"
            item_count = session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).count()
            if item_count > 0:
                return "has_items"
            session.delete(rfq)
            session.commit()
            return "ok"
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    result = await asyncio.to_thread(_delete)
    if result == "ok":
        return HTMLResponse("")  # empty response removes the row via hx-swap="outerHTML"
    msg = {"not_found": "RFQ not found.", "not_draft": "Only draft RFQs can be discarded.", "has_items": "RFQ has items and cannot be discarded."}
    return HTMLResponse(f"<tr><td colspan='7' class='px-4 py-2 text-red-600 text-sm'>{msg.get(result, 'Error')}</td></tr>", status_code=400)


@router.get("/rfqs/{rfq_id}")
async def rfq_detail(request: Request, rfq_id: str,
                     user: dict = Depends(require_user)):
    from includes.tools.quote_tools import _get_rfq_dict_sync
    rfq = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
    if not rfq:
        return RedirectResponse("/rfqs")
    _enrich_rfq_supplier_contacts(rfq)

    ctx = {
        "rfq": rfq,
        "active_nav": "rfqs",
        "rfq_thread_id": _lookup_rfq_thread_id(rfq_id, user.get("email", "")),
        "active_tab": "items",
    }
    return _render(request, "rfq_detail.html", "partials/rfq_detail.html", ctx, user)


@router.get("/rfqs/{rfq_id}/{tab}")
async def rfq_detail_tab(request: Request, rfq_id: str, tab: str,
                         user: dict = Depends(require_user)):
    from includes.tools.quote_tools import _get_rfq_dict_sync
    rfq = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
    if not rfq:
        return RedirectResponse("/rfqs")
    _enrich_rfq_supplier_contacts(rfq)

    ctx = _rfq_detail_context(rfq, user, tab)
    ctx["active_nav"] = "rfqs"
    return _render(request, "rfq_detail.html", "partials/rfq_detail.html", ctx, user)


@router.get("/partial/rfqs")
async def partial_rfq_list(request: Request, user: dict = Depends(require_user),
                           q: str = "", page: int = 1, mine: str = "1", status: str = "active"):
    rfqs, total, has_more, next_page = await _fetch_rfqs(
        q, page, mine=mine, user_email=user.get("email", ""), status=status)

    return templates.TemplateResponse(request, "partials/rfq_list.html", {
        "user": user,
        "rfqs": rfqs,
        "q": q,
        "page": page,
        "total": total,
        "has_more": has_more,
        "next_page": next_page,
        "mine": mine,
        "status": status,
    })


@router.get("/partial/rfqs/rows")
async def partial_rfq_rows(request: Request, user: dict = Depends(require_user),
                           q: str = "", page: int = 1, mine: str = "1", status: str = "active"):
    """Return just the RFQ card rows + sentinel for infinite scroll."""
    rfqs, total, has_more, next_page = await _fetch_rfqs(
        q, page, mine=mine, user_email=user.get("email", ""), status=status)

    return templates.TemplateResponse(request, "partials/_rfq_rows.html", {
        "rfqs": rfqs,
        "q": q,
        "has_more": has_more,
        "next_page": next_page,
        "mine": mine,
        "status": status,
    })


@router.get("/partial/rfqs/price-history")
async def partial_rfq_price_history(
    request: Request,
    product_id: str = "",
    supplier_id: str = "",
    part_number: str = "",
    user: dict = Depends(require_user),
):
    """Return HTML fragment with last 5 transactions for a product+supplier pair."""
    def _fetch_history():
        import uuid
        from sqlalchemy import and_, desc
        from includes.dashboard.models import Transaction, Product as ProductModel

        try:
            pid = uuid.UUID(product_id)
        except (ValueError, TypeError):
            pid = None
        try:
            sid = uuid.UUID(supplier_id)
        except (ValueError, TypeError):
            return []

        session = _helpers.get_session()
        try:
            rows = (
                session.query(
                    Transaction.date,
                    Transaction.doc_type,
                    Transaction.doc_number,
                    Transaction.quantity,
                    Transaction.cost,
                    Transaction.price,
                )
                .filter(and_(
                    Transaction.product_id == pid,
                    Transaction.supplier_id == sid,
                ))
                .order_by(desc(Transaction.date))
                .limit(5)
                .all()
            )
            
            # Fallback: if no rows by product_id and part_number is provided,
            # look up the correct product_id from the part_number and retry
            if not rows and part_number:
                prod = session.query(ProductModel).filter(
                    ProductModel.part_number.ilike(part_number.strip())
                ).first()
                if prod and prod.id != pid:
                    rows = (
                        session.query(
                            Transaction.date,
                            Transaction.doc_type,
                            Transaction.doc_number,
                            Transaction.quantity,
                            Transaction.cost,
                            Transaction.price,
                        )
                        .filter(and_(
                            Transaction.product_id == prod.id,
                            Transaction.supplier_id == sid,
                        ))
                        .order_by(desc(Transaction.date))
                        .limit(5)
                        .all()
                    )
            
            return [
                {
                    "date": r.date.isoformat() if r.date else "—",
                    "doc_type": r.doc_type or "—",
                    "doc_number": r.doc_number or "—",
                    "quantity": r.quantity,
                    "cost": r.cost,
                    "price": r.price,
                }
                for r in rows
            ]
        finally:
            session.close()

    rows = await asyncio.to_thread(_fetch_history)
    if not rows:
        return HTMLResponse('<span class="text-gray-400">No transaction history found.</span>')

    html_parts = [
        '<table class="w-full text-xs">',
        '<thead><tr class="border-b border-gray-200 dark:border-gray-700 text-gray-500">',
        '<th class="text-left py-1 pr-2">Date</th>',
        '<th class="text-left py-1 pr-2">Type</th>',
        '<th class="text-left py-1 pr-2">Doc #</th>',
        '<th class="text-right py-1 pr-2">Qty</th>',
        '<th class="text-right py-1 pr-2">Cost</th>',
        '<th class="text-right py-1">Sale</th>',
        '</tr></thead><tbody>',
    ]
    for r in rows:
        doc_label = {"PurchaseOrder": "PO", "SalesOrder": "SO", "Quote": "Qt"}.get(r["doc_type"], r["doc_type"])
        cost_str = f'${r["cost"]:.2f}' if r["cost"] is not None else "—"
        price_str = f'${r["price"]:.2f}' if r["price"] is not None else "—"
        qty_str = f'{r["quantity"]:.0f}' if r["quantity"] is not None else "—"
        html_parts.append(
            f'<tr class="border-b border-gray-100 dark:border-gray-700/50 whitespace-nowrap">'
            f'<td class="py-1 pr-3 text-gray-600 dark:text-gray-400">{r["date"]}</td>'
            f'<td class="py-1 pr-3"><span class="px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-700 text-[10px]">{doc_label}</span></td>'
            f'<td class="py-1 pr-3 font-mono text-gray-600 dark:text-gray-400">{r["doc_number"]}</td>'
            f'<td class="py-1 pr-3 text-right">{qty_str}</td>'
            f'<td class="py-1 pr-3 text-right font-medium">{cost_str}</td>'
            f'<td class="py-1 text-right font-medium">{price_str}</td>'
            f'</tr>'
        )
    html_parts.append('</tbody></table>')
    return HTMLResponse("".join(html_parts))


@router.get("/partial/rfqs/{rfq_id}")
async def partial_rfq_detail(request: Request, rfq_id: str,
                             user: dict = Depends(require_user)):
    from includes.tools.quote_tools import _get_rfq_dict_sync
    rfq = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
    if not rfq:
        return HTMLResponse("<p>RFQ not found.</p>")
    _enrich_rfq_supplier_contacts(rfq)

    return _render_rfq_detail_partial_response(request, user, rfq, default_tab="items")


@router.post("/partial/rfqs/{rfq_id}/update")
async def partial_rfq_update(request: Request, rfq_id: str,
                             user: dict = Depends(require_user)):
    """Update RFQ header properties (customer, netsuite, hubspot, notes)."""
    form = await request.form()
    data = {}
    updatable = ["customer", "customer_id", "opportunity_id", "assigned_to", "notes", "netsuite_opportunity", "hubspot_deal"]
    for key in updatable:
        val = form.get(key)
        if val is not None:
            stripped = val.strip()
            if key == "customer" and not stripped:
                continue
            if key in ("customer_id", "opportunity_id") and not stripped:
                data[key] = ""
                continue
            data[key] = stripped if stripped else None

    if data:
        from includes.tools.quote_tools import _update_rfq_sync
        user_ident = user.get("identifier", "dashboard")
        result = await asyncio.to_thread(_update_rfq_sync, rfq_id, data, user_ident)
        if isinstance(result, str):
            return HTMLResponse(f"<p>{result}</p>", status_code=404)
        rfq = result
    else:
        from includes.tools.quote_tools import _get_rfq_dict_sync
        rfq = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
        if not rfq:
            return HTMLResponse("<p>RFQ not found.</p>", status_code=404)

    _enrich_rfq_supplier_contacts(rfq)
    return _render_rfq_detail_partial_response(request, user, rfq)


@router.patch("/partial/rfqs/{rfq_id}/status")
async def partial_rfq_status(request: Request, rfq_id: str,
                             user: dict = Depends(require_user)):
    """Update just the RFQ status via the dropdown."""
    form = await request.form()
    new_status = form.get("status", "")

    from includes.tools.rfq_crud import _update_status_sync
    user_ident = user.get("identifier", "dashboard")
    result = await asyncio.to_thread(_update_status_sync, rfq_id, {"status": new_status}, user_ident)
    if isinstance(result, str):
        return HTMLResponse(f"<p>{result}</p>", status_code=400)

    rfq = result
    _enrich_rfq_supplier_contacts(rfq)
    return _render_rfq_detail_partial_response(request, user, rfq)


@router.post("/partial/rfqs/{rfq_id}/update-item")
async def partial_rfq_update_item(request: Request, rfq_id: str,
                                  user: dict = Depends(require_user)):
    """Update a single RFQ line item."""
    form = await request.form()
    try:
        line_num = int(form.get("line", 0))
    except (TypeError, ValueError):
        return HTMLResponse("<p>Invalid line number.</p>", status_code=400)

    data = {"line": line_num}
    updatable = ["input_description", "part_number", "brand", "quantity", "uom"]
    for key in updatable:
        val = form.get(key)
        if val is not None:
            val = val.strip()
            if key == "quantity":
                try:
                    val = int(val) if val else None
                except ValueError:
                    val = None
            data[key] = val if val else None

    from includes.tools.quote_tools import _update_item_sync
    user_ident = user.get("identifier", "dashboard")
    result = await asyncio.to_thread(_update_item_sync, rfq_id, data, user_ident)
    if isinstance(result, str):
        return HTMLResponse(f"<p>{result}</p>", status_code=404)
    rfq = result
    _enrich_rfq_supplier_contacts(rfq)
    return _render_rfq_detail_partial_response(request, user, rfq)


@router.delete("/partial/rfqs/{rfq_id}/delete-item/{line}")
async def partial_rfq_delete_item(request: Request, rfq_id: str, line: int,
                                  user: dict = Depends(require_user)):
    """Delete a single RFQ line item and renumber remaining items."""
    from includes.tools.rfq_crud import _delete_item_sync
    user_ident = user.get("identifier", "dashboard")
    result = await asyncio.to_thread(_delete_item_sync, rfq_id, line, user_ident)
    if isinstance(result, str):
        return HTMLResponse(f"<p>{result}</p>", status_code=400)
    rfq = result
    _enrich_rfq_supplier_contacts(rfq)
    return _render_rfq_detail_partial_response(request, user, rfq)


@router.post("/partial/rfqs/{rfq_id}/add-item")
async def partial_rfq_add_item(request: Request, rfq_id: str,
                               user: dict = Depends(require_user)):
    """Add a new line item to the RFQ."""
    form = await request.form()

    qty = form.get("quantity", "").strip()
    try:
        qty = int(qty) if qty else None
    except ValueError:
        qty = None

    def _add_item():
        from includes.dashboard.models import RFQ, RFQItem
        from includes.tools.quote_tools import _rfq_to_dict, _now_dt
        from sqlalchemy import func as sa_func
        session = _helpers.get_session()
        try:
            rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_id).first()
            if not rfq:
                return None
            max_line = session.query(sa_func.max(RFQItem.line)).filter(
                RFQItem.rfq_id == rfq.id
            ).scalar() or 0
            next_line = max_line + 1

            new_item = RFQItem(
                rfq_id=rfq.id,
                line=next_line,
                input_description=(form.get("input_description") or "").strip(),
                input_code="",
                part_number=(form.get("part_number") or "").strip() or None,
                brand=(form.get("brand") or "").strip() or None,
                quantity=qty,
                uom=(form.get("uom") or "").strip() or "ea",
                match="unmatched",
                suppliers=[],
            )
            session.add(new_item)

            from datetime import datetime, timezone
            history = list(rfq.history or [])
            desc = new_item.input_description or new_item.part_number or "new item"
            history.append({
                "date": datetime.now(timezone.utc).isoformat(),
                "user": user.get("identifier", "dashboard"),
                "action": f"Added line {next_line}: {desc}",
            })
            rfq.history = history
            rfq.updated_at = _now_dt()
            session.commit()
            session.refresh(rfq)
            return _rfq_to_dict(rfq)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    rfq = await asyncio.to_thread(_add_item)
    if not rfq:
        return HTMLResponse("<p>RFQ not found.</p>", status_code=404)
    _enrich_rfq_supplier_contacts(rfq)
    return _render_rfq_detail_partial_response(request, user, rfq)


@router.post("/partial/rfqs/{rfq_id}/clear-suppliers")
async def partial_rfq_clear_suppliers(request: Request, rfq_id: str,
                                     line: int = 0,
                                     user: dict = Depends(require_user)):
    """Remove all suppliers from a specific line item."""
    from includes.tools.quote_tools import _clear_suppliers_sync
    user_ident = user.get("identifier", "dashboard")
    result = await asyncio.to_thread(_clear_suppliers_sync, rfq_id, {"line": line}, user_ident)
    if isinstance(result, str):
        from includes.tools.quote_tools import _get_rfq_dict_sync
        rfq = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
        if not rfq:
            return HTMLResponse("<p>RFQ not found.</p>", status_code=404)
    else:
        rfq = result
    _enrich_rfq_supplier_contacts(rfq)
    return templates.TemplateResponse(request, "partials/rfq_detail.html", {
        "user": user, "rfq": rfq,
        "rfq_thread_id": _lookup_rfq_thread_id(rfq_id, user.get("email", "")),
    })


@router.post("/partial/rfqs/{rfq_id}/update-supplier-status")
async def partial_rfq_update_supplier_status(
    request: Request, rfq_id: str,
    user: dict = Depends(require_user),
):
    """Update a single supplier's status directly (shortlisted / selected / dropped)."""
    form = await request.form()
    try:
        line_num = int(form.get("line", 0))
    except (TypeError, ValueError):
        return HTMLResponse("<p>Invalid line number.</p>", status_code=400)

    supplier_name = (form.get("supplier_name") or "").strip()
    new_status = (form.get("status") or "").strip()
    if not supplier_name or new_status not in ("shortlisted", "dropped"):
        return HTMLResponse("<p>Invalid parameters.</p>", status_code=400)

    from includes.tools.quote_tools import _update_supplier_sync
    from starlette.responses import Response

    user_ident = user.get("identifier", "dashboard")
    data = {"line": line_num, "name": supplier_name, "status": new_status}
    result = await asyncio.to_thread(_update_supplier_sync, rfq_id, data, user_ident)
    if isinstance(result, str) and "not found" in result.lower():
        return HTMLResponse(f"<p>{result}</p>", status_code=404)

    return Response(status_code=204)


@router.post("/partial/rfqs/{rfq_id}/shortlist-all")
async def partial_rfq_shortlist_all(
    request: Request, rfq_id: str,
    user: dict = Depends(require_user),
):
    """Shortlist all non-dropped suppliers across all line items."""
    from includes.dashboard.models import RFQ, RFQItem
    from sqlalchemy.orm.attributes import flag_modified

    session = _helpers.get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_id).first()
        if not rfq:
            return HTMLResponse("<p>RFQ not found.</p>", status_code=404)

        items = session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).all()
        count = 0
        for item in items:
            suppliers = list(item.suppliers or [])
            changed = False
            for sup in suppliers:
                if isinstance(sup, dict) and sup.get("status") not in ("dropped", "shortlisted"):
                    sup["status"] = "shortlisted"
                    changed = True
                    count += 1
            if changed:
                item.suppliers = suppliers
                flag_modified(item, "suppliers")
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    from starlette.responses import Response
    return Response(status_code=204)


@router.post("/partial/rfqs/{rfq_id}/drop-supplier-all")
async def partial_rfq_drop_supplier_all(
    request: Request, rfq_id: str,
    user: dict = Depends(require_user),
):
    """Drop a supplier from all line items on the RFQ."""
    from includes.dashboard.models import RFQ, RFQItem
    from sqlalchemy.orm.attributes import flag_modified

    form = await request.form()
    supplier_name = (form.get("supplier_name") or "").strip()
    if not supplier_name:
        return HTMLResponse("<p>Missing supplier name.</p>", status_code=400)

    session = _helpers.get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_id).first()
        if not rfq:
            return HTMLResponse("<p>RFQ not found.</p>", status_code=404)

        items = session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).all()
        name_lower = supplier_name.lower()
        for item in items:
            suppliers = list(item.suppliers or [])
            changed = False
            for sup in suppliers:
                if isinstance(sup, dict) and (sup.get("name") or "").lower() == name_lower:
                    if sup.get("status") != "dropped":
                        sup["status"] = "dropped"
                        changed = True
            if changed:
                item.suppliers = suppliers
                flag_modified(item, "suppliers")
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    from starlette.responses import Response
    return Response(status_code=204)


@router.post("/partial/rfqs/{rfq_id}/shortlist-supplier-all-items")
async def partial_rfq_shortlist_supplier_all_items(
    request: Request, rfq_id: str,
    user: dict = Depends(require_user),
):
    """Shortlist a supplier on all line items where they already appear."""
    from includes.dashboard.models import RFQ, RFQItem
    from sqlalchemy.orm.attributes import flag_modified

    form = await request.form()
    supplier_name = (form.get("supplier_name") or "").strip()
    if not supplier_name:
        return HTMLResponse("<p>Missing supplier name.</p>", status_code=400)

    session = _helpers.get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_id).first()
        if not rfq:
            return HTMLResponse("<p>RFQ not found.</p>", status_code=404)

        items = session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).all()
        name_lower = supplier_name.lower()
        count = 0
        for item in items:
            suppliers = list(item.suppliers or [])
            changed = False
            for sup in suppliers:
                if isinstance(sup, dict) and (sup.get("name") or "").lower() == name_lower:
                    if sup.get("status") != "shortlisted":
                        sup["status"] = "shortlisted"
                        changed = True
                        count += 1
            if changed:
                item.suppliers = suppliers
                flag_modified(item, "suppliers")
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    from starlette.responses import Response
    return Response(status_code=204)


@router.post("/partial/rfqs/{rfq_id}/copy-supplier-to-all")
async def partial_rfq_copy_supplier_to_all(
    request: Request, rfq_id: str,
    user: dict = Depends(require_user),
):
    """Copy a supplier from one line item to all other items (skip duplicates)."""
    from includes.dashboard.models import RFQ, RFQItem
    from sqlalchemy.orm.attributes import flag_modified

    form = await request.form()
    try:
        source_line = int(form.get("line", 0))
    except (TypeError, ValueError):
        return HTMLResponse("<p>Invalid line number.</p>", status_code=400)

    supplier_name = (form.get("supplier_name") or "").strip()
    if not supplier_name:
        return HTMLResponse("<p>Missing supplier name.</p>", status_code=400)

    session = _helpers.get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_id).first()
        if not rfq:
            return HTMLResponse("<p>RFQ not found.</p>", status_code=404)

        items = session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).all()

        # Find the source supplier dict
        source_sup = None
        name_lower = supplier_name.lower()
        for item in items:
            if item.line == source_line:
                for sup in (item.suppliers or []):
                    if isinstance(sup, dict) and (sup.get("name") or "").lower() == name_lower:
                        source_sup = sup
                        break
                break

        if not source_sup:
            return HTMLResponse("<p>Supplier not found on source item.</p>", status_code=404)

        # Copy to all other items (skip if already present)
        for item in items:
            if item.line == source_line:
                continue
            suppliers = list(item.suppliers or [])
            already_present = any(
                isinstance(s, dict) and (s.get("name") or "").lower() == name_lower
                for s in suppliers
            )
            if already_present:
                continue
            # Create a fresh copy without item-specific pricing
            new_sup = {
                "name": source_sup.get("name", ""),
                "status": "shortlisted",
                "supplier_id": source_sup.get("supplier_id"),
                "contacts": source_sup.get("contacts", []),
                "country": source_sup.get("country"),
                "currency": source_sup.get("currency"),
                "tier": source_sup.get("tier"),
                "category": source_sup.get("category"),
                "source": source_sup.get("source"),
                "is_new": source_sup.get("is_new", False),
                "price": None,
                "price_type": None,
                "lead_time": source_sup.get("lead_time"),
                "notes": None,
            }
            suppliers.append(new_sup)
            item.suppliers = suppliers
            flag_modified(item, "suppliers")

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    from starlette.responses import Response
    return Response(status_code=204)


@router.post("/partial/rfqs/{rfq_id}/copy-supplier-to-items")
async def partial_rfq_copy_supplier_to_items(
    request: Request, rfq_id: str,
    user: dict = Depends(require_user),
):
    """Copy a supplier to specific line items (skip duplicates)."""
    from includes.dashboard.models import RFQ, RFQItem
    from sqlalchemy.orm.attributes import flag_modified
    import json as _json

    form = await request.form()
    try:
        source_line = int(form.get("line", 0))
    except (TypeError, ValueError):
        return HTMLResponse("<p>Invalid line number.</p>", status_code=400)

    supplier_name = (form.get("supplier_name") or "").strip()
    if not supplier_name:
        return HTMLResponse("<p>Missing supplier name.</p>", status_code=400)

    # Target lines as JSON array or comma-separated
    target_lines_raw = form.get("target_lines", "")
    try:
        target_lines = _json.loads(target_lines_raw)
    except (ValueError, TypeError):
        target_lines = [int(x.strip()) for x in target_lines_raw.split(",") if x.strip()]

    if not target_lines:
        return HTMLResponse("<p>No target items specified.</p>", status_code=400)

    session = _helpers.get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_id).first()
        if not rfq:
            return HTMLResponse("<p>RFQ not found.</p>", status_code=404)

        items = session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).all()

        # Find the source supplier dict
        source_sup = None
        name_lower = supplier_name.lower()
        for item in items:
            if item.line == source_line:
                for sup in (item.suppliers or []):
                    if isinstance(sup, dict) and (sup.get("name") or "").lower() == name_lower:
                        source_sup = sup
                        break
                break

        if not source_sup:
            return HTMLResponse("<p>Supplier not found on source item.</p>", status_code=404)

        target_set = set(target_lines)
        for item in items:
            if item.line not in target_set or item.line == source_line:
                continue
            suppliers = list(item.suppliers or [])
            already_present = any(
                isinstance(s, dict) and (s.get("name") or "").lower() == name_lower
                for s in suppliers
            )
            if already_present:
                continue
            new_sup = {
                "name": source_sup.get("name", ""),
                "status": "shortlisted",
                "supplier_id": source_sup.get("supplier_id"),
                "contacts": source_sup.get("contacts", []),
                "country": source_sup.get("country"),
                "currency": source_sup.get("currency"),
                "tier": source_sup.get("tier"),
                "category": source_sup.get("category"),
                "source": source_sup.get("source"),
                "is_new": source_sup.get("is_new", False),
                "price": None,
                "price_type": None,
                "lead_time": source_sup.get("lead_time"),
                "notes": None,
            }
            suppliers.append(new_sup)
            item.suppliers = suppliers
            flag_modified(item, "suppliers")

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    from starlette.responses import Response
    return Response(status_code=204)


@router.get("/partial/rfqs/{rfq_id}/email-suppliers")
async def partial_rfq_email_suppliers(
    request: Request, rfq_id: str,
    user: dict = Depends(require_user),
):
    """Generate email templates for all active suppliers on the RFQ."""
    from includes.tools.quote_tools import _get_rfq_dict_sync

    rfq = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
    if not rfq:
        return HTMLResponse("<p>RFQ not found.</p>", status_code=404)
    _enrich_rfq_supplier_contacts(rfq)

    suppliers = _build_rfq_supplier_email_data(rfq)

    return templates.TemplateResponse(request, "partials/_rfq_email_suppliers.html", {
        "user": user,
        "rfq": rfq,
        "suppliers": suppliers,
    })


@router.get("/partial/rfqs/{rfq_id}/{tab}")
async def partial_rfq_detail_tab(request: Request, rfq_id: str, tab: str,
                                 user: dict = Depends(require_user)):
    """Load RFQ detail partial with a specific active tab."""
    from includes.tools.quote_tools import _get_rfq_dict_sync

    rfq = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
    if not rfq:
        return HTMLResponse("<p>RFQ not found.</p>")
    _enrich_rfq_supplier_contacts(rfq)

    return _render_rfq_detail_partial_response(request, user, rfq, default_tab=tab)


@router.get("/api/rfqs/email-content/{message_id}")
async def api_get_email_content(
    message_id: str,
    user: dict = Depends(require_user),
):
    """Fetch email body on demand for a message that has no cached content.

    Looks up the email_tracking row, fetches from Gmail if body_markdown is NULL,
    caches it, and returns the content.
    """
    import asyncio as _aio

    def _fetch_and_cache():
        session = _helpers.get_session()
        try:
            tracking = session.query(EmailTracking).filter(
                EmailTracking.gmail_message_id == message_id
            ).first()
            if not tracking:
                return {"status": "error", "message": "Message not found"}

            # If already cached, return it
            if tracking.body_markdown:
                return {
                    "status": "ok",
                    "body_markdown": tracking.body_markdown,
                    "body_html": tracking.body_html,
                    "attachments": tracking.attachments_json,
                    "sender_name": tracking.sender_name,
                }

            # Fetch from Gmail API
            from includes.gmail import get_gmail_client
            from scripts.sync_gmail_mailboxes import fetch_message_content

            try:
                service = get_gmail_client(tracking.user_email)
            except Exception as e:
                logger.warning(f"Cannot get Gmail client for {tracking.user_email}: {e}")
                return {"status": "error", "message": f"Gmail not configured for {tracking.user_email}"}

            content = fetch_message_content(service, message_id)
            if not content:
                return {"status": "error", "message": "Email no longer available in Gmail (may have been deleted)"}

            # Cache in DB
            tracking.body_markdown = content["body_markdown"]
            tracking.body_html = content["body_html"]
            tracking.attachments_json = content["attachments_json"]
            tracking.sender_name = content["sender_name"]
            tracking.all_recipients = content["all_recipients"]
            tracking.updated_at = datetime.now(timezone.utc)
            session.commit()

            return {
                "status": "ok",
                "body_markdown": content["body_markdown"],
                "body_html": content["body_html"],
                "attachments": content["attachments_json"],
                "sender_name": content["sender_name"],
            }
        except Exception as e:
            session.rollback()
            return {"status": "error", "message": str(e)}
        finally:
            session.close()

    result = await asyncio.to_thread(_fetch_and_cache)
    return JSONResponse(result)


@router.get("/api/email-body/{message_id}")
async def api_get_email_body_rendered(
    message_id: str,
    user: dict = Depends(require_user),
):
    """Return structured email content for client-side rendering.

    Used by both the RFQ communications tab and admin email logs modal.
    Returns { status, body, quoted, attachments[] }.
    Client renders markdown with marked.js — this endpoint handles fetch/cache
    and splitting into body vs quoted portions.
    """

    def _fetch():
        session = _helpers.get_session()
        try:
            tracking = session.query(EmailTracking).filter(
                EmailTracking.gmail_message_id == message_id
            ).first()
            if not tracking:
                return {"status": "error", "message": "Message not found"}

            # Fetch from Gmail if not cached
            if not tracking.body_markdown:
                try:
                    from includes.gmail import get_gmail_client
                    from scripts.sync_gmail_mailboxes import fetch_message_content
                    service = get_gmail_client(tracking.user_email)
                    content = fetch_message_content(service, message_id)
                    if not content:
                        return {"status": "error", "message": "Email no longer available in Gmail (may have been deleted)"}
                    tracking.body_markdown = content["body_markdown"]
                    tracking.body_html = content["body_html"]
                    tracking.attachments_json = content["attachments_json"]
                    tracking.sender_name = content["sender_name"]
                    tracking.all_recipients = content["all_recipients"]
                    tracking.updated_at = datetime.now(timezone.utc)
                    session.commit()
                except Exception as e:
                    session.rollback()
                    return {"status": "error", "message": f"Failed to fetch: {e}"}

            # Split body and quoted content
            body_md = tracking.body_markdown or ""
            # Replace Gmail user placeholder with actual user email
            if tracking.user_email:
                body_md = body_md.replace("%%GMAIL_USER%%", tracking.user_email)
            parts = body_md.split("<!-- quoted -->")
            main_body = parts[0].strip()
            quoted_body = parts[1].strip() if len(parts) > 1 else None

            return {
                "status": "ok",
                "body": main_body,
                "quoted": quoted_body,
                "attachments": tracking.attachments_json or [],
            }
        except Exception as e:
            session.rollback()
            return {"status": "error", "message": str(e)}
        finally:
            session.close()

    result = await asyncio.to_thread(_fetch)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Email Integration: Draft Creation
# ---------------------------------------------------------------------------

@router.post("/api/rfqs/{rfq_id}/send-email-draft")
async def api_create_email_draft(
    request: Request,
    rfq_id: str,
    user: dict = Depends(require_user),
):
    """Create a Gmail draft email for an RFQ and return the compose URL.
    
    Request body (JSON):
    {
        "recipient_email": "supplier@example.com",
        "recipient_name": "Supplier Name",  (optional)
        "subject": "RFQ-12345 - Quote Request",
        "body_html": "<p>Dear Supplier,</p>..."
    }
    
    Response:
    {
        "status": "ok" | "error",
        "draft_id": "...",  (on success)
        "compose_url": "https://mail.google.com/...",  (on success)
        "message": "Draft created successfully" | error message
    }
    """
    try:
        from includes.tools.quote_tools import _get_rfq_dict_sync
        from includes.gmail.draft_service import create_draft_email
        
        # Get RFQ
        rfq = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
        if not rfq:
            return JSONResponse(
                {"status": "error", "message": "RFQ not found"},
                status_code=404
            )
        
        # Parse request body
        body = await request.json()
        recipient_email = body.get("recipient_email", "").strip()
        recipient_name = body.get("recipient_name", "").strip()
        subject = body.get("subject", "").strip()
        body_html = body.get("body_html", "").strip()
        
        # Validate
        if not recipient_email or "@" not in recipient_email:
            return JSONResponse(
                {"status": "error", "message": "Invalid recipient email"},
                status_code=400
            )
        if not subject:
            return JSONResponse(
                {"status": "error", "message": "Subject is required"},
                status_code=400
            )
        if not body_html:
            return JSONResponse(
                {"status": "error", "message": "Email body is required"},
                status_code=400
            )
        
        # Get user email (impersonation target)
        user_email = user.get("email", user.get("identifier", ""))
        if not user_email:
            return JSONResponse(
                {"status": "error", "message": "User email not found"},
                status_code=400
            )
        
        # Create draft with Gmail API (runs in thread to avoid blocking)
        draft_result = await asyncio.to_thread(
            create_draft_email,
            user_email=user_email,
            recipient_email=recipient_email,
            subject=subject,
            body_html=body_html,
            rfq_id=rfq_id,
            email_type="rfq_outreach",  # Can be extended to support other types
            opportunity_id=rfq.get("netsuite_opportunity") or rfq.get("hubspot_deal")
        )
        
        if draft_result["status"] != "ok":
            return JSONResponse(
                {"status": "error", "message": draft_result.get("message", "Draft creation failed")},
                status_code=500
            )
        
        return JSONResponse({
            "status": "ok",
            "draft_id": draft_result["draft_id"],
            "thread_id": draft_result["thread_id"],
            "compose_url": draft_result["compose_url"],
            "message": "Draft created successfully. Opening Gmail compose..."
        })
    
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error creating email draft for RFQ {rfq_id}: {e}", exc_info=True)
        return JSONResponse(
            {"status": "error", "message": f"Internal error: {str(e)}"},
            status_code=500
        )


@router.post("/api/rfqs/{rfq_id}/send-email-direct")
async def api_send_email_direct(
    request: Request,
    rfq_id: str,
    user: dict = Depends(require_user),
):
    """Send an RFQ email directly via Gmail API (no Gmail UI handoff)."""
    try:
        from includes.tools.quote_tools import _get_rfq_dict_sync
        from includes.gmail.draft_service import send_email_direct

        rfq = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
        if not rfq:
            return JSONResponse({"status": "error", "message": "RFQ not found"}, status_code=404)

        body = await request.json()
        recipient_email = body.get("recipient_email", "").strip()
        recipient_name = body.get("recipient_name", "").strip()
        subject = body.get("subject", "").strip()
        body_html = body.get("body_html", "").strip()

        if not recipient_email or "@" not in recipient_email:
            return JSONResponse({"status": "error", "message": "Invalid recipient email"}, status_code=400)
        if not subject:
            return JSONResponse({"status": "error", "message": "Subject is required"}, status_code=400)
        if not body_html:
            return JSONResponse({"status": "error", "message": "Email body is required"}, status_code=400)

        user_email = user.get("email", user.get("identifier", ""))
        if not user_email:
            return JSONResponse({"status": "error", "message": "User email not found"}, status_code=400)

        send_result = await asyncio.to_thread(
            send_email_direct,
            user_email=user_email,
            recipient_email=recipient_email,
            subject=subject,
            body_html=body_html,
            rfq_id=rfq_id,
            email_type="rfq_outreach",
            opportunity_id=rfq.get("netsuite_opportunity") or rfq.get("hubspot_deal"),
        )

        if send_result["status"] != "ok":
            return JSONResponse({"status": "error", "message": send_result.get("message", "Send failed")}, status_code=500)

        return JSONResponse({
            "status": "ok",
            "message_id": send_result.get("message_id"),
            "thread_id": send_result.get("thread_id"),
            "message": "Email sent successfully."
        })

    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error sending direct email for RFQ {rfq_id}: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": f"Internal error: {str(e)}"}, status_code=500)


@router.patch("/api/rfqs/{rfq_id}/supplier-contact")
async def api_update_supplier_contact(
    request: Request,
    rfq_id: str,
    user: dict = Depends(require_user),
):
    """Update email and contact name for a supplier on an RFQ.

    Request body (JSON):
    {
        "supplier_id": "uuid-string",    # optional if name is provided
        "name": "Acme Corp",             # supplier name (to locate the supplier in RFQ items)
        "email": "new@example.com",
        "contact_name": "John Smith"
    }

    Updates the RFQ item's suppliers JSONB and upserts the Contact table.
    """
    try:
        body = await request.json()
        supplier_name = (body.get("name") or "").strip()
        new_email = (body.get("email") or "").strip()
        contact_name = (body.get("contact_name") or "").strip()
        supplier_id = body.get("supplier_id")

        if not supplier_name:
            return JSONResponse(
                {"status": "error", "message": "Supplier name is required"},
                status_code=400,
            )
        if not new_email or "@" not in new_email:
            return JSONResponse(
                {"status": "error", "message": "A valid email address is required"},
                status_code=400,
            )

        from includes.tools.quote_tools import _get_rfq_dict_sync
        from includes.dashboard.models import RFQ, Contact
        import uuid as _uuid

        rfq = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
        if not rfq:
            return JSONResponse(
                {"status": "error", "message": "RFQ not found"},
                status_code=404,
            )

        name_lower = supplier_name.lower()

        # Update in DB: find all RFQItems with this supplier name and patch contacts
        session = _helpers.get_session()
        try:
            db_rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_id).first()
            if not db_rfq:
                return JSONResponse(
                    {"status": "error", "message": "RFQ not found in database"},
                    status_code=404,
                )

            items_updated = 0
            for item in db_rfq.items:
                suppliers = list(item.suppliers or [])
                changed = False
                for sup in suppliers:
                    if not isinstance(sup, dict):
                        continue
                    sup_name = (sup.get("name") or "").strip().lower()
                    sup_sid = sup.get("supplier_id")
                    # Match by supplier_id if available, otherwise by name
                    if supplier_id and sup_sid and str(sup_sid) == str(supplier_id):
                        pass
                    elif sup_name != name_lower:
                        continue

                    contacts = sup.get("contacts") or []
                    if not isinstance(contacts, list):
                        contacts = []
                    updated = False
                    for c in contacts:
                        if isinstance(c, dict):
                            c["email"] = new_email
                            if contact_name:
                                c["name"] = contact_name
                            updated = True
                    if not updated:
                        # No contacts list existed — create one
                        contacts = [{"name": contact_name or supplier_name, "email": new_email}]
                    sup["contacts"] = contacts
                    changed = True

                if changed:
                    # Must reassign + flag_modified to trigger SQLAlchemy JSONB change detection
                    from sqlalchemy.orm.attributes import flag_modified
                    item.suppliers = suppliers
                    flag_modified(item, 'suppliers')
                    items_updated += 1

            if items_updated == 0:
                return JSONResponse(
                    {"status": "error", "message": f"Supplier '{supplier_name}' not found in RFQ items"},
                    status_code=404,
                )

            # Also upsert the Contact table if we have a supplier_id
            if supplier_id:
                try:
                    from sqlalchemy.orm.attributes import flag_modified
                    sid_uuid = _uuid.UUID(str(supplier_id))
                    # Find active contacts for this supplier (NOT by email —
                    # email may have changed, and matching by new_email would
                    # miss the old row and create a duplicate).
                    existing = session.query(Contact).filter(
                        Contact.supplier_id == sid_uuid,
                        Contact.isinactive == False,
                    ).all()
                    if existing:
                        # Update the first active contact; deactivate extras to
                        # prevent stale rows from being re-merged on refresh.
                        primary = existing[0]
                        primary.email = new_email
                        if contact_name:
                            primary.fullname = contact_name
                            primary.firstname = contact_name.split()[0] if contact_name else None
                        for stale in existing[1:]:
                            stale.isinactive = True
                    else:
                        new_contact = Contact(
                            supplier_id=sid_uuid,
                            label="Source",
                            fullname=contact_name or None,
                            firstname=contact_name.split()[0] if contact_name else None,
                            email=new_email,
                            isinactive=False,
                        )
                        session.add(new_contact)
                except (ValueError, TypeError):
                    pass  # supplier_id not a valid UUID, skip Contact table

            # Add history entry
            now_iso = datetime.now(timezone.utc).isoformat()
            history = list(db_rfq.history or [])
            history.append({
                "date": now_iso,
                "user": user.get("email", user.get("identifier", "")),
                "action": f"Updated contact for {supplier_name}: {new_email}"
            })
            db_rfq.history = history
            db_rfq.updated_at = datetime.now(timezone.utc)

            session.commit()

            return JSONResponse({
                "status": "ok",
                "message": f"Updated contact info for {supplier_name} ({items_updated} item{'s' if items_updated != 1 else ''})",
            })

        finally:
            session.close()

    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error updating supplier contact for RFQ {rfq_id}: {e}", exc_info=True)
        return JSONResponse(
            {"status": "error", "message": f"Internal error: {str(e)}"},
            status_code=500,
        )