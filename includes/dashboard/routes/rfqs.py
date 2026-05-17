"""RFQ routes: list, detail, create, update items/suppliers, price history."""

import asyncio
import math

from fastapi import Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse

from includes.dashboard.models import Supplier, Transaction
from . import _helpers
from ._helpers import router, templates, require_user, _render
from .api import _lookup_rfq_thread_id

RFQ_PAGE_SIZE = 25


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

    def _contacts_need_enrichment(contacts: list) -> bool:
        if not contacts:
            return True
        return not any(c.get("email") for c in contacts if isinstance(c, dict))

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
                Supplier.id, Supplier.contacts, Supplier.supply_chain_position, Supplier.terms,
                Supplier.country, Supplier.currency, Supplier.source,
            ).filter(
                Supplier.id.in_(list(by_id.keys()))
            ).all()
            for row in rows:
                sid = str(row.id)
                if sid in by_id:
                    scp = row.supply_chain_position or {}
                    for sup in by_id[sid]:
                        if row.contacts and _contacts_need_enrichment(sup.get("contacts", [])):
                            merge_supplier_contacts(sup, row.contacts)
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
                    for sup in sup_list:
                        if matched.contacts and _contacts_need_enrichment(sup.get("contacts", [])):
                            merge_supplier_contacts(sup, matched.contacts)
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


# ---------------------------------------------------------------------------
# Fetch helper
# ---------------------------------------------------------------------------
async def _fetch_rfqs(q: str = "", page: int = 1, mine: str = "", user_email: str = ""):
    """Fetch RFQs from SQL with optional text search and pagination."""
    from includes.tools.quote_tools import _list_rfqs_sync, _rfq_to_dict

    def _query():
        from includes.dashboard.models import RFQ
        session = _helpers.get_session()
        try:
            query = session.query(RFQ)
            if mine == "1" and user_email:
                query = query.filter(RFQ.assigned_to.ilike(user_email))
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
                   q: str = "", page: int = 1, mine: str = "1"):
    rfqs, total, has_more, next_page = await _fetch_rfqs(
        q, page, mine=mine, user_email=user.get("email", ""))

    ctx = {
        "rfqs": rfqs,
        "q": q,
        "page": page,
        "total": total,
        "has_more": has_more,
        "next_page": next_page,
        "active_nav": "rfqs",
        "mine": mine,
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
    }
    return _render(request, "rfq_detail.html", "partials/rfq_detail.html", ctx, user)


@router.get("/partial/rfqs")
async def partial_rfq_list(request: Request, user: dict = Depends(require_user),
                           q: str = "", page: int = 1, mine: str = "1"):
    rfqs, total, has_more, next_page = await _fetch_rfqs(
        q, page, mine=mine, user_email=user.get("email", ""))

    return templates.TemplateResponse(request, "partials/rfq_list.html", {
        "user": user,
        "rfqs": rfqs,
        "q": q,
        "page": page,
        "total": total,
        "has_more": has_more,
        "next_page": next_page,
        "mine": mine,
    })


@router.get("/partial/rfqs/rows")
async def partial_rfq_rows(request: Request, user: dict = Depends(require_user),
                           q: str = "", page: int = 1, mine: str = "1"):
    """Return just the RFQ card rows + sentinel for infinite scroll."""
    rfqs, total, has_more, next_page = await _fetch_rfqs(
        q, page, mine=mine, user_email=user.get("email", ""))

    return templates.TemplateResponse(request, "partials/_rfq_rows.html", {
        "rfqs": rfqs,
        "q": q,
        "has_more": has_more,
        "next_page": next_page,
        "mine": mine,
    })


@router.get("/partial/rfqs/price-history")
async def partial_rfq_price_history(
    request: Request,
    product_id: str = "",
    supplier_id: str = "",
    user: dict = Depends(require_user),
):
    """Return HTML fragment with last 5 transactions for a product+supplier pair."""
    def _fetch_history():
        import uuid
        from sqlalchemy import and_, desc
        from includes.dashboard.models import Transaction

        try:
            pid = uuid.UUID(product_id)
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

    return templates.TemplateResponse(request, "partials/rfq_detail.html", {
        "user": user,
        "rfq": rfq,
        "rfq_thread_id": _lookup_rfq_thread_id(rfq_id, user.get("email", "")),
    })


@router.post("/partial/rfqs/{rfq_id}/update")
async def partial_rfq_update(request: Request, rfq_id: str,
                             user: dict = Depends(require_user)):
    """Update RFQ header properties (customer, netsuite, hubspot, notes)."""
    form = await request.form()
    data = {}
    updatable = ["customer", "reference", "notes", "netsuite_opportunity", "hubspot_deal"]
    for key in updatable:
        val = form.get(key)
        if val is not None:
            data[key] = val.strip() if val.strip() else None

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
    return templates.TemplateResponse(request, "partials/rfq_detail.html", {
        "user": user, "rfq": rfq,
        "rfq_thread_id": _lookup_rfq_thread_id(rfq_id, user.get("email", "")),
    })


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
    return templates.TemplateResponse(request, "partials/rfq_detail.html", {
        "user": user, "rfq": rfq,
        "rfq_thread_id": _lookup_rfq_thread_id(rfq_id, user.get("email", "")),
    })


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
    return templates.TemplateResponse(request, "partials/rfq_detail.html", {
        "user": user, "rfq": rfq,
        "rfq_thread_id": _lookup_rfq_thread_id(rfq_id, user.get("email", "")),
    })


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
                status="unidentified",
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
    return templates.TemplateResponse(request, "partials/rfq_detail.html", {
        "user": user, "rfq": rfq,
        "rfq_thread_id": _lookup_rfq_thread_id(rfq_id, user.get("email", "")),
    })


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
    if not supplier_name or new_status not in ("shortlisted", "selected", "dropped"):
        return HTMLResponse("<p>Invalid parameters.</p>", status_code=400)

    from includes.tools.quote_tools import _update_supplier_sync
    from starlette.responses import Response

    user_ident = user.get("identifier", "dashboard")
    data = {"line": line_num, "name": supplier_name, "status": new_status}
    result = await asyncio.to_thread(_update_supplier_sync, rfq_id, data, user_ident)
    if isinstance(result, str) and "not found" in result.lower():
        return HTMLResponse(f"<p>{result}</p>", status_code=404)

    return Response(status_code=204)
