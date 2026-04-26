"""
Dashboard routes for the FastAPI app.

Provides full-page and HTMX partial routes for Suppliers, Products,
and the home dashboard.
"""

import math
import logging

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from includes.dashboard.database import get_session
from config import config
from includes.dashboard.models import (
    Brand,
    Product,
    ProductSupplier,
    Supplier,
    SupplierBrand,
)

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")

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
# Dashboard home
# ---------------------------------------------------------------------------
@router.get("/")
async def dashboard_home(request: Request, user: dict = Depends(require_user)):
    session = get_session()
    try:
        stats = {
            "suppliers": session.query(func.count(Supplier.id)).scalar(),
            "products": session.query(func.count(Product.id)).scalar(),
        }
    finally:
        session.close()

    # RFQ count from async store
    store = _get_store()
    if store:
        from includes.tools.quote_tools import NAMESPACE
        items = await store.asearch(NAMESPACE, limit=1000)
        stats["rfqs"] = len(items)
    else:
        stats["rfqs"] = 0

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
            func.count(ProductSupplier.id).label("purchase_count"),
        ).outerjoin(
            ProductSupplier, ProductSupplier.supplier_id == Supplier.id
        ).group_by(Supplier.id)

        if q:
            query = query.filter(Supplier.name.ilike(f"%{q}%"))

        total = query.count()
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        rows = (
            query
            .order_by(func.count(ProductSupplier.id).desc(), Supplier.name)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )

        suppliers = []
        for s, pc in rows:
            suppliers.append({
                "id": str(s.id),
                "name": s.name,
                "country": s.country,
                "city": s.city,
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
            session.query(ProductSupplier, Product)
            .join(Product, ProductSupplier.product_id == Product.id)
            .filter(ProductSupplier.supplier_id == supplier.id)
            .order_by(ProductSupplier.date.desc().nullslast())
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
                "price": ps.price,
            })
    finally:
        session.close()

    ctx = {
        "supplier": supplier,
        "contacts": contacts,
        "brands": brands,
        "purchases": purchases,
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
            session.query(ProductSupplier, Supplier)
            .join(Supplier, ProductSupplier.supplier_id == Supplier.id)
            .filter(ProductSupplier.product_id == product.id)
            .order_by(ProductSupplier.date.desc().nullslast())
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
            func.count(ProductSupplier.id).label("purchase_count"),
        ).outerjoin(
            ProductSupplier, ProductSupplier.supplier_id == Supplier.id
        ).group_by(Supplier.id)

        if q:
            query = query.filter(Supplier.name.ilike(f"%{q}%"))

        total = query.count()
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        rows = (
            query
            .order_by(func.count(ProductSupplier.id).desc(), Supplier.name)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )

        suppliers = []
        for s, pc in rows:
            suppliers.append({
                "id": str(s.id),
                "name": s.name,
                "country": s.country,
                "city": s.city,
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
            func.count(ProductSupplier.id).label("purchase_count"),
        ).outerjoin(
            ProductSupplier, ProductSupplier.supplier_id == Supplier.id
        ).group_by(Supplier.id)

        if q:
            query = query.filter(Supplier.name.ilike(f"%{q}%"))

        total = query.count()
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        rows = (
            query
            .order_by(func.count(ProductSupplier.id).desc(), Supplier.name)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )

        suppliers = []
        for s, pc in rows:
            suppliers.append({
                "id": str(s.id),
                "name": s.name,
                "country": s.country,
                "city": s.city,
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
            session.query(ProductSupplier, Product)
            .join(Product, ProductSupplier.product_id == Product.id)
            .filter(ProductSupplier.supplier_id == supplier.id)
            .order_by(ProductSupplier.date.desc().nullslast())
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
            session.query(ProductSupplier, Supplier)
            .join(Supplier, ProductSupplier.supplier_id == Supplier.id)
            .filter(ProductSupplier.product_id == product.id)
            .order_by(ProductSupplier.date.desc().nullslast())
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


@router.get("/rfqs")
async def rfq_list(request: Request, user: dict = require_admin):
    store = _get_store()
    rfqs = []
    if store:
        from includes.tools.quote_tools import NAMESPACE
        items = await store.asearch(NAMESPACE, limit=200)
        for item in items:
            rfqs.append(item.value)
        rfqs.sort(key=lambda r: r.get("created_date", ""), reverse=True)

    ctx = {
        "rfqs": rfqs,
        "active_nav": "rfqs",
    }
    return _render(request, "rfqs.html", "partials/rfq_list.html", ctx, user)


@router.get("/rfqs/{rfq_id}")
async def rfq_detail(request: Request, rfq_id: str,
                     user: dict = require_admin):
    store = _get_store()
    if not store:
        return RedirectResponse("/rfqs")
    from includes.tools.quote_tools import NAMESPACE
    item = await store.aget(NAMESPACE, rfq_id)
    if not item:
        return RedirectResponse("/rfqs")

    ctx = {
        "rfq": item.value,
        "active_nav": "rfqs",
    }
    return _render(request, "rfq_detail.html", "partials/rfq_detail.html", ctx, user)


@router.get("/partial/rfqs")
async def partial_rfq_list(request: Request, user: dict = require_admin):
    store = _get_store()
    rfqs = []
    if store:
        from includes.tools.quote_tools import NAMESPACE
        items = await store.asearch(NAMESPACE, limit=200)
        for item in items:
            rfqs.append(item.value)
        rfqs.sort(key=lambda r: r.get("created_date", ""), reverse=True)

    return templates.TemplateResponse("partials/rfq_list.html", {
        "request": request,
        "user": user,
        "rfqs": rfqs,
    })


@router.get("/partial/rfqs/{rfq_id}")
async def partial_rfq_detail(request: Request, rfq_id: str,
                             user: dict = require_admin):
    store = _get_store()
    if not store:
        return HTMLResponse("<p>Store not available.</p>")
    from includes.tools.quote_tools import NAMESPACE
    item = await store.aget(NAMESPACE, rfq_id)
    if not item:
        return HTMLResponse("<p>RFQ not found.</p>")

    return templates.TemplateResponse("partials/rfq_detail.html", {
        "request": request,
        "user": user,
        "rfq": item.value,
    })


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
