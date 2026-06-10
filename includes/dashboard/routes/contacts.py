"""Dashboard routes for Contacts."""

import math

from fastapi import Request, Depends
from sqlalchemy import or_

from includes.dashboard.models import Contact, Supplier, Customer
from . import _helpers
from ._helpers import router, templates, require_user, _render, PAGE_SIZE


def _query_contacts(session, q: str, page: int):
    """Shared query logic for contact list routes."""
    query = (
        session.query(Contact, Supplier, Customer)
        .outerjoin(Supplier, Contact.supplier_id == Supplier.id)
        .outerjoin(Customer, Contact.customer_id == Customer.id)
        .filter(Contact.isinactive == False)
    )

    if q:
        query = query.filter(or_(
            Contact.fullname.ilike(f"%{q}%"),
            Contact.email.ilike(f"%{q}%"),
            Contact.phone.ilike(f"%{q}%"),
        ))

    total = query.count()
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(1, min(page, total_pages))

    rows = (
        query
        .order_by(Contact.fullname, Contact.email)
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )
    contacts = []
    for contact, supplier, customer in rows:
        contacts.append({
            "id": str(contact.id),
            "fullname": contact.fullname,
            "email": contact.email,
            "phone": contact.phone,
            "label": contact.label,
            "parent_type": "supplier" if contact.supplier_id else "customer",
            "parent_name": (supplier.name if supplier else None) or (customer.companyname or customer.fullname if customer else None),
            "parent_id": str(contact.supplier_id or contact.customer_id or ""),
        })

    return contacts, total, total_pages, page


@router.get("/contacts")
def contact_list(request: Request, user: dict = Depends(require_user),
                 q: str = "", page: int = 1):
    session = _helpers.get_session()
    try:
        contacts, total, total_pages, page = _query_contacts(session, q, page)
    finally:
        session.close()

    ctx = {
        "contacts": contacts,
        "q": q,
        "page": page,
        "total": total,
        "has_more": page < total_pages,
        "next_page": page + 1,
        "active_nav": "contacts",
    }
    return _render(request, "contacts.html", "partials/contact_list.html", ctx, user)


@router.get("/partial/contacts")
def partial_contact_list(request: Request, user: dict = Depends(require_user),
                         q: str = "", page: int = 1):
    session = _helpers.get_session()
    try:
        contacts, total, total_pages, page = _query_contacts(session, q, page)
    finally:
        session.close()

    return templates.TemplateResponse(request, "partials/contact_list.html", {
        "user": user,
        "contacts": contacts,
        "q": q,
        "page": page,
        "total": total,
        "has_more": page < total_pages,
        "next_page": page + 1,
        "active_nav": "contacts",
    })


@router.get("/partial/contacts/rows")
def partial_contact_rows(request: Request, user: dict = Depends(require_user),
                         q: str = "", page: int = 1):
    """Return just the <tr> rows + sentinel for infinite scroll."""
    session = _helpers.get_session()
    try:
        contacts, total, total_pages, page = _query_contacts(session, q, page)
    finally:
        session.close()

    return templates.TemplateResponse(request, "partials/_contact_rows.html", {
        "contacts": contacts,
        "q": q,
        "has_more": page < total_pages,
        "next_page": page + 1,
    })
