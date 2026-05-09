"""
Dashboard routes for the FastAPI app.

Provides full-page and HTMX partial routes for Suppliers, Products,
and the home dashboard.
"""

import math
import logging
import os
import hashlib

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from includes.dashboard.database import get_session, update_supplier, add_supplier_comment
from config import config
from includes.dashboard.models import (
    Brand,
    Product,
    Transaction,
    Supplier,
    SupplierBrand,
)

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")

# Cache-busting hash for static assets (computed once at startup)
def _css_hash() -> str:
    css_path = os.path.join("public", "tailwind.min.css")
    try:
        with open(css_path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()[:8]
    except FileNotFoundError:
        return "dev"

templates.env.globals["css_version"] = _css_hash()

router = APIRouter()

PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------
def require_user(request: Request) -> dict:
    """Ensure a logged-in user; redirect to /login otherwise."""
    user = request.session.get("user")
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    # Attach computed role so templates/downstream code can use it
    user["role"] = (
        "Admin"
        if user.get("email", "").lower() in config.get_admin_emails()
        else "Staff"
    )
    return user


def require_role(*allowed_roles: str):
    """Dependency factory: restrict a route to users with one of the given roles.

    Usage:
        @router.get("/users")
        def user_list(user: dict = Depends(require_role("Admin"))):
            ...
    """
    def _guard(request: Request) -> dict:
        user = require_user(request)
        if user["role"] not in allowed_roles:
            from fastapi import HTTPException
            if _is_htmx(request):
                raise HTTPException(status_code=403)
            raise HTTPException(status_code=403)
        return user
    return Depends(_guard)


# Convenience alias for the most common guard
require_admin = require_role("Admin")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _render(request: Request, full_template: str, partial_template: str,
            context: dict, user: dict):
    """Return a partial if HTMX, else the full page."""
    context["request"] = request
    context["user"] = user
    if _is_htmx(request):
        return templates.TemplateResponse(partial_template, context)
    return templates.TemplateResponse(full_template, context)


# ---------------------------------------------------------------------------
# Latest thread (for iframe resume on page reload)
# ---------------------------------------------------------------------------
@router.get("/api/latest-thread")
def latest_thread(user: dict = Depends(require_user)):
    """Return the user's most recently active Chainlit thread ID."""
    session = get_session()
    try:
        row = session.execute(
            text("""
                SELECT t."id"
                FROM threads t
                LEFT JOIN steps s ON t."id" = s."threadId"
                WHERE t."userIdentifier" = :email
                GROUP BY t."id"
                ORDER BY COALESCE(MAX(s."createdAt"), t."createdAt") DESC
                LIMIT 1
            """),
            {"email": user["email"]},
        ).fetchone()
    finally:
        session.close()
    return JSONResponse({"thread_id": row[0] if row else None})


# ---------------------------------------------------------------------------
# Dashboard home
# ---------------------------------------------------------------------------
@router.get("/")
async def dashboard_home(request: Request, user: dict = Depends(require_user)):
    session = get_session()
    try:
        stats = {
            "suppliers": session.query(func.count(Supplier.id)).scalar(),
            "products": session.query(func.count(Product.id)).scalar(),
            "purchases": session.query(func.count(Transaction.id)).scalar(),
        }
    finally:
        session.close()

    # RFQ count from SQL
    from includes.dashboard.models import RFQ as RFQModel
    rfq_session = get_session()
    try:
        stats["rfqs"] = rfq_session.query(func.count(RFQModel.id)).scalar()
    finally:
        rfq_session.close()

    return templates.TemplateResponse("home.html", {
        "request": request,
        "user": user,
        "stats": stats,
        "active_nav": "home",
    })


# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------
@router.get("/suppliers")
def supplier_list(request: Request, user: dict = Depends(require_user),
                  q: str = "", page: int = 1):
    session = get_session()
    try:
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
    session = get_session()
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
        "scp_options": config.get_supply_chain_options(),
        "active_nav": "suppliers",
    }
    return _render(request, "supplier_detail.html", "partials/supplier_detail.html", ctx, user)


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------
@router.get("/products")
def product_list(request: Request, user: dict = Depends(require_user),
                 q: str = "", page: int = 1):
    session = get_session()
    try:
        query = session.query(Product)

        if q:
            query = query.filter(
                Product.part_number.ilike(f"%{q}%")
                | Product.brand.ilike(f"%{q}%")
                | Product.description.ilike(f"%{q}%")
            )

        total = query.count()
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        products = (
            query
            .order_by(Product.brand, Product.part_number)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )
    finally:
        session.close()

    ctx = {
        "products": products,
        "q": q,
        "page": page,
        "total": total,
        "has_more": page < total_pages,
        "next_page": page + 1,
        "active_nav": "products",
    }
    return _render(request, "products.html", "partials/product_list.html", ctx, user)


@router.get("/products/{product_id}")
def product_detail_view(request: Request, product_id: str,
                        user: dict = Depends(require_user)):
    session = get_session()
    try:
        product = session.query(Product).filter(
            Product.id == product_id
        ).first()
        if not product:
            return RedirectResponse("/products")

        # Purchase history for this product
        purchases_raw = (
            session.query(Transaction, Supplier)
            .join(Supplier, Transaction.supplier_id == Supplier.id)
            .filter(Transaction.product_id == product.id)
            .order_by(Transaction.date.desc().nullslast())
            .limit(50)
            .all()
        )
        purchases = []
        for ps, sup in purchases_raw:
            purchases.append({
                "doc_number": ps.doc_number,
                "date": str(ps.date) if ps.date else None,
                "supplier_id": str(sup.id),
                "supplier_name": sup.name,
                "quantity": ps.quantity,
                "price": ps.price,
            })
    finally:
        session.close()

    ctx = {
        "product": product,
        "purchases": purchases,
        "active_nav": "products",
    }
    return _render(request, "product_detail.html", "partials/product_detail.html", ctx, user)


# ---------------------------------------------------------------------------
# HTMX partial routes (same data, always return partial fragment)
# ---------------------------------------------------------------------------
@router.get("/partial/suppliers")
def partial_supplier_list(request: Request, user: dict = Depends(require_user),
                          q: str = "", page: int = 1):
    """Force partial response for HTMX navigation."""
    # Reuse the full route logic but override _is_htmx
    request._headers = request.headers  # keep original
    # Simpler: just call the list function with an htmx-like request
    session = get_session()
    try:
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

    return templates.TemplateResponse("partials/supplier_list.html", {
        "request": request,
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
    session = get_session()
    try:
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

    return templates.TemplateResponse("partials/_supplier_rows.html", {
        "request": request,
        "suppliers": suppliers,
        "q": q,
        "has_more": page < total_pages,
        "next_page": page + 1,
    })


@router.get("/partial/suppliers/{supplier_id}")
def partial_supplier_detail(request: Request, supplier_id: str,
                            user: dict = Depends(require_user)):
    session = get_session()
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

    return templates.TemplateResponse("partials/supplier_detail.html", {
        "request": request,
        "user": user,
        "supplier": supplier,
        "contacts": contacts,
        "brands": brands,
        "purchases": purchases,
        "scp_options": config.get_supply_chain_options(),
    })


@router.post("/partial/suppliers/{supplier_id}/update")
def partial_supplier_update(request: Request, supplier_id: str,
                            user: dict = Depends(require_user)):
    import asyncio
    loop = asyncio.new_event_loop()
    form = loop.run_until_complete(request.form())
    loop.close()

    updates = {k: v for k, v in form.items() if k != "request"}

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
    session = get_session()
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

    return templates.TemplateResponse("partials/supplier_detail.html", {
        "request": request,
        "user": user,
        "supplier": supplier,
        "contacts": contacts,
        "brands": brands,
        "purchases": purchases,
        "scp_options": config.get_supply_chain_options(),
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

    return templates.TemplateResponse("partials/_supplier_contacts.html", {
        "request": request,
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
    session = get_session()
    try:
        supplier = session.query(Supplier).filter(Supplier.id == supplier_id).first()
    finally:
        session.close()

    return templates.TemplateResponse("partials/supplier_comments.html", {
        "request": request,
        "supplier": supplier,
        "comments": comments,
    })


@router.get("/partial/products")
def partial_product_list(request: Request, user: dict = Depends(require_user),
                         q: str = "", page: int = 1):
    session = get_session()
    try:
        query = session.query(Product)

        if q:
            query = query.filter(
                Product.part_number.ilike(f"%{q}%")
                | Product.brand.ilike(f"%{q}%")
                | Product.description.ilike(f"%{q}%")
            )

        total = query.count()
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        products = (
            query
            .order_by(Product.brand, Product.part_number)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )
    finally:
        session.close()

    return templates.TemplateResponse("partials/product_list.html", {
        "request": request,
        "user": user,
        "products": products,
        "q": q,
        "page": page,
        "total": total,
        "has_more": page < total_pages,
        "next_page": page + 1,
    })


@router.get("/partial/products/rows")
def partial_product_rows(request: Request, user: dict = Depends(require_user),
                         q: str = "", page: int = 1):
    """Return just the <tr> rows + sentinel for infinite scroll."""
    session = get_session()
    try:
        query = session.query(Product)

        if q:
            query = query.filter(
                Product.part_number.ilike(f"%{q}%")
                | Product.brand.ilike(f"%{q}%")
                | Product.description.ilike(f"%{q}%")
            )

        total = query.count()
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        products = (
            query
            .order_by(Product.brand, Product.part_number)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )
    finally:
        session.close()

    return templates.TemplateResponse("partials/_product_rows.html", {
        "request": request,
        "products": products,
        "q": q,
        "has_more": page < total_pages,
        "next_page": page + 1,
    })


@router.get("/partial/products/{product_id}")
def partial_product_detail(request: Request, product_id: str,
                           user: dict = Depends(require_user)):
    session = get_session()
    try:
        product = session.query(Product).filter(
            Product.id == product_id
        ).first()
        if not product:
            return HTMLResponse("<p>Product not found.</p>")

        purchases_raw = (
            session.query(Transaction, Supplier)
            .join(Supplier, Transaction.supplier_id == Supplier.id)
            .filter(Transaction.product_id == product.id)
            .order_by(Transaction.date.desc().nullslast())
            .limit(50)
            .all()
        )
        purchases = []
        for ps, sup in purchases_raw:
            purchases.append({
                "doc_number": ps.doc_number,
                "date": str(ps.date) if ps.date else None,
                "supplier_id": str(sup.id),
                "supplier_name": sup.name,
                "quantity": ps.quantity,
                "price": ps.price,
            })
    finally:
        session.close()

    return templates.TemplateResponse("partials/product_detail.html", {
        "request": request,
        "user": user,
        "product": product,
        "purchases": purchases,
    })


# ---------------------------------------------------------------------------
# RFQs (data lives in LangGraph async store, so routes are async)
# ---------------------------------------------------------------------------
def _get_store():
    """Lazily import the shared store instance from app.py."""
    from app import store
    return store


def _normalize_rfq_suppliers(rfq: dict) -> None:
    """Ensure every supplier dict in the RFQ has all expected keys with safe defaults.

    This guards against old/inconsistent data that may be missing keys the
    templates expect (price, status, lead_time, contacts, etc.).
    """
    _SUPPLIER_DEFAULTS = {
        "name": "",
        "price": None,
        "price_type": None,
        "lead_time": None,
        "status": "candidate",
        "notes": None,
        "supplier_id": None,
        "contacts": [],
    }
    for item in rfq.get("items", []):
        suppliers = item.get("suppliers", [])
        # Filter out non-dict entries (e.g. bare strings from old data)
        cleaned = []
        for sup in suppliers:
            if isinstance(sup, str):
                cleaned.append({**_SUPPLIER_DEFAULTS, "name": sup})
            elif isinstance(sup, dict):
                for key, default in _SUPPLIER_DEFAULTS.items():
                    sup.setdefault(key, default)
                # Fix contacts that got stored as a non-list
                if not isinstance(sup["contacts"], list):
                    sup["contacts"] = []
                cleaned.append(sup)
        item["suppliers"] = cleaned


def _enrich_rfq_supplier_contacts(rfq: dict) -> None:
    """Back-fill missing supplier contacts, terms, and tier from the DB.

    When a supplier was added to an RFQ, the contacts snapshot may have been
    empty or missing email/phone.  This looks up current DB contacts for
    suppliers that either have a supplier_id or can be matched by name,
    and merges in any missing email/phone fields.  It also pulls terms and
    supply_chain_position tier for display.
    """
    from includes.dashboard.database import (
        match_supplier_by_name,
        merge_supplier_contacts,
    )

    # First normalize all supplier dicts to ensure expected keys exist
    _normalize_rfq_suppliers(rfq)

    def _contacts_need_enrichment(contacts: list) -> bool:
        """True if contacts are empty or have no email."""
        if not contacts:
            return True
        return not any(c.get("email") for c in contacts if isinstance(c, dict))

    # Collect ALL suppliers grouped by id / name for enrichment
    by_id: dict[str, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}   # lowercased name -> supplier dicts
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

    session = get_session()
    try:
        from includes.dashboard.models import Supplier

        # Enrich by supplier_id
        if by_id:
            rows = session.query(
                Supplier.id, Supplier.contacts, Supplier.supply_chain_position, Supplier.terms,
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
                        if scp.get("tier"):
                            sup["tier"] = scp["tier"]
                        if row.terms:
                            sup["terms"] = row.terms

        # Enrich by name using shared matching
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
                        if scp.get("tier"):
                            sup["tier"] = scp["tier"]
                        if matched.terms:
                            sup["terms"] = matched.terms
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Purchases (Transaction table — purchase & quote history)
# ---------------------------------------------------------------------------
@router.get("/transactions")
def transaction_list(request: Request, user: dict = Depends(require_user),
                     q: str = "", page: int = 1):
    from includes.netsuite.constants import get_status_label
    session = get_session()
    try:
        query = (
            session.query(Transaction, Supplier.name, Product.part_number, Product.brand)
            .join(Supplier, Transaction.supplier_id == Supplier.id)
            .join(Product, Transaction.product_id == Product.id)
        )

        if q:
            query = query.filter(
                Transaction.doc_number.ilike(f"%{q}%")
                | Supplier.name.ilike(f"%{q}%")
                | Product.part_number.ilike(f"%{q}%")
                | Product.brand.ilike(f"%{q}%")
            )

        total = query.count()
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        rows = (
            query
            .order_by(Transaction.date.desc().nullslast(), Transaction.doc_number)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )

        transactions = []
        for ps, sup_name, part_number, brand in rows:
            cost_display = None
            if ps.cost is not None:
                currency = ps.cost_currency or "AUD"
                if currency == "AUD":
                    cost_display = f"${ps.cost:,.2f}"
                else:
                    cost_display = f"${ps.cost:,.2f} {currency}"
            price_display = f"${ps.price:,.2f}" if ps.price is not None else None
            transactions.append({
                "id": str(ps.id),
                "doc_number": ps.doc_number,
                "date": str(ps.date) if ps.date else None,
                "supplier_name": sup_name,
                "supplier_id": str(ps.supplier_id),
                "product_part": part_number,
                "product_brand": brand,
                "product_id": str(ps.product_id),
                "quantity": ps.quantity,
                "price": price_display,
                "cost": cost_display,
                "status": get_status_label(ps.doc_type or "", ps.status or "") if ps.status else None,
            })
    finally:
        session.close()

    ctx = {
        "transactions": transactions,
        "q": q,
        "page": page,
        "total": total,
        "has_more": page < total_pages,
        "next_page": page + 1,
        "active_nav": "transactions",
    }
    return _render(request, "transactions.html", "partials/transaction_list.html", ctx, user)


@router.get("/partial/transactions")
def partial_transaction_list(request: Request, user: dict = Depends(require_user),
                             q: str = "", page: int = 1):
    from includes.netsuite.constants import get_status_label
    session = get_session()
    try:
        query = (
            session.query(Transaction, Supplier.name, Product.part_number, Product.brand)
            .join(Supplier, Transaction.supplier_id == Supplier.id)
            .join(Product, Transaction.product_id == Product.id)
        )

        if q:
            query = query.filter(
                Transaction.doc_number.ilike(f"%{q}%")
                | Supplier.name.ilike(f"%{q}%")
                | Product.part_number.ilike(f"%{q}%")
                | Product.brand.ilike(f"%{q}%")
            )

        total = query.count()
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        rows = (
            query
            .order_by(Transaction.date.desc().nullslast(), Transaction.doc_number)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )

        transactions = []
        for ps, sup_name, part_number, brand in rows:
            cost_display = None
            if ps.cost is not None:
                currency = ps.cost_currency or "AUD"
                if currency == "AUD":
                    cost_display = f"${ps.cost:,.2f}"
                else:
                    cost_display = f"${ps.cost:,.2f} {currency}"
            price_display = f"${ps.price:,.2f}" if ps.price is not None else None
            transactions.append({
                "id": str(ps.id),
                "doc_number": ps.doc_number,
                "date": str(ps.date) if ps.date else None,
                "supplier_name": sup_name,
                "supplier_id": str(ps.supplier_id),
                "product_part": part_number,
                "product_brand": brand,
                "product_id": str(ps.product_id),
                "quantity": ps.quantity,
                "price": price_display,
                "cost": cost_display,
                "status": get_status_label(ps.doc_type or "", ps.status or "") if ps.status else None,
            })
    finally:
        session.close()

    return templates.TemplateResponse("partials/transaction_list.html", {
        "request": request,
        "user": user,
        "transactions": transactions,
        "q": q,
        "page": page,
        "total": total,
        "has_more": page < total_pages,
        "next_page": page + 1,
    })


@router.get("/partial/transactions/rows")
def partial_transaction_rows(request: Request, user: dict = Depends(require_user),
                             q: str = "", page: int = 1):
    """Return just the <tr> rows + sentinel for infinite scroll."""
    from includes.netsuite.constants import get_status_label
    session = get_session()
    try:
        query = (
            session.query(Transaction, Supplier.name, Product.part_number, Product.brand)
            .join(Supplier, Transaction.supplier_id == Supplier.id)
            .join(Product, Transaction.product_id == Product.id)
        )

        if q:
            query = query.filter(
                Transaction.doc_number.ilike(f"%{q}%")
                | Supplier.name.ilike(f"%{q}%")
                | Product.part_number.ilike(f"%{q}%")
                | Product.brand.ilike(f"%{q}%")
            )

        total = query.count()
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        rows = (
            query
            .order_by(Transaction.date.desc().nullslast(), Transaction.doc_number)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )

        transactions = []
        for ps, sup_name, part_number, brand in rows:
            cost_display = None
            if ps.cost is not None:
                currency = ps.cost_currency or "AUD"
                if currency == "AUD":
                    cost_display = f"${ps.cost:,.2f}"
                else:
                    cost_display = f"${ps.cost:,.2f} {currency}"
            price_display = f"${ps.price:,.2f}" if ps.price is not None else None
            transactions.append({
                "id": str(ps.id),
                "doc_number": ps.doc_number,
                "date": str(ps.date) if ps.date else None,
                "supplier_name": sup_name,
                "supplier_id": str(ps.supplier_id),
                "product_part": part_number,
                "product_brand": brand,
                "product_id": str(ps.product_id),
                "quantity": ps.quantity,
                "price": price_display,
                "cost": cost_display,
                "status": get_status_label(ps.doc_type or "", ps.status or "") if ps.status else None,
            })
    finally:
        session.close()

    return templates.TemplateResponse("partials/_transaction_rows.html", {
        "request": request,
        "transactions": transactions,
        "q": q,
        "has_more": page < total_pages,
        "next_page": page + 1,
    })


RFQ_PAGE_SIZE = 25


async def _fetch_rfqs(q: str = "", page: int = 1, mine: str = "", user_email: str = ""):
    """Fetch RFQs from SQL with optional text search and pagination.

    Returns (rfqs_page, total, has_more, next_page).
    """
    from includes.tools.quote_tools import _list_rfqs_sync, _rfq_to_dict

    def _query():
        from includes.dashboard.models import RFQ
        session = get_session()
        try:
            query = session.query(RFQ)
            if mine == "1" and user_email:
                query = query.filter(RFQ.assigned_to.ilike(user_email))
            query = query.order_by(RFQ.created_date.desc())
            return [_rfq_to_dict(r) for r in query.limit(1000).all()]
        finally:
            session.close()

    import asyncio
    rfqs = await asyncio.to_thread(_query)

    # Text filter
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


@router.get("/rfqs/{rfq_id}")
async def rfq_detail(request: Request, rfq_id: str,
                     user: dict = Depends(require_user)):
    import asyncio
    from includes.tools.quote_tools import _get_rfq_dict_sync
    rfq = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
    if not rfq:
        return RedirectResponse("/rfqs")
    _enrich_rfq_supplier_contacts(rfq)

    ctx = {
        "rfq": rfq,
        "active_nav": "rfqs",
    }
    return _render(request, "rfq_detail.html", "partials/rfq_detail.html", ctx, user)


@router.get("/partial/rfqs")
async def partial_rfq_list(request: Request, user: dict = Depends(require_user),
                           q: str = "", page: int = 1, mine: str = "1"):
    rfqs, total, has_more, next_page = await _fetch_rfqs(
        q, page, mine=mine, user_email=user.get("email", ""))

    return templates.TemplateResponse("partials/rfq_list.html", {
        "request": request,
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

    return templates.TemplateResponse("partials/_rfq_rows.html", {
        "request": request,
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
    import asyncio

    def _fetch_history():
        import uuid
        from sqlalchemy import and_, desc
        from includes.dashboard.models import Transaction
        from includes.dashboard.database import get_session

        try:
            pid = uuid.UUID(product_id)
            sid = uuid.UUID(supplier_id)
        except (ValueError, TypeError):
            return []

        session = get_session()
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
    import asyncio
    from includes.tools.quote_tools import _get_rfq_dict_sync
    rfq = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
    if not rfq:
        return HTMLResponse("<p>RFQ not found.</p>")
    _enrich_rfq_supplier_contacts(rfq)

    return templates.TemplateResponse("partials/rfq_detail.html", {
        "request": request,
        "user": user,
        "rfq": rfq,
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
        import asyncio
        from includes.tools.quote_tools import _update_rfq_sync
        user_ident = user.get("identifier", "dashboard")
        result = await asyncio.to_thread(_update_rfq_sync, rfq_id, data, user_ident)
        if isinstance(result, str):
            return HTMLResponse(f"<p>{result}</p>", status_code=404)
        rfq = result
    else:
        import asyncio
        from includes.tools.quote_tools import _get_rfq_dict_sync
        rfq = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
        if not rfq:
            return HTMLResponse("<p>RFQ not found.</p>", status_code=404)

    _enrich_rfq_supplier_contacts(rfq)
    return templates.TemplateResponse("partials/rfq_detail.html", {
        "request": request, "user": user, "rfq": rfq,
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

    import asyncio
    from includes.tools.quote_tools import _update_item_sync
    user_ident = user.get("identifier", "dashboard")
    result = await asyncio.to_thread(_update_item_sync, rfq_id, data, user_ident)
    if isinstance(result, str):
        return HTMLResponse(f"<p>{result}</p>", status_code=404)
    rfq = result
    _enrich_rfq_supplier_contacts(rfq)
    return templates.TemplateResponse("partials/rfq_detail.html", {
        "request": request, "user": user, "rfq": rfq,
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
        session = get_session()
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

    import asyncio
    rfq = await asyncio.to_thread(_add_item)
    if not rfq:
        return HTMLResponse("<p>RFQ not found.</p>", status_code=404)
    _enrich_rfq_supplier_contacts(rfq)
    return templates.TemplateResponse("partials/rfq_detail.html", {
        "request": request, "user": user, "rfq": rfq,
    })


@router.post("/partial/rfqs/{rfq_id}/clear-suppliers")
async def partial_rfq_clear_suppliers(request: Request, rfq_id: str,
                                     line: int = 0,
                                     user: dict = Depends(require_user)):
    """Remove all suppliers from a specific line item."""
    import asyncio
    from includes.tools.quote_tools import _clear_suppliers_sync
    user_ident = user.get("identifier", "dashboard")
    result = await asyncio.to_thread(_clear_suppliers_sync, rfq_id, {"line": line}, user_ident)
    if isinstance(result, str):
        # Not-found or nothing-to-clear — still re-render with current data
        from includes.tools.quote_tools import _get_rfq_dict_sync
        rfq = await asyncio.to_thread(_get_rfq_dict_sync, rfq_id)
        if not rfq:
            return HTMLResponse("<p>RFQ not found.</p>", status_code=404)
    else:
        rfq = result
    _enrich_rfq_supplier_contacts(rfq)
    return templates.TemplateResponse("partials/rfq_detail.html", {
        "request": request, "user": user, "rfq": rfq,
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

    import asyncio
    from includes.tools.quote_tools import _update_supplier_sync
    from starlette.responses import Response

    user_ident = user.get("identifier", "dashboard")
    data = {"line": line_num, "name": supplier_name, "status": new_status}
    result = await asyncio.to_thread(_update_supplier_sync, rfq_id, data, user_ident)
    if isinstance(result, str) and "not found" in result.lower():
        return HTMLResponse(f"<p>{result}</p>", status_code=404)

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
_USER_STATS_SQL = text("""
    SELECT
        u.id,
        u.identifier,
        COUNT(DISTINCT t.id)  AS thread_count,
        COUNT(s.id)           AS message_count,
        MAX(s."createdAt")    AS last_active
    FROM users u
    LEFT JOIN threads t ON t."userId" = u.id
    LEFT JOIN steps s   ON s."threadId" = t.id
    GROUP BY u.id, u.identifier
    ORDER BY last_active DESC NULLS LAST
""")


def _humanize_timestamp(iso_str: str | None) -> tuple[str, str]:
    """Convert an ISO timestamp to (human_label, exact_datetime).

    Returns e.g. ("Today 9:04 AM", "2026-04-26 09:04:32") or
    ("3 days ago", "2026-04-23 14:12:05").
    """
    if not iso_str:
        return ("—", "")
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    try:
        local_tz = ZoneInfo(config.TIMEZONE)
        # Chainlit stores ISO strings like "2026-04-26T09:04:32.123456+00:00"
        raw = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_local = dt.astimezone(local_tz)
        now = datetime.now(local_tz)
        exact = dt_local.strftime("%Y-%m-%d %H:%M:%S")
        time_fmt = dt_local.strftime("%-I:%M %p")

        delta = now - dt_local
        days = delta.days

        if days == 0:
            label = f"Today {time_fmt}"
        elif days == 1:
            label = f"Yesterday {time_fmt}"
        elif days < 7:
            label = f"{days} days ago"
        elif days < 14:
            label = "Last week"
        elif days < 30:
            weeks = days // 7
            label = f"{weeks} weeks ago"
        elif days < 365:
            months = days // 30
            label = f"{months} month{'s' if months != 1 else ''} ago"
        else:
            label = dt.strftime("%b %Y")

        return (label, exact)
    except (ValueError, TypeError):
        return (iso_str[:16].replace("T", " ") if len(iso_str) > 16 else iso_str, iso_str)


def _query_users(session):
    rows = session.execute(_USER_STATS_SQL).fetchall()
    users = []
    for row in rows:
        human, exact = _humanize_timestamp(row.last_active)
        users.append({
            "id": row.id,
            "identifier": row.identifier,
            "thread_count": row.thread_count,
            "message_count": row.message_count,
            "last_active": human,
            "last_active_exact": exact,
        })
    return users


async def _query_users_with_roles(session):
    """Query user stats and enrich with role + display name."""
    users = _query_users(session)
    admin_emails = config.get_admin_emails()
    store = _get_store()
    for u in users:
        email = u["identifier"]
        u["role"] = "Admin" if email.lower() in admin_emails else "Staff"
        u["display_name"] = None
        if store:
            profile = await store.aget(("users",), email)
            if profile and profile.value:
                u["display_name"] = (
                    profile.value.get("preferred_name")
                    or profile.value.get("full_name")
                    or profile.value.get("first_name")
                )
    return users


@router.get("/users")
async def user_list(request: Request, user: dict = require_admin):
    session = get_session()
    try:
        users = await _query_users_with_roles(session)
    finally:
        session.close()

    ctx = {"users": users, "active_nav": "users"}
    return _render(request, "users.html", "partials/user_list.html", ctx, user)


@router.get("/partial/users")
async def partial_user_list(request: Request, user: dict = require_admin):
    session = get_session()
    try:
        users = await _query_users_with_roles(session)
    finally:
        session.close()

    return templates.TemplateResponse("partials/user_list.html", {
        "request": request,
        "user": user,
        "users": users,
    })


# ---------------------------------------------------------------------------
# System Admin
# ---------------------------------------------------------------------------

def _job_to_dict(job) -> dict:
    """Convert a Job dataclass to a template-friendly dict."""
    from datetime import datetime, timezone
    if job.finished_at:
        delta = job.finished_at - job.started_at
        duration = str(delta).split(".")[0]
    elif job.status == "running":
        delta = datetime.now(timezone.utc) - job.started_at
        duration = str(delta).split(".")[0]
    else:
        duration = "—"
    return {
        "id": job.id,
        "script_name": job.script_name,
        "status": job.status,
        "started_at": job.started_at,
        "duration": duration,
        "last_output": "\n".join(list(job.output)[-10:]) if job.output else "",
    }


@router.get("/admin")
async def admin_page(request: Request, user: dict = require_admin):
    from config.scripts import list_scripts
    ctx = {
        "scripts": list_scripts(),
        "active_nav": "admin",
    }
    return _render(request, "admin.html", "partials/admin.html", ctx, user)


@router.get("/partial/admin")
async def partial_admin(request: Request, user: dict = require_admin):
    from config.scripts import list_scripts
    return templates.TemplateResponse("partials/admin.html", {
        "request": request,
        "user": user,
        "scripts": list_scripts(),
    })


@router.get("/partial/admin/jobs")
async def partial_admin_jobs(request: Request, user: dict = require_admin):
    from app import job_runner
    jobs = [_job_to_dict(j) for j in reversed(job_runner.list_jobs())]
    return templates.TemplateResponse("partials/admin_jobs.html", {
        "request": request,
        "jobs": jobs,
    })


@router.post("/admin/run-script")
async def admin_run_script(request: Request, user: dict = require_admin):
    from app import job_runner
    from config.scripts import validate_args

    form = await request.form()
    script_name = form.get("script_name", "")
    raw_args = form.get("args", "").strip()
    args = raw_args.split() if raw_args else []

    try:
        validate_args(script_name, args)
        await job_runner.run_script(script_name, args)
    except ValueError as e:
        logger.warning(f"Admin run-script error: {e}")

    jobs = [_job_to_dict(j) for j in reversed(job_runner.list_jobs())]
    return templates.TemplateResponse("partials/admin_jobs.html", {
        "request": request,
        "jobs": jobs,
    })


@router.post("/admin/cancel-job")
async def admin_cancel_job(request: Request, user: dict = require_admin):
    from app import job_runner

    form = await request.form()
    job_id = form.get("job_id", "")

    try:
        await job_runner.cancel(job_id)
    except ValueError as e:
        logger.warning(f"Admin cancel-job error: {e}")

    jobs = [_job_to_dict(j) for j in reversed(job_runner.list_jobs())]
    return templates.TemplateResponse("partials/admin_jobs.html", {
        "request": request,
        "jobs": jobs,
    })


@router.get("/partial/admin/netsuite-status")
async def partial_netsuite_status(request: Request, user: dict = require_admin):
    from includes.netsuite import NetSuiteClient
    client = NetSuiteClient()
    result = client.test_connection()
    return templates.TemplateResponse("partials/admin_netsuite_status.html", {
        "request": request,
        "netsuite": result,
    })
