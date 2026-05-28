"""Supplier list, detail, and HTMX partial routes."""

import math

from fastapi import Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse

from includes.dashboard.database import update_supplier, add_supplier_comment
from includes.dashboard.models import Brand, Product, Transaction, Supplier, SupplierBrand
from . import _helpers
from ._helpers import router, templates, require_user, _render, PAGE_SIZE


# ---------------------------------------------------------------------------
# Full-page routes
# ---------------------------------------------------------------------------
@router.get("/suppliers")
def supplier_list(request: Request, user: dict = Depends(require_user),
                  q: str = "", page: int = 1):
    session = _helpers.get_session()
    try:
        from sqlalchemy import func
        query = session.query(
            Supplier,
            func.count(Transaction.id).label("purchase_count"),
        ).outerjoin(
            Transaction, Transaction.supplier_id == Supplier.id
        ).group_by(Supplier.id)

        if q:
            query = query.filter(Supplier.name.ilike(f"%{q}%"))

        total = query.count()
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        rows = (
            query
            .order_by(func.count(Transaction.id).desc(), Supplier.name)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )

        suppliers = []
        for s, pc in rows:
            scp = s.supply_chain_position or {}
            suppliers.append({
                "id": str(s.id),
                "name": s.name,
                "location": ", ".join(filter(None, [s.city, s.country])) or None,
                "supply_chain": ", ".join(filter(None, [scp.get("tier"), scp.get("category")])) or None,
                "purchase_count": pc,
            })
    finally:
        session.close()

    ctx = {
        "suppliers": suppliers,
        "q": q,
        "page": page,
        "total": total,
        "has_more": page < total_pages,
        "next_page": page + 1,
        "active_nav": "suppliers",
    }
    return _render(request, "suppliers.html", "partials/supplier_list.html", ctx, user)


@router.get("/suppliers/{supplier_id}")
def supplier_detail(request: Request, supplier_id: str,
                    user: dict = Depends(require_user)):
    session = _helpers.get_session()
    try:
        supplier = session.query(Supplier).filter(
            Supplier.id == supplier_id
        ).first()
        if not supplier:
            return RedirectResponse("/suppliers")

        # Contacts (JSONB field)
        contacts = []
        if supplier.contacts:
            for c in supplier.contacts:
                if isinstance(c, dict):
                    contacts.append(c)

        # Brands via SupplierBrand join
        brands = (
            session.query(Brand)
            .join(SupplierBrand, SupplierBrand.brand_id == Brand.id)
            .filter(SupplierBrand.supplier_id == supplier.id)
            .filter(Brand.duplicate_of.is_(None))
            .order_by(Brand.name)
            .all()
        )

        # Recent purchases
        purchases_raw = (
            session.query(Transaction, Product)
            .join(Product, Transaction.product_id == Product.id)
            .filter(Transaction.supplier_id == supplier.id)
            .order_by(Transaction.date.desc().nullslast())
            .limit(50)
            .all()
        )
        purchases = []
        for ps, prod in purchases_raw:
            purchases.append({
                "doc_number": ps.doc_number,
                "date": str(ps.date) if ps.date else None,
                "product_id": str(prod.id),
                "product_part": prod.part_number,
                "quantity": ps.quantity,
                "cost": ps.cost,
                "cost_currency": ps.cost_currency,
                "price": ps.price,
            })
    finally:
        session.close()

    ctx = {
        "supplier": supplier,
        "contacts": contacts,
        "brands": brands,
        "purchases": purchases,
        "scp_options": _helpers.config.get_supply_chain_options(),
        "active_nav": "suppliers",
    }
    return _render(request, "supplier_detail.html", "partials/supplier_detail.html", ctx, user)


# ---------------------------------------------------------------------------
# HTMX partial routes
# ---------------------------------------------------------------------------
@router.get("/partial/suppliers")
def partial_supplier_list(request: Request, user: dict = Depends(require_user),
                          q: str = "", page: int = 1):
    """Force partial response for HTMX navigation."""
    session = _helpers.get_session()
    try:
        from sqlalchemy import func
        query = session.query(
            Supplier,
            func.count(Transaction.id).label("purchase_count"),
        ).outerjoin(
            Transaction, Transaction.supplier_id == Supplier.id
        ).group_by(Supplier.id)

        if q:
            query = query.filter(Supplier.name.ilike(f"%{q}%"))

        total = query.count()
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        rows = (
            query
            .order_by(func.count(Transaction.id).desc(), Supplier.name)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )

        suppliers = []
        for s, pc in rows:
            scp = s.supply_chain_position or {}
            suppliers.append({
                "id": str(s.id),
                "name": s.name,
                "location": ", ".join(filter(None, [s.city, s.country])) or None,
                "supply_chain": ", ".join(filter(None, [scp.get("tier"), scp.get("category")])) or None,
                "purchase_count": pc,
            })
    finally:
        session.close()

    return templates.TemplateResponse(request, "partials/supplier_list.html", {
        "user": user,
        "suppliers": suppliers,
        "q": q,
        "page": page,
        "total": total,
        "has_more": page < total_pages,
        "next_page": page + 1,
    })


@router.get("/partial/suppliers/rows")
def partial_supplier_rows(request: Request, user: dict = Depends(require_user),
                          q: str = "", page: int = 1):
    """Return just the <tr> rows + sentinel for infinite scroll."""
    session = _helpers.get_session()
    try:
        from sqlalchemy import func
        query = session.query(
            Supplier,
            func.count(Transaction.id).label("purchase_count"),
        ).outerjoin(
            Transaction, Transaction.supplier_id == Supplier.id
        ).group_by(Supplier.id)

        if q:
            query = query.filter(Supplier.name.ilike(f"%{q}%"))

        total = query.count()
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        rows = (
            query
            .order_by(func.count(Transaction.id).desc(), Supplier.name)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )

        suppliers = []
        for s, pc in rows:
            scp = s.supply_chain_position or {}
            suppliers.append({
                "id": str(s.id),
                "name": s.name,
                "location": ", ".join(filter(None, [s.city, s.country])) or None,
                "supply_chain": ", ".join(filter(None, [scp.get("tier"), scp.get("category")])) or None,
                "purchase_count": pc,
            })
    finally:
        session.close()

    return templates.TemplateResponse(request, "partials/_supplier_rows.html", {
        "suppliers": suppliers,
        "q": q,
        "has_more": page < total_pages,
        "next_page": page + 1,
    })


@router.get("/partial/suppliers/{supplier_id}")
def partial_supplier_detail(request: Request, supplier_id: str,
                            user: dict = Depends(require_user)):
    session = _helpers.get_session()
    try:
        supplier = session.query(Supplier).filter(
            Supplier.id == supplier_id
        ).first()
        if not supplier:
            return HTMLResponse("<p>Supplier not found.</p>")

        contacts = []
        if supplier.contacts:
            for c in supplier.contacts:
                if isinstance(c, dict):
                    contacts.append(c)

        brands = (
            session.query(Brand)
            .join(SupplierBrand, SupplierBrand.brand_id == Brand.id)
            .filter(SupplierBrand.supplier_id == supplier.id)
            .filter(Brand.duplicate_of.is_(None))
            .order_by(Brand.name)
            .all()
        )

        purchases_raw = (
            session.query(Transaction, Product)
            .join(Product, Transaction.product_id == Product.id)
            .filter(Transaction.supplier_id == supplier.id)
            .order_by(Transaction.date.desc().nullslast())
            .limit(50)
            .all()
        )
        purchases = []
        for ps, prod in purchases_raw:
            purchases.append({
                "doc_number": ps.doc_number,
                "date": str(ps.date) if ps.date else None,
                "product_id": str(prod.id),
                "product_part": prod.part_number,
                "quantity": ps.quantity,
                "cost": ps.cost,
                "cost_currency": ps.cost_currency,
                "price": ps.price,
            })
    finally:
        session.close()

    return templates.TemplateResponse(request, "partials/supplier_detail.html", {
        "user": user,
        "supplier": supplier,
        "contacts": contacts,
        "brands": brands,
        "purchases": purchases,
        "scp_options": _helpers.config.get_supply_chain_options(),
    })


@router.post("/partial/suppliers/{supplier_id}/update")
def partial_supplier_update(request: Request, supplier_id: str,
                            user: dict = Depends(require_user)):
    import asyncio
    loop = asyncio.new_event_loop()
    form = loop.run_until_complete(request.form())
    loop.close()

    updates = {k: v for k, v in form.items() if k != "request"}

    # Parse alt_names and alt_domains from comma-separated text to JSON arrays
    for json_list_field in ("alt_names", "alt_domains"):
        raw = updates.pop(json_list_field, None)
        if raw is not None:
            items = [x.strip() for x in raw.split(",") if x.strip()]
            updates[json_list_field] = items if items else None

    # Build supply_chain_position JSONB from combined scp_position field ("tier|category")
    scp_position = updates.pop("scp_position", "")
    updates.pop("scp_category", None)  # clean up legacy field names
    updates.pop("scp_tier", None)
    if scp_position and "|" in scp_position:
        tier, category = scp_position.split("|", 1)
        updates["supply_chain_position"] = {
            "category": category or None,
            "tier": tier or None,
        }
    else:
        updates["supply_chain_position"] = None

    author = user.get("name") or user.get("email", "unknown")
    supplier = update_supplier(supplier_id, updates, f"user:{author}")
    if not supplier:
        return HTMLResponse("<p>Supplier not found.</p>")

    # Re-fetch full context for the detail partial
    session = _helpers.get_session()
    try:
        contacts = []
        if supplier.contacts:
            for c in supplier.contacts:
                if isinstance(c, dict):
                    contacts.append(c)

        brands = (
            session.query(Brand)
            .join(SupplierBrand, SupplierBrand.brand_id == Brand.id)
            .filter(SupplierBrand.supplier_id == supplier.id)
            .filter(Brand.duplicate_of.is_(None))
            .order_by(Brand.name)
            .all()
        )

        purchases_raw = (
            session.query(Transaction, Product)
            .join(Product, Transaction.product_id == Product.id)
            .filter(Transaction.supplier_id == supplier.id)
            .order_by(Transaction.date.desc().nullslast())
            .limit(50)
            .all()
        )
        purchases = []
        for ps, prod in purchases_raw:
            purchases.append({
                "doc_number": ps.doc_number,
                "date": str(ps.date) if ps.date else None,
                "product_id": str(prod.id),
                "product_part": prod.part_number,
                "quantity": ps.quantity,
                "cost": ps.cost,
                "cost_currency": ps.cost_currency,
                "price": ps.price,
            })
    finally:
        session.close()

    return templates.TemplateResponse(request, "partials/supplier_detail.html", {
        "user": user,
        "supplier": supplier,
        "contacts": contacts,
        "brands": brands,
        "purchases": purchases,
        "scp_options": _helpers.config.get_supply_chain_options(),
    })


@router.post("/partial/suppliers/{supplier_id}/update-contacts")
def partial_supplier_update_contacts(request: Request, supplier_id: str,
                                     user: dict = Depends(require_user)):
    """Update the contacts JSONB from the structured editor form."""
    import asyncio
    import json
    loop = asyncio.new_event_loop()
    form = loop.run_until_complete(request.form())
    loop.close()

    contacts_json = form.get("contacts_json", "[]")
    try:
        contacts = json.loads(contacts_json)
    except (json.JSONDecodeError, TypeError):
        return HTMLResponse("<p>Invalid contacts data.</p>", status_code=400)

    # Sanitize: keep only expected keys, strip whitespace
    cleaned = []
    for c in contacts:
        if not isinstance(c, dict):
            continue
        entry = {
            "name": (c.get("name") or "").strip() or None,
            "email": (c.get("email") or "").strip() or None,
            "phone": (c.get("phone") or "").strip() or None,
            "label": (c.get("label") or "").strip() or None,
        }
        # Skip completely empty rows
        if not any(entry.values()):
            continue
        cleaned.append(entry)

    author = user.get("name") or user.get("email", "unknown")
    supplier = update_supplier(supplier_id, {"contacts": cleaned}, f"user:{author}")
    if not supplier:
        return HTMLResponse("<p>Supplier not found.</p>")

    # Re-render just the contacts section
    contacts_list = []
    if supplier.contacts:
        for c in supplier.contacts:
            if isinstance(c, dict):
                contacts_list.append(c)

    return templates.TemplateResponse(request, "partials/_supplier_contacts.html", {
        "user": user,
        "supplier": supplier,
        "contacts": contacts_list,
    })


@router.post("/partial/suppliers/{supplier_id}/comments")
def partial_supplier_add_comment(request: Request, supplier_id: str,
                                 user: dict = Depends(require_user)):
    import asyncio
    loop = asyncio.new_event_loop()
    form = loop.run_until_complete(request.form())
    loop.close()

    comment_text = form.get("comment", "").strip()
    if not comment_text:
        return HTMLResponse("<p>Comment cannot be empty.</p>", status_code=400)

    author = user.get("name") or user.get("email", "unknown")
    comments = add_supplier_comment(supplier_id, author, comment_text)

    # Re-fetch supplier to render the comments section
    session = _helpers.get_session()
    try:
        supplier = session.query(Supplier).filter(Supplier.id == supplier_id).first()
    finally:
        session.close()

    return templates.TemplateResponse(request, "partials/supplier_comments.html", {
        "supplier": supplier,
        "comments": comments,
    })
