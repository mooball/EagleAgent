"""
Product and Procurement tools.

Provides simple interfaces for querying the products catalog, performing exact matches,
and running vector similarity searches using pgvector and Gemini.
"""

import logging
import re
from typing import Optional
from langchain_core.tools import tool
from sqlalchemy import create_engine, or_, text, func
from sqlalchemy.orm import sessionmaker, aliased
from langchain_google_genai import GoogleGenerativeAIEmbeddings
import asyncio

from config.settings import Config
from includes.dashboard.models import Product, Brand, Supplier, SupplierBrand, Transaction

logger = logging.getLogger(__name__)

# Regex for normalizing part numbers — strips all non-alphanumeric chars.
# "C50LR-BR24-16" → "C50LRBR2416", "ABC/123.456" → "ABC123456"
_NORMALIZE_RE = re.compile(r'[^a-zA-Z0-9]')


def normalize_part_number(pn: str | None) -> str:
    """Strip separators for comparison. Returns empty string on None/empty."""
    if not pn:
        return ''
    return _NORMALIZE_RE.sub('', pn)


def _norm_expr(column):
    """SQLAlchemy expression for normalized part number comparison.
    
    Returns regexp_replace(column, '[^a-zA-Z0-9]', '', 'g') for use in
    WHERE clauses. Matches the functional index idx_products_part_number_norm.
    """
    return func.regexp_replace(column, '[^a-zA-Z0-9]', '', 'g')

def get_engine():
    db_url = Config.DATABASE_URL
    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
    elif db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1)
    return create_engine(db_url)

# Module-level singletons to avoid recreating on every call
_engine = None
_SessionLocal = None
_embeddings_model = None

def get_session():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = get_engine()
        _SessionLocal = sessionmaker(bind=_engine)
    return _SessionLocal()

def get_embeddings_model():
    global _embeddings_model
    if _embeddings_model is None:
        embed_model_name = Config.EMBEDDINGS_MODEL
        _embeddings_model = GoogleGenerativeAIEmbeddings(model=embed_model_name, location=Config.EMBEDDINGS_LOCATION, output_dimensionality=256)
    return _embeddings_model

def _do_product_search(part_number: Optional[str] = None, 
                       brand: Optional[str] = None, 
                       supplier_code: Optional[str] = None,
                       description: Optional[str] = None, 
                       limit: int = 10) -> str:
    """Executes the actual sqlalchemy queries synchronously"""
    session = get_session()
    try:
        base_query = session.query(Product)
        
        # Exact or partial match for part_number (normalized — ignores dashes/slashes/etc.)
        if part_number:
            norm_pn = normalize_part_number(part_number)
            base_query = base_query.filter(_norm_expr(Product.part_number).ilike(f"%{norm_pn}%"))
            
        # Exact or partial match for brand
        if brand:
            base_query = base_query.filter(Product.brand.ilike(f"%{brand}%"))
            
        # Exact or partial match for supplier_code
        if supplier_code:
            base_query = base_query.filter(Product.supplier_code.ilike(f"%{supplier_code}%"))
            
        results = []
        seen_ids = set()
        
        # First attempt: traditional string match on description (AND search)
        if description:
            words = [word.strip() for word in description.split() if word.strip()]
            string_query = base_query
            for word in words:
                string_query = string_query.filter(Product.description.ilike(f"%{word}%"))
                
            total_matches_count = string_query.count()
            string_results = string_query.limit(limit).all()
            for r in string_results:
                if r.id not in seen_ids:
                    results.append(r)
                    seen_ids.add(r.id)
        else:
            # If no description provided, just execute base query
            total_matches_count = base_query.count()
            results = base_query.limit(limit).all()
            for r in results:
                seen_ids.add(r.id)

        # Second attempt: fallback to vector proximity if we need more results and have a description
        if description and len(results) < limit:
            try:
                emb_model = get_embeddings_model()
                query_vector = emb_model.embed_query(description)
                vector_query = base_query.order_by(Product.embedding.cosine_distance(query_vector))
                
                v_results = vector_query.limit(limit * 2).all()
                for r in v_results:
                    if r.id not in seen_ids:
                        results.append(r)
                        seen_ids.add(r.id)
                        if len(results) >= limit:
                            break
            except Exception as e:
                logger.error(f"Failed to compute embedding for description search: {e}")
                
        # Trim final results just in case
        results = results[:limit]
        
        if not results:
            return "No products found matching those criteria."
            
        output_parts = [f"Found {total_matches_count} matching products. Displaying {len(results)} matches:"]
        for p in results:
            item = f"- Part Number: {p.part_number} | Brand: {p.brand} | Desc: {p.description or 'N/A'}"
            if p.supplier_code:
                item += f" | Supplier Code: {p.supplier_code}"
            output_parts.append(item)
            
        if total_matches_count > limit:
            output_parts.append(f"\nNote: There are {total_matches_count - limit} more unshown results. Ask the user if they'd like to list more or refine the search.")
            
        return "\n".join(output_parts)
    except Exception as e:
        logger.error(f"Error executing product search: {e}")
        return f"An error occurred while searching the database: {str(e)}"
    finally:
        session.close()

@tool
async def search_products(part_number: Optional[str] = None, 
                         brand: Optional[str] = None, 
                         supplier_code: Optional[str] = None,
                         description: Optional[str] = None, 
                         limit: int = 10) -> str:
    """
    Search the procurement products catalog.
    
    You can search by:
    - part_number: string match on the part number (e.g., '123-ABC')
    - brand: string match on the brand name (e.g., 'Caterpillar')
    - supplier_code: string match on the supplier's internal part code
    - description: semantic vector search to find similar capabilities or types of products
    
    Provide as many arguments as necessary. Specifying part_number, brand, or supplier_code will filter the search,
    while specifying description will semantically sort the results.
    """
    # Run synchronous database work via asyncio to prevent blocking the async graph
    return await asyncio.to_thread(_do_product_search, part_number, brand, supplier_code, description, limit)


def _do_brand_search(query: Optional[str] = None, limit: int = 20) -> str:
    """Executes the brand search synchronously."""
    session = get_session()
    try:
        base_query = session.query(Brand).filter(Brand.duplicate_of.is_(None))
        if query:
            base_query = base_query.filter(Brand.name.ilike(f"%{query}%"))

        total = base_query.count()
        if total == 0:
            return f"No brands found matching '{query}'." if query else "No brands found in the database."

        results = base_query.order_by(Brand.name).limit(limit).all()

        output_parts = [f"Found {total} matching brand(s). Displaying {len(results)}:"]
        for b in results:
            output_parts.append(f"- {b.name} (netsuite_id: {b.netsuite_id})")

        if total > limit:
            output_parts.append(f"\nNote: There are {total - limit} more unshown results.")

        return "\n".join(output_parts)
    except Exception as e:
        logger.error(f"Error executing brand search: {e}")
        return f"An error occurred while searching brands: {str(e)}"
    finally:
        session.close()


@tool
async def search_brands(query: Optional[str] = None, limit: int = 20) -> str:
    """
    Search the brands database.

    Search by brand name (partial, case-insensitive match).
    If a match is a known duplicate, the canonical brand is returned instead.
    Call with no arguments to get a total count of all brands.

    Args:
        query: The brand name to search for (e.g. 'Hilti', 'Cat'). Omit to count all brands.
        limit: Maximum number of results to return (default: 20)
    """
    return await asyncio.to_thread(_do_brand_search, query, limit)


def _suggest_spelling(session, query: str) -> Optional[str]:
    """Use pg_trgm word_similarity to suggest a corrected spelling from supplier names/notes.
    
    Returns the best matching word if similarity is high enough, else None.
    """
    try:
        # Extract individual words from query (3+ chars) to check each
        words = [w for w in query.split() if len(w) >= 3]
        if not words:
            return None

        for word in words:
            # Find the best matching supplier name word via pg_trgm
            row = session.execute(
                text(
                    "SELECT DISTINCT w.word, word_similarity(:q, w.word) AS sim "
                    "FROM suppliers s, LATERAL unnest(string_to_array(s.name, ' ')) AS w(word) "
                    "WHERE length(w.word) >= 3 AND word_similarity(:q, w.word) > 0.5 "
                    "ORDER BY sim DESC LIMIT 1"
                ),
                {"q": word},
            ).fetchone()
            if row and row[0].lower() != word.lower():
                # Suggest the corrected query with this word replaced
                corrected = query.replace(word, row[0])
                if corrected.lower() != query.lower():
                    return corrected
    except Exception as e:
        logger.warning(f"Spell suggestion failed: {e}")
    return None


def _flagged_supplier_targets(session, supplier_ids) -> dict:
    """{supplier_id: (target_id, target_name)} for use_instead-flagged rows.

    Returns {} on any error (mock sessions in tests included).
    """
    if not supplier_ids:
        return {}
    try:
        rows = (
            session.query(Supplier.id, Supplier.use_instead)
            .filter(
                Supplier.id.in_(list(supplier_ids)),
                Supplier.use_instead.isnot(None),
            )
            .all()
        )
        if not rows:
            return {}
        target_ids = {r[1] for r in rows if r[1]}
        targets = {
            t.id: t.name
            for t in session.query(Supplier).filter(Supplier.id.in_(list(target_ids))).all()
        }
        return {r[0]: (r[1], targets.get(r[1])) for r in rows}
    except Exception:
        return {}


def _do_supplier_search(name: Optional[str] = None,
                        brand: Optional[str] = None,
                        country: Optional[str] = None,
                        query: Optional[str] = None,
                        limit: int = 50) -> str:
    """Executes the supplier search synchronously."""
    import time
    from sqlalchemy import func, desc
    t0 = time.monotonic()
    session = get_session()
    t_session = time.monotonic()
    logger.info(f"[TIMING] supplier_search: get_session took {t_session - t0:.3f}s")
    try:
        # Build a subquery for purchase stats (order count + last date) per supplier
        purchase_sub = (
            session.query(
                Transaction.supplier_id,
                func.count(Transaction.id).label('purchase_count'),
                func.max(Transaction.date).label('last_purchase_date'),
            )
        )
        if brand:
            purchase_sub = (
                purchase_sub
                .join(Product, Transaction.product_id == Product.id)
                .filter(Product.brand.ilike(f"%{brand}%"))
            )
        purchase_sub = (
            purchase_sub
            .group_by(Transaction.supplier_id)
            .subquery()
        )

        # Base query: LEFT JOIN purchase stats so every supplier gets a count (0 if none)
        # Note: known duplicates are NOT filtered here — they're flagged in the
        # output so the agent can still use them for historical counts, but
        # must never link them to new RFQs/transactions.
        base_query = (
            session.query(
                Supplier,
                func.coalesce(purchase_sub.c.purchase_count, 0).label('purchase_count'),
                purchase_sub.c.last_purchase_date,
            )
            .outerjoin(purchase_sub, Supplier.id == purchase_sub.c.supplier_id)
        )

        if name:
            base_query = base_query.filter(Supplier.name.ilike(f"%{name}%"))

        if country:
            base_query = base_query.filter(Supplier.country.ilike(f"%{country}%"))

        if brand:
            base_query = (
                base_query
                .join(SupplierBrand, Supplier.id == SupplierBrand.supplier_id)
                .join(Brand, SupplierBrand.brand_id == Brand.id)
                .filter(Brand.name.ilike(f"%{brand}%"))
            )

        results = []
        seen_ids = set()
        text_match_count = 0

        # Stage 1: Get text matches, sorted by purchase count desc
        t_stage1 = time.monotonic()
        if query:
            string_query = base_query.filter(
                or_(
                    Supplier.name.ilike(f"%{query}%"),
                    Supplier.notes.ilike(f"%{query}%"),
                    Supplier.city.ilike(f"%{query}%"),
                )
            ).order_by(desc('purchase_count'), Supplier.name)
            text_results = string_query.distinct().all()
            total = len(text_results)
            text_match_count = total
            for row in text_results:
                s = row[0]
                if s.id not in seen_ids:
                    results.append(row)
                    seen_ids.add(s.id)
        else:
            total = base_query.distinct().count()
            rows = (
                base_query
                .order_by(desc('purchase_count'), Supplier.name)
                .distinct()
                .limit(limit)
                .all()
            )
            for row in rows:
                s = row[0]
                results.append(row)
                seen_ids.add(s.id)
        logger.info(f"[TIMING] supplier_search: stage1 query took {time.monotonic() - t_stage1:.3f}s (found {len(results)} results)")

        # We embed the query once and reuse for both product-vector and supplier-vector stages
        query_vector = None
        if query:
            try:
                t_embed = time.monotonic()
                emb_model = get_embeddings_model()
                query_vector = emb_model.embed_query(query)
                logger.info(f"[TIMING] supplier_search: embedding took {time.monotonic() - t_embed:.3f}s")
            except Exception as e:
                logger.error(f"Failed to compute query embedding: {e}")

        # Stage 2: Product-vector search — find suppliers who have sold similar products
        if query_vector and len(results) < limit:
            try:
                t_prod = time.monotonic()
                product_limit = 20  # top N similar products to consider
                similar_products = (
                    session.query(Product.id)
                    .filter(Product.embedding.isnot(None))
                    .order_by(Product.embedding.cosine_distance(query_vector))
                    .limit(product_limit)
                    .all()
                )
                product_ids = [p[0] for p in similar_products]

                if product_ids:
                    # Find suppliers linked to these products, excluding already-seen suppliers
                    product_supplier_query = (
                        base_query.filter(
                            Supplier.id.in_(
                                session.query(Transaction.supplier_id)
                                .filter(Transaction.product_id.in_(product_ids))
                                .distinct()
                            ),
                            ~Supplier.id.in_(list(seen_ids)) if seen_ids else True,
                        )
                        .order_by(desc('purchase_count'), Supplier.name)
                        .distinct()
                        .limit(limit - len(results))
                        .all()
                    )
                    for row in product_supplier_query:
                        s = row[0]
                        if s.id not in seen_ids:
                            results.append(row)
                            seen_ids.add(s.id)
                            total += 1
                logger.info(f"[TIMING] supplier_search: product-vector stage took {time.monotonic() - t_prod:.3f}s (now {len(results)} results)")
            except Exception as e:
                logger.error(f"Failed product-vector supplier search: {e}")

        # Stage 3: Supplier-notes vector search — semantic match on supplier notes
        if query_vector and len(results) < limit:
            try:
                t_vector = time.monotonic()
                vector_results = (
                    base_query.filter(
                        Supplier.embedding.isnot(None),
                        ~Supplier.id.in_(list(seen_ids)) if seen_ids else True,
                    )
                    .order_by(Supplier.embedding.cosine_distance(query_vector))
                    .limit(limit - len(results))
                    .all()
                )
                logger.info(f"[TIMING] supplier_search: supplier-notes vector fill took {time.monotonic() - t_vector:.3f}s")
                for row in vector_results:
                    s = row[0]
                    if s.id not in seen_ids:
                        results.append(row)
                        seen_ids.add(s.id)
                        total += 1
            except Exception as e:
                logger.error(f"Failed vector fill for supplier search: {e}")

        if not results:
            suggestion = _suggest_spelling(session, query) if query else None
            if suggestion:
                return f"No suppliers found matching '{query}'. Did you mean **{suggestion}**?"
            return "No suppliers found matching those criteria."

        # Sort all results by purchase count descending, then name
        results.sort(key=lambda row: (-row[1], (row[0].name or "").lower()))

        # Apply display limit
        displayed = results[:limit]

        # Collect all supplier IDs we need metadata for
        supplier_ids = [row[0].id for row in displayed]

        # Fetch linked brand names for each supplier
        t_brands = time.monotonic()
        brand_links = (
            session.query(SupplierBrand.supplier_id, Brand.name)
            .join(Brand, SupplierBrand.brand_id == Brand.id)
            .filter(SupplierBrand.supplier_id.in_(supplier_ids))
            .all()
        )
        logger.info(f"[TIMING] supplier_search: brand links query took {time.monotonic() - t_brands:.3f}s")
        supplier_brands = {}
        for sid, bname in brand_links:
            supplier_brands.setdefault(sid, []).append(bname)

        dup_targets = _flagged_supplier_targets(session, supplier_ids)
        dup_count = sum(1 for sid in supplier_ids if sid in dup_targets)

        output_parts = [f"Found {total} matching supplier(s). Displaying {len(displayed)}, sorted by purchase history (most purchases first):" + (
            f" — {dup_count} flagged as known duplicate(s) ⚠️" if dup_count else ""
        )]
        for row in displayed:
            s = row[0]
            purchase_count = row[1]
            last_purchase_date = row[2]
            item = f"- [{s.name}](/suppliers/{s.id})"
            if s.id in dup_targets:
                target_id, target_name = dup_targets[s.id]
                item += " ⚠️ **KNOWN DUPLICATE — DO NOT USE**"
                if target_name:
                    item += f" — use [{target_name}](/suppliers/{target_id}) instead"
            if s.city or s.country:
                location = ", ".join(filter(None, [s.city, s.country]))
                item += f" | Location: {location}"
            if s.url:
                item += f" | URL: {s.url}"
            if s.contacts:
                for contact in s.contacts:
                    label = contact.get("label", "")
                    c_parts = []
                    if contact.get("name"):
                        c_parts.append(contact["name"])
                    if contact.get("email"):
                        c_parts.append(contact["email"])
                    if contact.get("phone"):
                        c_parts.append(contact["phone"])
                    if c_parts:
                        item += f" | {label} Contact: {', '.join(c_parts)}"
            brands = supplier_brands.get(s.id, [])
            if brands:
                item += f" | Brands: {', '.join(sorted(brands))}"
            if purchase_count > 0:
                date_str = last_purchase_date.strftime("%-d %b %Y") if last_purchase_date else "N/A"
                item += f" | Purchases: {purchase_count} | Last Purchase: {date_str}"
            else:
                item += f" | Purchases: 0"
            output_parts.append(item)

        if total > len(displayed):
            output_parts.append(f"\nNote: There are {total - len(displayed)} more unshown results. Ask the user if they'd like to see more or refine the search.")

        # If few text matches, suggest a spelling correction
        if query and text_match_count < 3:
            suggestion = _suggest_spelling(session, query)
            if suggestion:
                output_parts.append(f"\nDid you mean **{suggestion}**? Try searching with the corrected spelling for more results.")

        logger.info(f"[TIMING] supplier_search: TOTAL took {time.monotonic() - t0:.3f}s")
        return "\n".join(output_parts)
    except Exception as e:
        logger.error(f"Error executing supplier search: {e}")
        return f"An error occurred while searching suppliers: {str(e)}"
    finally:
        session.close()


@tool
async def search_suppliers(name: Optional[str] = None,
                           brand: Optional[str] = None,
                           country: Optional[str] = None,
                           query: Optional[str] = None,
                           limit: int = 50) -> str:
    """
    Search the suppliers database.

    You can search by:
    - name: partial match on the supplier name
    - brand: find suppliers that carry a specific brand
    - country: filter by country
    - query: text + semantic search across supplier name, notes, city, AND purchase history.
      Supports natural language descriptions (e.g. 'heavy-duty conveyor components',
      'tyre digital inflation gauge', 'industrial adhesives manufacturer').
      String matches are tried first, then suppliers who have sold similar products
      are found via product-vector search, then vector similarity on supplier notes
      fills remaining results.

    Duplicate handling: known duplicates are NOT hidden. They appear flagged with
    "⚠️ KNOWN DUPLICATE — DO NOT USE" and a pointer to the surviving supplier.
    Use flagged rows only for historical data (purchase counts, past pricing).
    NEVER attach them to a new RFQ, quote, or transaction — use the surviving
    supplier instead.

    Provide as many arguments as needed to narrow results.

    Args:
        name: Supplier name to search for (e.g. 'Acme')
        brand: Brand name to filter by (e.g. 'Hilti') — finds suppliers linked to that brand
        country: Country to filter by (e.g. 'Australia')
        query: Text and semantic search across name, notes, city, and purchase history.
              Accepts natural language descriptions. Finds keyword matches first, then
              suppliers who have sold similar products, then ranks by vector proximity.
        limit: Maximum number of results to return (default: 50)
    """
    return await asyncio.to_thread(_do_supplier_search, name, brand, country, query, limit)


def _do_part_purchase_history(part_number: str, limit: int = 20) -> str:
    """Executes the per-part purchase history search synchronously."""
    session = get_session()
    try:
        # Find matching products by part number (normalized comparison)
        norm_pn = normalize_part_number(part_number)
        products = session.query(Product).filter(
            _norm_expr(Product.part_number).ilike(f"%{norm_pn}%")
        ).all()

        if not products:
            return f"No products found matching part number '{part_number}'."

        product_ids = [p.id for p in products]
        product_map = {p.id: p for p in products}

        # Query purchase history grouped by supplier
        from sqlalchemy import func, desc
        results = (
            session.query(
                Supplier.id.label('supplier_id'),
                Supplier.name.label('supplier_name'),
                Supplier.city.label('supplier_city'),
                Supplier.country.label('supplier_country'),
                Supplier.contacts.label('supplier_contacts'),
                Product.part_number.label('part_number'),
                Product.brand.label('brand'),
                func.max(Transaction.date).label('most_recent_date'),
                func.sum(Transaction.quantity).label('total_quantity'),
                func.count(Transaction.id).label('order_count'),
            )
            .join(Product, Transaction.product_id == Product.id)
            .join(Supplier, Transaction.supplier_id == Supplier.id)
            .filter(Transaction.product_id.in_(product_ids))
            .group_by(Supplier.id, Supplier.name, Supplier.city, Supplier.country, Supplier.contacts, Product.part_number, Product.brand)
            .order_by(desc('order_count'))
            .limit(limit)
            .all()
        )

        if not results:
            matched_parts = ', '.join(p.part_number for p in products)
            return f"Found product(s) ({matched_parts}) but no purchase history records exist."

        # Get most recent cost and price per supplier+product via a separate query
        from sqlalchemy import and_
        price_subquery = {}
        for row in results:
            latest = (
                session.query(Transaction.cost, Transaction.price)
                .join(Product, Transaction.product_id == Product.id)
                .filter(
                    and_(
                        Transaction.supplier_id == row.supplier_id,
                        Product.part_number == row.part_number,
                    )
                )
                .order_by(Transaction.date.desc().nulls_last())
                .first()
            )
            price_subquery[(row.supplier_id, row.part_number)] = (
                (latest.cost, latest.price) if latest else (None, None)
            )

        # Format output as markdown table
        output_parts = [f"Purchase history for part number '{part_number}':"]
        output_parts.append(f"\nFound {len(results)} supplier(s), sorted by number of purchases:\n")
        output_parts.append("| # | Supplier ID | Supplier | Location | Contact | Part Number | Brand | Last Cost | Last Sale Price | Last Date | Total Qty | Orders |")
        output_parts.append("|---|-------------|----------|----------|---------|-------------|-------|-----------|-----------------|-----------|-----------|---------|") 

        dup_targets = _flagged_supplier_targets(
            session, [row.supplier_id for row in results]
        )
        for idx, row in enumerate(results, 1):
            cost, price = price_subquery.get((row.supplier_id, row.part_number), (None, None))
            cost_str = f"${cost:,.2f}" if cost is not None else "N/A"
            price_str = f"${price:,.2f}" if price is not None else "N/A"
            date_str = row.most_recent_date.strftime("%-d %b %Y") if row.most_recent_date else "N/A"
            qty_str = f"{row.total_quantity:,.0f}" if row.total_quantity else "0"
            # Build location string
            location_parts = [p for p in [row.supplier_city, row.supplier_country] if p]
            location_str = ", ".join(location_parts) if location_parts else "N/A"
            # Build contact string from JSONB contacts
            contact_str = "N/A"
            if row.supplier_contacts:
                contacts = row.supplier_contacts if isinstance(row.supplier_contacts, list) else []
                if contacts:
                    c = contacts[0]  # Primary contact
                    parts = [p for p in [c.get("name"), c.get("email"), c.get("phone")] if p]
                    contact_str = " | ".join(parts) if parts else "N/A"
            name_cell = f"[{row.supplier_name}](/suppliers/{row.supplier_id})"
            if row.supplier_id in dup_targets:
                name_cell += " ⚠️ KNOWN DUPLICATE — DO NOT USE"
            output_parts.append(
                f"| {idx} | {row.supplier_id} | {name_cell} | {location_str} | {contact_str} | {row.part_number} | {row.brand or 'N/A'} | {cost_str} | {price_str} | {date_str} | {qty_str} | {row.order_count} |"
            )

        return "\n".join(output_parts)
    except Exception as e:
        logger.error(f"Error executing purchase history search: {e}")
        return f"An error occurred while searching purchase history: {str(e)}"
    finally:
        session.close()


@tool
async def part_purchase_history(part_number: str, limit: int = 20) -> str:
    """
    Search past purchase records to find which suppliers have supplied a given part.

    Returns a per-supplier summary: supplier name, most recent cost price, most recent
    sale price, most recent supply date, total quantity ever purchased, and number of orders.
    Sorted by total quantity descending.

    Use when the user asks "who can supply part X?", "which suppliers have we
    bought part X from?", "purchase history for part X", or similar.

    Known duplicate suppliers appear flagged with "⚠️ KNOWN DUPLICATE — DO NOT
    USE" — they are historical context only and must never be linked to new
    RFQs or transactions.

    Args:
        part_number: The part number to search for (e.g. '123-ABC'). Partial matches supported.
        limit: Maximum number of supplier results to return (default: 20)
    """
    return await asyncio.to_thread(_do_part_purchase_history, part_number, limit)


def _do_search_purchase_history(
    part_number: str = None,
    supplier: str = None,
    date_from: str = None,
    date_to: str = None,
    doc_number: str = None,
    limit: int = 50,
) -> str:
    """Executes generic purchase history search with flexible filters."""
    from sqlalchemy import func, desc, and_
    from datetime import datetime

    session = get_session()
    try:
        query = (
            session.query(
                Transaction.doc_number,
                Transaction.date,
                Transaction.quantity,
                Transaction.cost,
                Transaction.price,
                Transaction.status,
                Product.part_number.label('part_number'),
                Product.brand.label('brand'),
                Supplier.id.label('supplier_id'),
                Supplier.name.label('supplier_name'),
            )
            .join(Product, Transaction.product_id == Product.id)
            .join(Supplier, Transaction.supplier_id == Supplier.id)
        )

        filters = []
        filter_desc = []

        if part_number:
            norm_pn = normalize_part_number(part_number)
            query = query.filter(_norm_expr(Product.part_number).ilike(f"%{norm_pn}%"))
            filter_desc.append(f"part number matching '{part_number}'")

        if supplier:
            query = query.filter(Supplier.name.ilike(f"%{supplier}%"))
            filter_desc.append(f"supplier matching '{supplier}'")

        if doc_number:
            query = query.filter(Transaction.doc_number.ilike(f"%{doc_number}%"))
            filter_desc.append(f"document number matching '{doc_number}'")

        if date_from:
            try:
                dt = datetime.strptime(date_from, "%Y-%m-%d").date()
                query = query.filter(Transaction.date >= dt)
                filter_desc.append(f"from {date_from}")
            except ValueError:
                return f"Invalid date_from format '{date_from}'. Use YYYY-MM-DD."

        if date_to:
            try:
                dt = datetime.strptime(date_to, "%Y-%m-%d").date()
                query = query.filter(Transaction.date <= dt)
                filter_desc.append(f"to {date_to}")
            except ValueError:
                return f"Invalid date_to format '{date_to}'. Use YYYY-MM-DD."

        # Get total count before limiting
        total_count = query.count()

        if total_count == 0:
            desc_str = ", ".join(filter_desc) if filter_desc else "no filters"
            return f"No purchase history records found ({desc_str})."

        # If no specific filters, just return the count summary
        if not any([part_number, supplier, doc_number, date_from, date_to]):
            # Provide aggregate stats
            stats = session.query(
                func.count(Transaction.id).label('total_records'),
                func.count(func.distinct(Transaction.doc_number)).label('total_pos'),
                func.count(func.distinct(Transaction.product_id)).label('total_products'),
                func.count(func.distinct(Transaction.supplier_id)).label('total_suppliers'),
                func.min(Transaction.date).label('earliest_date'),
                func.max(Transaction.date).label('latest_date'),
            ).first()

            earliest = stats.earliest_date.strftime("%-d %b %Y") if stats.earliest_date else "N/A"
            latest = stats.latest_date.strftime("%-d %b %Y") if stats.latest_date else "N/A"

            return (
                f"Purchase history database summary:\n\n"
                f"| Metric | Value |\n"
                f"|--------|-------|\n"
                f"| Total purchase records | {stats.total_records:,} |\n"
                f"| Unique purchase orders | {stats.total_pos:,} |\n"
                f"| Unique products | {stats.total_products:,} |\n"
                f"| Unique suppliers | {stats.total_suppliers:,} |\n"
                f"| Date range | {earliest} — {latest} |\n"
            )

        # Fetch rows
        rows = (
            query
            .order_by(Transaction.date.desc().nulls_last())
            .limit(limit)
            .all()
        )

        desc_str = ", ".join(filter_desc)
        output = [f"Purchase history search ({desc_str}):"]
        output.append(f"\nFound {total_count:,} records. Showing {'all' if total_count <= limit else f'first {limit}'}:\n")
        output.append("| # | Doc Number | Date | Part Number | Brand | Supplier | Qty | Cost | Sale Price | Status |")
        output.append("|---|------------|------|-------------|-------|----------|-----|------|------------|--------|")

        for idx, row in enumerate(rows, 1):
            date_str = row.date.strftime("%-d %b %Y") if row.date else "N/A"
            cost_str = f"${row.cost:,.2f}" if row.cost is not None else "N/A"
            price_str = f"${row.price:,.2f}" if row.price is not None else "N/A"
            qty_str = f"{row.quantity:,.0f}" if row.quantity is not None else "N/A"
            output.append(
                f"| {idx} | {row.doc_number or 'N/A'} | {date_str} | {row.part_number} | {row.brand or 'N/A'} | [{row.supplier_name}](/suppliers/{row.supplier_id}) | {qty_str} | {cost_str} | {price_str} | {row.status or 'N/A'} |"
            )

        if total_count > limit:
            output.append(f"\n*{total_count - limit:,} more records not shown. Narrow filters or increase limit.*")

        return "\n".join(output)
    except Exception as e:
        logger.error(f"Error executing purchase history search: {e}")
        return f"An error occurred while searching purchase history: {str(e)}"
    finally:
        session.close()


@tool
async def search_purchase_history(
    part_number: str = None,
    supplier: str = None,
    date_from: str = None,
    date_to: str = None,
    doc_number: str = None,
    limit: int = 50,
) -> str:
    """
    Search and filter purchase history records. Flexible general-purpose query tool.

    Use with NO arguments to get a summary of the entire purchase history database
    (total records, total POs/quotes, unique products, unique suppliers, date range).

    Use with filters to find specific records. All filters are optional and combinable.

    Use when the user asks "how many purchase orders do we have?", "show me purchases
    from supplier X", "what did we buy in 2026?", "find document number P12345", or similar.

    For per-part supplier analysis ("who supplies part X?"), prefer part_purchase_history instead.

    Args:
        part_number: Filter by part number (partial match, e.g. '123-ABC')
        supplier: Filter by supplier name (partial match, e.g. 'Acme')
        date_from: Start date filter in YYYY-MM-DD format (e.g. '2026-01-01')
        date_to: End date filter in YYYY-MM-DD format (e.g. '2026-12-31')
        doc_number: Filter by document number - PO or quote (partial match, e.g. 'P158740')
        limit: Maximum number of records to return (default: 50)
    """
    return await asyncio.to_thread(
        _do_search_purchase_history, part_number, supplier, date_from, date_to, doc_number, limit
    )


# ---------------------------------------------------------------------------
# Structured DB helpers — return dicts with IDs for RFQ linking
# ---------------------------------------------------------------------------

def _find_product_by_code(part_number: str, brand: str = None) -> Optional[dict]:
    """Find a product by part_number OR supplier_code. Returns dict or None.

    When multiple products match, the one with the most purchase-history
    transactions is returned (ties broken by most recently modified).
    """
    session = get_session()
    try:
        norm_pn = normalize_part_number(part_number)

        # Rank matches by purchase count — the most-purchased product wins.
        purchase_counts = (
            session.query(
                Transaction.product_id,
                func.count(Transaction.id).label("purchase_count"),
            )
            .group_by(Transaction.product_id)
            .subquery()
        )
        query = (
            session.query(Product)
            .outerjoin(
                purchase_counts,
                purchase_counts.c.product_id == Product.id,
            )
            .filter(
                or_(
                    _norm_expr(Product.part_number).ilike(norm_pn),
                    _norm_expr(Product.supplier_code).ilike(norm_pn),
                )
            )
        )
        if brand:
            query = query.filter(Product.brand.ilike(brand))
        product = query.order_by(
            purchase_counts.c.purchase_count.desc().nulls_last(),
            Product.netsuite_last_modified.desc().nulls_last(),
        ).first()
        if product:
            return {
                "id": str(product.id),
                "part_number": product.part_number,
                "brand": product.brand,
                "description": product.description,
                "supplier_code": product.supplier_code,
            }
        return None
    finally:
        session.close()


# Brand names treated as "no brand" during classification — never looked up.
BRAND_NAME_EXCLUSIONS = ("other", "n/a", "na", "none", "unknown")


def _brand_result(status: str, brand=None, alternatives=None) -> dict:
    return {
        "status": status,
        "brand": (
            {
                "name": brand.name,
                "id": str(brand.id),
                "netsuite_id": brand.netsuite_id,
            }
            if brand is not None
            else None
        ),
        "alternatives": alternatives or [],
    }


def match_brands(names: list[str], limit: int = 10) -> dict[str, dict]:
    """Batch deterministic brand lookup — two queries for any number of names.

    Same semantics as :func:`match_brand` (normalised exact first, then
    normalised substring), but resolves many names at once. Returns a dict
    keyed by the exact input name.
    """
    session = get_session()
    try:
        norms: dict[str, str] = {}
        for name in names:
            norm = normalize_part_number(name)
            if norm and norm not in norms:
                norms[norm] = name
        if not norms:
            return {}

        norm_list = list(norms.keys())
        # ilike (case-insensitive) keeps parity with the single-name matcher;
        # normalised names are alphanumeric only so no wildcard injection.
        exact_rows = (
            session.query(Brand)
            .filter(
                Brand.duplicate_of.is_(None),
                or_(*[_norm_expr(Brand.name).ilike(n) for n in norm_list]),
            )
            .order_by(Brand.name)
            .all()
        )
        conds = [_norm_expr(Brand.name).ilike(f"%{n}%") for n in norm_list]
        near_rows = (
            session.query(Brand)
            .filter(Brand.duplicate_of.is_(None), or_(*conds))
            .order_by(Brand.name)
            .limit(max(limit, 1) * len(norm_list))
            .all()
        )

        def _nkey(b) -> str:
            return normalize_part_number(b.name).lower()

        results: dict[str, dict] = {}
        for norm, name in norms.items():
            key = norm.lower()
            exact_matches = [b for b in exact_rows if _nkey(b) == key]
            near_matches = [b for b in near_rows if key in _nkey(b)]
            if exact_matches:
                chosen = exact_matches[0]
                alternatives = [b.name for b in near_matches if b.id != chosen.id][: limit - 1]
                results[name] = _brand_result("exact", chosen, alternatives)
            elif near_matches:
                results[name] = _brand_result(
                    "near", None, [b.name for b in near_matches[:limit]]
                )
            else:
                results[name] = _brand_result("none")
        return results
    finally:
        session.close()


def match_brand(name: str, limit: int = 10) -> dict:
    """Deterministic brand lookup for classification.

    Normalised exact equality first (punctuation/case-insensitive), then a
    normalised substring pass ("toyota" → "Toyota Parts"). Only canonical
    brands (duplicate_of IS NULL) are considered; ties resolve
    alphabetically.

    Returns:
        {"status": "exact" | "near" | "none",
         "brand": {"name": str, "id": str, "netsuite_id": str} | None,
         "alternatives": [str, ...]}  # other candidate names
    """
    return match_brands([name], limit=limit).get(name) or _brand_result("none")


def _find_purchase_history_for_part(part_number: str, limit: int = 20) -> list[dict]:
    """Return structured purchase history per supplier for a part number.

    Each dict: supplier_id, supplier_name, contacts, cost, price, margin,
    date, doc_number, order_count, total_qty
    """
    from sqlalchemy import func, desc, and_

    session = get_session()
    try:
        norm_pn = normalize_part_number(part_number)
        products = session.query(Product).filter(
            _norm_expr(Product.part_number).ilike(norm_pn)
        ).all()
        if not products:
            return []

        product_ids = [p.id for p in products]

        results = (
            session.query(
                Supplier.id.label("supplier_id"),
                Supplier.name.label("supplier_name"),
                Supplier.contacts.label("supplier_contacts"),
                func.max(Transaction.date).label("most_recent_date"),
                func.sum(Transaction.quantity).label("total_quantity"),
                func.count(Transaction.id).label("order_count"),
            )
            .join(Product, Transaction.product_id == Product.id)
            .join(Supplier, Transaction.supplier_id == Supplier.id)
            .filter(Transaction.product_id.in_(product_ids))
            .group_by(Supplier.id, Supplier.name, Supplier.contacts)
            .order_by(desc("order_count"))
            .limit(limit)
            .all()
        )

        out = []
        dup_targets = _flagged_supplier_targets(
            session, [row.supplier_id for row in results]
        )
        for row in results:
            # Get latest PO cost
            latest_po = (
                session.query(Transaction.cost, Transaction.price, Transaction.doc_number, Transaction.date)
                .filter(
                    and_(
                        Transaction.supplier_id == row.supplier_id,
                        Transaction.product_id.in_(product_ids),
                        Transaction.doc_type == "PurchaseOrder",
                    )
                )
                .order_by(Transaction.date.desc().nulls_last())
                .first()
            )
            # Get latest SO sale price
            latest_so = (
                session.query(Transaction.price, Transaction.doc_number, Transaction.date)
                .filter(
                    and_(
                        Transaction.supplier_id == row.supplier_id,
                        Transaction.product_id.in_(product_ids),
                        Transaction.doc_type == "SalesOrder",
                    )
                )
                .order_by(Transaction.date.desc().nulls_last())
                .first()
            )
            # Fall back to any latest transaction for basic price
            latest_any = (
                session.query(Transaction.price, Transaction.cost, Transaction.doc_number)
                .filter(
                    and_(
                        Transaction.supplier_id == row.supplier_id,
                        Transaction.product_id.in_(product_ids),
                    )
                )
                .order_by(Transaction.date.desc().nulls_last())
                .first()
            )

            contacts = []
            if row.supplier_contacts and isinstance(row.supplier_contacts, list):
                contacts = row.supplier_contacts

            # Determine cost (prefer PO cost field, fall back to PO price)
            cost = None
            if latest_po:
                cost = latest_po.cost if latest_po.cost is not None else latest_po.price

            # Determine sale price (from SO)
            sale_price = latest_so.price if latest_so else None

            # Fall back price from any transaction
            price = latest_any[0] if latest_any else None

            # Calculate margin if both cost and sale are available
            margin = None
            if cost and sale_price and sale_price > 0:
                margin = round((sale_price - cost) / sale_price * 100, 1)

            out.append({
                "supplier_id": str(row.supplier_id),
                "name": row.supplier_name,
                "contacts": contacts,
                "is_duplicate": row.supplier_id in dup_targets,
                "cost": cost,
                "price": sale_price or price,
                "margin": margin,
                "doc_number": latest_po.doc_number if latest_po else (latest_any[2] if latest_any else None),
                "date": row.most_recent_date.isoformat() if row.most_recent_date else None,
                "order_count": row.order_count,
                "total_quantity": float(row.total_quantity) if row.total_quantity else 0,
            })
        return out
    finally:
        session.close()


def _find_suppliers_by_brand(brand: str, limit: int = 200) -> list[dict]:
    """Find suppliers linked to a brand. Returns list of dicts with supplier_id, name, contacts."""
    session = get_session()
    try:
        results = (
            session.query(Supplier)
            .join(SupplierBrand, SupplierBrand.supplier_id == Supplier.id)
            .join(Brand, SupplierBrand.brand_id == Brand.id)
            .filter(
                Brand.name.ilike(f"%{brand}%"),
                Brand.duplicate_of.is_(None),
                Supplier.use_instead.is_(None),
            )
            .limit(limit)
            .all()
        )
        out = []
        for s in results:
            contacts = s.contacts if isinstance(s.contacts, list) else []
            out.append({
                "supplier_id": str(s.id),
                "name": s.name,
                "contacts": contacts,
            })
        return out
    finally:
        session.close()


def _find_brand_suppliers_with_tier(brand: str, limit: int = 200) -> list[dict]:
    """Find suppliers linked to a brand, enriched with tier and brand-specific transaction count.

    Returns list sorted by tier (A first) then transaction count (desc).
    Transaction count is filtered to only transactions for products of this brand.
    Each dict: supplier_id, name, contacts, tier, transaction_count, country.
    """
    from sqlalchemy import func, desc

    session = get_session()
    try:
        # Sub-query: count transactions per supplier for THIS specific brand
        tx_count_sub = (
            session.query(
                Transaction.supplier_id,
                func.count(Transaction.id).label("tx_count"),
            )
            .join(Product, Transaction.product_id == Product.id)
            .join(Brand, Product.brand_id == Brand.id)
            .filter(Brand.name.ilike(brand), Brand.duplicate_of.is_(None))
            .group_by(Transaction.supplier_id)
            .subquery()
        )

        results = (
            session.query(
                Supplier.id,
                Supplier.name,
                Supplier.contacts,
                Supplier.country,
                Supplier.supply_chain_position,
                func.coalesce(tx_count_sub.c.tx_count, 0).label("tx_count"),
            )
            .join(SupplierBrand, SupplierBrand.supplier_id == Supplier.id)
            .join(Brand, SupplierBrand.brand_id == Brand.id)
            .outerjoin(tx_count_sub, tx_count_sub.c.supplier_id == Supplier.id)
            .filter(
                Brand.name.ilike(brand),
                Brand.duplicate_of.is_(None),
                Supplier.use_instead.is_(None),
            )
            .all()
        )

        _tier_order = {"A": 0, "B": 1, "C": 2, "D": 3}
        out = []
        for row in results:
            scp = row.supply_chain_position or {}
            tier = scp.get("tier")
            contacts = row.contacts if isinstance(row.contacts, list) else []
            out.append({
                "supplier_id": str(row.id),
                "name": row.name,
                "contacts": contacts,
                "tier": tier,
                "transaction_count": row.tx_count,
                "country": row.country,
            })

        # Sort: suppliers with brand transactions first (tier, then txn count desc),
        # then zero-transaction suppliers (tier only).
        out.sort(key=lambda s: (
            0 if s["transaction_count"] > 0 else 1,
            _tier_order.get(s["tier"], 9),
            -s["transaction_count"]
        ))
        return out[:limit]
    finally:
        session.close()