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
from sqlalchemy import func
from sqlalchemy.orm import Session

from includes.database import get_session
from includes.db_models import (
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
# Auth dependency (same check as main.py — imported at wire-up time)
# ---------------------------------------------------------------------------
def require_user(request: Request) -> dict:
    user = request.session.get("user")
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


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
def dashboard_home(request: Request, user: dict = Depends(require_user)):
    session = get_session()
    try:
        stats = {
            "suppliers": session.query(func.count(Supplier.id)).scalar(),
            "products": session.query(func.count(Product.id)).scalar(),
        }
    finally:
        session.close()
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
        "total_pages": total_pages,
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
        "total_pages": total_pages,
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
        "total_pages": total_pages,
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
        "total_pages": total_pages,
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
