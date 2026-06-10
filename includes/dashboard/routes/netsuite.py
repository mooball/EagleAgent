"""Listing routes for NetSuite expanded tables: Customers, Contacts, Opportunities."""

import math

from fastapi import Request, Depends
from sqlalchemy import func, or_

from includes.dashboard.models import Customer, Contact, Opportunity, Product, Supplier, Transaction
from . import _helpers
from ._helpers import router, templates, require_user, _render, PAGE_SIZE


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------
@router.get("/contacts")
def contact_list(request: Request, user: dict = Depends(require_user),
                 q: str = "", page: int = 1):
    session = _helpers.get_session()
    try:
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
    finally:
        session.close()

    return templates.TemplateResponse(request, "partials/_contact_rows.html", {
        "contacts": contacts,
        "q": q,
        "has_more": page < total_pages,
        "next_page": page + 1,
    })


# ---------------------------------------------------------------------------
# Opportunities
# ---------------------------------------------------------------------------
@router.get("/opportunities")
def opportunity_list(request: Request, user: dict = Depends(require_user),
                     q: str = "", page: int = 1):
    session = _helpers.get_session()
    try:
        query = (
            session.query(Opportunity, Customer)
            .outerjoin(Customer, Opportunity.customer_id == Customer.id)
        )

        if q:
            query = query.filter(or_(
                Opportunity.title.ilike(f"%{q}%"),
                Opportunity.opportunity_number.ilike(f"%{q}%"),
                Customer.companyname.ilike(f"%{q}%"),
            ))

        total = query.count()
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        rows = (
            query
            .order_by(Opportunity.netsuite_last_modified.desc().nullslast())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )
        opportunities = []
        for opp, customer in rows:
            opportunities.append({
                "id": str(opp.id),
                "opportunity_number": opp.opportunity_number,
                "title": opp.title,
                "status": opp.status,
                "total": opp.total,
                "currency": opp.currency,
                "customer_name": (customer.companyname or customer.fullname) if customer else None,
                "last_modified": opp.netsuite_last_modified.strftime("%Y-%m-%d") if opp.netsuite_last_modified else None,
            })
    finally:
        session.close()

    ctx = {
        "opportunities": opportunities,
        "q": q,
        "page": page,
        "total": total,
        "has_more": page < total_pages,
        "next_page": page + 1,
        "active_nav": "opportunities",
    }
    return _render(request, "opportunities.html", "partials/opportunity_list.html", ctx, user)


@router.get("/partial/opportunities")
def partial_opportunity_list(request: Request, user: dict = Depends(require_user),
                             q: str = "", page: int = 1):
    session = _helpers.get_session()
    try:
        query = (
            session.query(Opportunity, Customer)
            .outerjoin(Customer, Opportunity.customer_id == Customer.id)
        )

        if q:
            query = query.filter(or_(
                Opportunity.title.ilike(f"%{q}%"),
                Opportunity.opportunity_number.ilike(f"%{q}%"),
                Customer.companyname.ilike(f"%{q}%"),
            ))

        total = query.count()
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        rows = (
            query
            .order_by(Opportunity.netsuite_last_modified.desc().nullslast())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )
        opportunities = []
        for opp, customer in rows:
            opportunities.append({
                "id": str(opp.id),
                "opportunity_number": opp.opportunity_number,
                "title": opp.title,
                "status": opp.status,
                "total": opp.total,
                "currency": opp.currency,
                "customer_name": (customer.companyname or customer.fullname) if customer else None,
                "last_modified": opp.netsuite_last_modified.strftime("%Y-%m-%d") if opp.netsuite_last_modified else None,
            })
    finally:
        session.close()

    return templates.TemplateResponse(request, "partials/opportunity_list.html", {
        "user": user,
        "opportunities": opportunities,
        "q": q,
        "page": page,
        "total": total,
        "has_more": page < total_pages,
        "next_page": page + 1,
        "active_nav": "opportunities",
    })


@router.get("/partial/opportunities/rows")
def partial_opportunity_rows(request: Request, user: dict = Depends(require_user),
                             q: str = "", page: int = 1):
    """Return just the <tr> rows + sentinel for infinite scroll."""
    session = _helpers.get_session()
    try:
        query = (
            session.query(Opportunity, Customer)
            .outerjoin(Customer, Opportunity.customer_id == Customer.id)
        )

        if q:
            query = query.filter(or_(
                Opportunity.title.ilike(f"%{q}%"),
                Opportunity.opportunity_number.ilike(f"%{q}%"),
                Customer.companyname.ilike(f"%{q}%"),
            ))

        total = query.count()
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        rows = (
            query
            .order_by(Opportunity.netsuite_last_modified.desc().nullslast())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )
        opportunities = []
        for opp, customer in rows:
            opportunities.append({
                "id": str(opp.id),
                "opportunity_number": opp.opportunity_number,
                "title": opp.title,
                "status": opp.status,
                "total": opp.total,
                "currency": opp.currency,
                "customer_name": (customer.companyname or customer.fullname) if customer else None,
                "last_modified": opp.netsuite_last_modified.strftime("%Y-%m-%d") if opp.netsuite_last_modified else None,
            })
    finally:
        session.close()

    return templates.TemplateResponse(request, "partials/_opportunity_rows.html", {
        "opportunities": opportunities,
        "q": q,
        "has_more": page < total_pages,
        "next_page": page + 1,
    })


# ---------------------------------------------------------------------------
# Opportunity Detail
# ---------------------------------------------------------------------------
@router.get("/opportunities/{opp_id}")
def opportunity_detail(request: Request, opp_id: str, user: dict = Depends(require_user)):
    session = _helpers.get_session()
    try:
        opp = session.query(Opportunity).filter(Opportunity.id == opp_id).first()
        if not opp:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Opportunity not found")

        customer = session.query(Customer).filter(Customer.id == opp.customer_id).first() if opp.customer_id else None

        # Load related transactions with product/supplier names
        from includes.netsuite.constants import get_status_label
        txn_rows = (
            session.query(Transaction, Product, Supplier)
            .outerjoin(Product, Transaction.product_id == Product.id)
            .outerjoin(Supplier, Transaction.supplier_id == Supplier.id)
            .filter(Transaction.opportunity_id == opp.id)
            .order_by(Transaction.date.desc().nullslast())
            .all()
        )
        transactions = []
        for txn, product, supplier in txn_rows:
            transactions.append({
                "id": str(txn.id),
                "doc_number": txn.doc_number,
                "doc_type": txn.doc_type,
                "date": str(txn.date) if txn.date else None,
                "supplier_name": supplier.name if supplier else None,
                "supplier_id": str(txn.supplier_id),
                "product_part": product.part_number if product else None,
                "product_brand": product.brand if product else None,
                "product_id": str(txn.product_id),
                "quantity": txn.quantity,
                "price": float(txn.price) if txn.price is not None else None,
                "cost": float(txn.cost) if txn.cost is not None else None,
                "cost_currency": supplier.currency if supplier else txn.cost_currency,
                "status": get_status_label(txn.doc_type or "", txn.status or "") if txn.status else None,
            })

        salesrep_name = None
        if opp.salesrep:
            salesrep_name = opp.salesrep.name
    finally:
        session.close()

    ctx = {
        "opp": opp,
        "customer": customer,
        "salesrep_name": salesrep_name,
        "transactions": transactions,
        "active_nav": "opportunities",
    }
    return _render(request, "opportunity_detail.html", "partials/opportunity_detail.html", ctx, user)
