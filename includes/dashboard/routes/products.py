"""Product list, detail, and HTMX partial routes."""

import math

from fastapi import Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func

from includes.dashboard.models import Product, Transaction, Supplier
from . import _helpers
from ._helpers import router, templates, require_user, _render, PAGE_SIZE


# ---------------------------------------------------------------------------
# Embedding model (lazy singleton, reused across requests)
# ---------------------------------------------------------------------------
_embeddings_model = None


def _get_embeddings_model():
    """Return a cached GoogleGenerativeAIEmbeddings instance (256-dim)."""
    global _embeddings_model
    if _embeddings_model is None:
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        _embeddings_model = GoogleGenerativeAIEmbeddings(
            model=_helpers.config.EMBEDDINGS_MODEL,
            location=_helpers.config.EMBEDDINGS_LOCATION,
            output_dimensionality=256,
        )
    return _embeddings_model


# ---------------------------------------------------------------------------
# Hybrid search helper
# ---------------------------------------------------------------------------
def _hybrid_product_search(session, q: str, semantic: bool = False):
    """Run text (ILIKE) search and optionally semantic (pgvector) search.

    Returns (products, total) where each Product ORM object has a
    ``_match_types`` attribute: a set of {"B", "P", "D", "S1", "S2"}.
    """
    txn_count = func.count(Transaction.id).label("txn_count")
    base_query = (
        session.query(Product, txn_count)
        .outerjoin(Transaction, Transaction.product_id == Product.id)
        .group_by(Product.id)
    )

    # -- Text search -----------------------------------------------------------
    text_matches: dict[str, set] = {}  # product_id → match types
    text_products: list = []

    if q:
        # Part number match
        p_query = base_query.filter(Product.part_number.ilike(f"%{q}%"))
        for p, cnt in p_query.all():
            text_matches.setdefault(p.id, set()).add("P")

        # Brand match
        b_query = base_query.filter(Product.brand.ilike(f"%{q}%"))
        for p, cnt in b_query.all():
            text_matches.setdefault(p.id, set()).add("B")

        # Description match
        d_query = base_query.filter(Product.description.ilike(f"%{q}%"))
        for p, cnt in d_query.all():
            text_matches.setdefault(p.id, set()).add("D")

        # Fetch all text-matched products with txn_count in one go
        if text_matches:
            text_products = (
                base_query.filter(Product.id.in_(text_matches.keys()))
                .order_by(txn_count.desc(), Product.brand, Product.part_number)
                .all()
            )
            for p, cnt in text_products:
                p.txn_count = cnt

    # -- Semantic search -------------------------------------------------------
    semantic_ids: dict[str, tuple] = {}  # product_id → (distance, match_type)
    if semantic and q:
        try:
            emb_model = _get_embeddings_model()
            query_vector = emb_model.embed_query(q, task_type="SEMANTIC_SIMILARITY")
            # Only search products that HAVE embeddings; hard cutoff at 0.4
            sem_query = (
                session.query(Product, txn_count, Product.embedding.cosine_distance(query_vector).label("distance"))
                .outerjoin(Transaction, Transaction.product_id == Product.id)
                .filter(Product.embedding.isnot(None))
                .group_by(Product.id)
                .having(func.count() > 0)
                .order_by("distance")
                .limit(30)  # fetch extra so we still get ~20 after HAVING filter
            )
            for p, cnt, dist in sem_query.all():
                if dist >= 0.4:
                    continue  # hard cutoff
                if p.id not in semantic_ids:
                    p.txn_count = cnt
                    tier = "S1" if dist < 0.2 else "S2"
                    semantic_ids[p.id] = (dist, tier)
        except Exception:
            pass  # Semantic search is optional — don't break the page

    # -- Merge ----------------------------------------------------------------
    all_ids: dict[str, tuple] = {}  # product_id → (product, txn_count, match_types)

    # Text results first (ordered by txn_count)
    for p, cnt in text_products:
        all_ids[p.id] = (p, cnt, text_matches.get(p.id, set()))

    # Semantic results (append after text results)
    if semantic_ids:
        # Fetch full product objects for semantic matches
        sem_products = (
            base_query.filter(Product.id.in_(semantic_ids.keys()))
            .all()
        )
        for p, cnt in sem_products:
            p.txn_count = cnt
            if p.id not in all_ids:
                all_ids[p.id] = (p, cnt, {semantic_ids[p.id][1]})

    # Build final ordered list: text matches first (by txn_count), then semantic
    result = []
    text_order = [p_id for p_id in text_matches if p_id in all_ids]
    sem_order = [p_id for p_id in semantic_ids if p_id in all_ids and p_id not in text_matches]

    for p_id in text_order:
        p, cnt, types = all_ids[p_id]
        p._match_types = types
        result.append(p)

    # Sort semantic results by distance (closest first)
    for p_id in sorted(sem_order, key=lambda pid: semantic_ids[pid][0]):
        p, cnt, types = all_ids[p_id]
        p._match_types = types
        result.append(p)

    return result, len(result)


# ---------------------------------------------------------------------------
# Full-page routes
# ---------------------------------------------------------------------------
@router.get("/products")
def product_list(request: Request, user: dict = Depends(require_user),
                 q: str = "", page: int = 1, semantic: str = ""):
    use_semantic = semantic == "1"
    session = _helpers.get_session()
    try:
        if q:
            all_products, total = _hybrid_product_search(session, q, semantic=use_semantic)
            total_pages = max(1, math.ceil(total / PAGE_SIZE))
            page = max(1, min(page, total_pages))
            products = all_products[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        else:
            txn_count = func.count(Transaction.id).label("txn_count")
            query = (
                session.query(Product, txn_count)
                .outerjoin(Transaction, Transaction.product_id == Product.id)
                .group_by(Product.id)
            )
            total = query.count()
            total_pages = max(1, math.ceil(total / PAGE_SIZE))
            page = max(1, min(page, total_pages))
            rows = (
                query
                .order_by(txn_count.desc(), Product.brand, Product.part_number)
                .offset((page - 1) * PAGE_SIZE)
                .limit(PAGE_SIZE)
                .all()
            )
            products = []
            for p, cnt in rows:
                p.txn_count = cnt
                products.append(p)
    finally:
        session.close()

    ctx = {
        "products": products,
        "q": q,

        "semantic": "1" if use_semantic else "",
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
                         q: str = "", page: int = 1, semantic: str = ""):
    use_semantic = semantic == "1"
    session = _helpers.get_session()
    try:
        if q:
            all_products, total = _hybrid_product_search(session, q, semantic=use_semantic)
            total_pages = max(1, math.ceil(total / PAGE_SIZE))
            page = max(1, min(page, total_pages))
            products = all_products[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        else:
            txn_count = func.count(Transaction.id).label("txn_count")
            query = (
                session.query(Product, txn_count)
                .outerjoin(Transaction, Transaction.product_id == Product.id)
                .group_by(Product.id)
            )
            total = query.count()
            total_pages = max(1, math.ceil(total / PAGE_SIZE))
            page = max(1, min(page, total_pages))
            rows = (
                query
                .order_by(txn_count.desc(), Product.brand, Product.part_number)
                .offset((page - 1) * PAGE_SIZE)
                .limit(PAGE_SIZE)
                .all()
            )
            products = []
            for p, cnt in rows:
                p.txn_count = cnt
                products.append(p)
    finally:
        session.close()

    return templates.TemplateResponse(request, "partials/product_list.html", {
        "user": user,
        "products": products,
        "q": q,

        "semantic": "1" if use_semantic else "",
        "page": page,
        "total": total,
        "has_more": page < total_pages,
        "next_page": page + 1,
    })


@router.get("/partial/products/rows")
def partial_product_rows(request: Request, user: dict = Depends(require_user),
                         q: str = "", page: int = 1, semantic: str = ""):
    """Return just the <tr> rows + sentinel for infinite scroll."""
    use_semantic = semantic == "1"
    session = _helpers.get_session()
    try:
        if q:
            all_products, total = _hybrid_product_search(session, q, semantic=use_semantic)
            total_pages = max(1, math.ceil(total / PAGE_SIZE))
            page = max(1, min(page, total_pages))
            products = all_products[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        else:
            txn_count = func.count(Transaction.id).label("txn_count")
            query = (
                session.query(Product, txn_count)
                .outerjoin(Transaction, Transaction.product_id == Product.id)
                .group_by(Product.id)
            )
            total = query.count()
            total_pages = max(1, math.ceil(total / PAGE_SIZE))
            page = max(1, min(page, total_pages))
            rows = (
                query
                .order_by(txn_count.desc(), Product.brand, Product.part_number)
                .offset((page - 1) * PAGE_SIZE)
                .limit(PAGE_SIZE)
                .all()
            )
            products = []
            for p, cnt in rows:
                p.txn_count = cnt
                products.append(p)
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
