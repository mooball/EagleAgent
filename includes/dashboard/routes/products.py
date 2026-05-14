"""Product list, detail, and HTMX partial routes."""

import math

from fastapi import Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse

from includes.dashboard.models import Product, Transaction, Supplier
from . import _helpers
from ._helpers import router, templates, require_user, _render, PAGE_SIZE


# ---------------------------------------------------------------------------
# Full-page routes
# ---------------------------------------------------------------------------
@router.get("/products")
def product_list(request: Request, user: dict = Depends(require_user),
                 q: str = "", page: int = 1):
    session = _helpers.get_session()
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
    session = _helpers.get_session()
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
                "cost": ps.cost,
                "currency": sup.currency,
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
# HTMX partial routes
# ---------------------------------------------------------------------------
@router.get("/partial/products")
def partial_product_list(request: Request, user: dict = Depends(require_user),
                         q: str = "", page: int = 1):
    session = _helpers.get_session()
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

    return templates.TemplateResponse(request, "partials/product_list.html", {
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
    session = _helpers.get_session()
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

    return templates.TemplateResponse(request, "partials/_product_rows.html", {
        "products": products,
        "q": q,
        "has_more": page < total_pages,
        "next_page": page + 1,
    })


@router.get("/partial/products/{product_id}")
def partial_product_detail(request: Request, product_id: str,
                           user: dict = Depends(require_user)):
    session = _helpers.get_session()
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
                "cost": ps.cost,
                "currency": sup.currency,
                "price": ps.price,
            })
    finally:
        session.close()

    return templates.TemplateResponse(request, "partials/product_detail.html", {
        "user": user,
        "product": product,
        "purchases": purchases,
    })
