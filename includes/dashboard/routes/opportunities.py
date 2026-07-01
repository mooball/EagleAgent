"""Dashboard routes for Opportunities."""

import math

from fastapi import Request, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import or_, text

from includes.dashboard.models import Customer, Opportunity, Product, Supplier, Transaction
from . import _helpers
from ._helpers import router, templates, require_user, _render, PAGE_SIZE


def _query_opportunities(session, q: str, page: int):
    """Shared query logic for opportunity list routes."""
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

    return opportunities, total, total_pages, page


@router.get("/opportunities")
def opportunity_list(request: Request, user: dict = Depends(require_user),
                     q: str = "", page: int = 1):
    session = _helpers.get_session()
    try:
        opportunities, total, total_pages, page = _query_opportunities(session, q, page)
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
        opportunities, total, total_pages, page = _query_opportunities(session, q, page)
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
        opportunities, total, total_pages, page = _query_opportunities(session, q, page)
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


@router.get("/api/opportunities/search")
async def api_opportunity_search(request: Request, q: str = "", customer_id: str = "", user: dict = Depends(require_user)):
    """Search opportunities by number or title for autocomplete.
    
    Optional customer_id filters results to that customer's opportunities.
    Returns {results: [{id, number, name, title, customer_id, customer_name, salesrep_email}]}.
    """
    if not q or len(q.strip()) < 2:
        return JSONResponse({"results": []})

    session = _helpers.get_session()
    try:
        query = """
            SELECT o.id, o.opportunity_number, o.title,
                   o.customer_id, c.companyname AS customer_name,
                   ne.email AS salesrep_email
            FROM opportunities o
            LEFT JOIN customers c ON o.customer_id = c.id
            LEFT JOIN netsuite_employee_mappings ne ON o.salesrep_id = ne.id
            WHERE (LOWER(o.opportunity_number) LIKE :q OR LOWER(o.title) LIKE :q)
        """
        params = {"q": f"%{q.strip().lower()}%"}

        if customer_id and customer_id.strip():
            query += " AND o.customer_id = :cid"
            params["cid"] = customer_id.strip()

        query += " ORDER BY o.opportunity_number LIMIT 10"

        rows = session.execute(text(query), params).mappings().all()
        return JSONResponse({"results": [
            {
                "id": str(r["id"]),
                "number": r["opportunity_number"] or "",
                "name": f"{r['opportunity_number'] or ''} — {r['title'] or ''}".strip(" —"),
                "title": r["title"] or "",
                "customer_id": str(r["customer_id"]) if r["customer_id"] else "",
                "customer_name": r["customer_name"] or "",
                "salesrep_email": r["salesrep_email"] or "",
            }
            for r in rows
        ]})
    finally:
        session.close()
