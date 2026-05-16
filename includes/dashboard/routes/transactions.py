"""Transaction (purchase & quote history) routes."""

import math

from fastapi import Request, Depends

from includes.dashboard.models import Transaction, Supplier, Product
from . import _helpers
from ._helpers import router, templates, require_user, _render, PAGE_SIZE


@router.get("/transactions")
def transaction_list(request: Request, user: dict = Depends(require_user),
                     q: str = "", page: int = 1):
    from includes.netsuite.constants import get_status_label
    session = _helpers.get_session()
    try:
        query = (
            session.query(Transaction, Supplier.name, Supplier.currency, Product.part_number, Product.brand)
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
        for ps, sup_name, sup_currency, part_number, brand in rows:
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
                "price": float(ps.price) if ps.price is not None else None,
                "cost": float(ps.cost) if ps.cost is not None else None,
                "cost_currency": sup_currency,
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
    session = _helpers.get_session()
    try:
        query = (
            session.query(Transaction, Supplier.name, Supplier.currency, Product.part_number, Product.brand)
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
        for ps, sup_name, sup_currency, part_number, brand in rows:
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
                "price": float(ps.price) if ps.price is not None else None,
                "cost": float(ps.cost) if ps.cost is not None else None,
                "cost_currency": sup_currency,
                "status": get_status_label(ps.doc_type or "", ps.status or "") if ps.status else None,
            })
    finally:
        session.close()

    return templates.TemplateResponse(request, "partials/transaction_list.html", {
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
    session = _helpers.get_session()
    try:
        query = (
            session.query(Transaction, Supplier.name, Supplier.currency, Product.part_number, Product.brand)
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
        for ps, sup_name, sup_currency, part_number, brand in rows:
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
                "price": float(ps.price) if ps.price is not None else None,
                "cost": float(ps.cost) if ps.cost is not None else None,
                "cost_currency": sup_currency,
                "status": get_status_label(ps.doc_type or "", ps.status or "") if ps.status else None,
            })
    finally:
        session.close()

    return templates.TemplateResponse(request, "partials/_transaction_rows.html", {
        "transactions": transactions,
        "q": q,
        "has_more": page < total_pages,
        "next_page": page + 1,
    })
