"""Dashboard routes for Customers."""

import math

from fastapi import Request, Depends
from sqlalchemy import or_

from includes.dashboard.models import Customer
from . import _helpers
from ._helpers import router, templates, require_user, _render, PAGE_SIZE


@router.get("/customers")
def customer_list(request: Request, user: dict = Depends(require_user),
                  q: str = "", page: int = 1):
    session = _helpers.get_session()
    try:
        query = session.query(Customer).filter(Customer.isinactive == False)

        if q:
            query = query.filter(or_(
                Customer.companyname.ilike(f"%{q}%"),
                Customer.fullname.ilike(f"%{q}%"),
                Customer.email.ilike(f"%{q}%"),
                Customer.entity_code.ilike(f"%{q}%"),
            ))

        total = query.count()
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        customers = (
            query
            .order_by(Customer.companyname, Customer.fullname)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )
    finally:
        session.close()

    ctx = {
        "customers": customers,
        "q": q,
        "page": page,
        "total": total,
        "has_more": page < total_pages,
        "next_page": page + 1,
        "active_nav": "customers",
    }
    return _render(request, "customers.html", "partials/customer_list.html", ctx, user)


@router.get("/partial/customers")
def partial_customer_list(request: Request, user: dict = Depends(require_user),
                          q: str = "", page: int = 1):
    session = _helpers.get_session()
    try:
        query = session.query(Customer).filter(Customer.isinactive == False)

        if q:
            query = query.filter(or_(
                Customer.companyname.ilike(f"%{q}%"),
                Customer.fullname.ilike(f"%{q}%"),
                Customer.email.ilike(f"%{q}%"),
                Customer.entity_code.ilike(f"%{q}%"),
            ))

        total = query.count()
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        customers = (
            query
            .order_by(Customer.companyname, Customer.fullname)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )
    finally:
        session.close()

    return templates.TemplateResponse(request, "partials/customer_list.html", {
        "user": user,
        "customers": customers,
        "q": q,
        "page": page,
        "total": total,
        "has_more": page < total_pages,
        "next_page": page + 1,
        "active_nav": "customers",
    })


@router.get("/partial/customers/rows")
def partial_customer_rows(request: Request, user: dict = Depends(require_user),
                          q: str = "", page: int = 1):
    """Return just the <tr> rows + sentinel for infinite scroll."""
    session = _helpers.get_session()
    try:
        query = session.query(Customer).filter(Customer.isinactive == False)

        if q:
            query = query.filter(or_(
                Customer.companyname.ilike(f"%{q}%"),
                Customer.fullname.ilike(f"%{q}%"),
                Customer.email.ilike(f"%{q}%"),
                Customer.entity_code.ilike(f"%{q}%"),
            ))

        total = query.count()
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        customers = (
            query
            .order_by(Customer.companyname, Customer.fullname)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )
    finally:
        session.close()

    return templates.TemplateResponse(request, "partials/_customer_rows.html", {
        "customers": customers,
        "q": q,
        "has_more": page < total_pages,
        "next_page": page + 1,
    })
