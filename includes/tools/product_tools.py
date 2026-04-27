"""
Product and Procurement tools.

Provides simple interfaces for querying the products catalog, performing exact matches,
and running vector similarity searches using pgvector and Gemini.
"""

import logging
from typing import Optional
from langchain_core.tools import tool
from sqlalchemy import create_engine, or_, text
from sqlalchemy.orm import sessionmaker, aliased
from langchain_google_genai import GoogleGenerativeAIEmbeddings
import asyncio

from config.settings import Config
from includes.dashboard.models import Product, Brand, Supplier, SupplierBrand, ProductSupplier

logger = logging.getLogger(__name__)

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
        
        # Exact or partial match for part_number
        if part_number:
            base_query = base_query.filter(Product.part_number.ilike(f"%{part_number}%"))
            
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
                ProductSupplier.supplier_id,
                func.count(ProductSupplier.id).label('purchase_count'),
                func.max(ProductSupplier.date).label('last_purchase_date'),
            )
        )
        if brand:
            purchase_sub = (
                purchase_sub
                .join(Product, ProductSupplier.product_id == Product.id)
                .filter(Product.brand.ilike(f"%{brand}%"))
            )
        purchase_sub = (
            purchase_sub
            .group_by(ProductSupplier.supplier_id)
            .subquery()
        )

        # Base query: LEFT JOIN purchase stats so every supplier gets a count (0 if none)
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
                                session.query(ProductSupplier.supplier_id)
                                .filter(ProductSupplier.product_id.in_(product_ids))
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

        output_parts = [f"Found {total} matching supplier(s). Displaying {len(displayed)}, sorted by purchase history (most purchases first):"]
        for row in displayed:
            s = row[0]
            purchase_count = row[1]
            last_purchase_date = row[2]
            item = f"- [{s.name}](/suppliers/{s.id})"
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
        # Find matching products by part number
        products = session.query(Product).filter(
            Product.part_number.ilike(f"%{part_number}%")
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
                func.max(ProductSupplier.date).label('most_recent_date'),
                func.sum(ProductSupplier.quantity).label('total_quantity'),
                func.count(ProductSupplier.id).label('order_count'),
            )
            .join(Product, ProductSupplier.product_id == Product.id)
            .join(Supplier, ProductSupplier.supplier_id == Supplier.id)
            .filter(ProductSupplier.product_id.in_(product_ids))
            .group_by(Supplier.id, Supplier.name, Supplier.city, Supplier.country, Supplier.contacts, Product.part_number, Product.brand)
            .order_by(desc('order_count'))
            .limit(limit)
            .all()
        )

        if not results:
            matched_parts = ', '.join(p.part_number for p in products)
            return f"Found product(s) ({matched_parts}) but no purchase history records exist."

        # Get most recent price per supplier+product via a separate query
        from sqlalchemy import and_
        price_subquery = {}
        for row in results:
            latest = (
                session.query(ProductSupplier.price)
                .join(Product, ProductSupplier.product_id == Product.id)
                .filter(
                    and_(
                        ProductSupplier.supplier_id == row.supplier_id,
                        Product.part_number == row.part_number,
                    )
                )
                .order_by(ProductSupplier.date.desc().nulls_last())
                .first()
            )
            price_subquery[(row.supplier_id, row.part_number)] = latest[0] if latest else None

        # Format output as markdown table
        output_parts = [f"Purchase history for part number '{part_number}':"]
        output_parts.append(f"\nFound {len(results)} supplier(s), sorted by number of purchases:\n")
        output_parts.append("| # | Supplier ID | Supplier | Location | Contact | Part Number | Brand | Last Price | Last Date | Total Qty | Orders |")
        output_parts.append("|---|-------------|----------|----------|---------|-------------|-------|-----------|-----------|-----------|--------|")

        for idx, row in enumerate(results, 1):
            price = price_subquery.get((row.supplier_id, row.part_number))
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
            output_parts.append(
                f"| {idx} | {row.supplier_id} | [{row.supplier_name}](/suppliers/{row.supplier_id}) | {location_str} | {contact_str} | {row.part_number} | {row.brand or 'N/A'} | {price_str} | {date_str} | {qty_str} | {row.order_count} |"
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

    Returns a per-supplier summary: supplier name, most recent price, most recent
    supply date, total quantity ever purchased, and number of orders.
    Sorted by total quantity descending.

    Use when the user asks "who can supply part X?", "which suppliers have we
    bought part X from?", "purchase history for part X", or similar.

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
                ProductSupplier.doc_number,
                ProductSupplier.date,
                ProductSupplier.quantity,
                ProductSupplier.price,
                ProductSupplier.status,
                Product.part_number.label('part_number'),
                Product.brand.label('brand'),
                Supplier.id.label('supplier_id'),
                Supplier.name.label('supplier_name'),
            )
            .join(Product, ProductSupplier.product_id == Product.id)
            .join(Supplier, ProductSupplier.supplier_id == Supplier.id)
        )

        filters = []
        filter_desc = []

        if part_number:
            query = query.filter(Product.part_number.ilike(f"%{part_number}%"))
            filter_desc.append(f"part number matching '{part_number}'")

        if supplier:
            query = query.filter(Supplier.name.ilike(f"%{supplier}%"))
            filter_desc.append(f"supplier matching '{supplier}'")

        if doc_number:
            query = query.filter(ProductSupplier.doc_number.ilike(f"%{doc_number}%"))
            filter_desc.append(f"document number matching '{doc_number}'")

        if date_from:
            try:
                dt = datetime.strptime(date_from, "%Y-%m-%d").date()
                query = query.filter(ProductSupplier.date >= dt)
                filter_desc.append(f"from {date_from}")
            except ValueError:
                return f"Invalid date_from format '{date_from}'. Use YYYY-MM-DD."

        if date_to:
            try:
                dt = datetime.strptime(date_to, "%Y-%m-%d").date()
                query = query.filter(ProductSupplier.date <= dt)
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
                func.count(ProductSupplier.id).label('total_records'),
                func.count(func.distinct(ProductSupplier.doc_number)).label('total_pos'),
                func.count(func.distinct(ProductSupplier.product_id)).label('total_products'),
                func.count(func.distinct(ProductSupplier.supplier_id)).label('total_suppliers'),
                func.min(ProductSupplier.date).label('earliest_date'),
                func.max(ProductSupplier.date).label('latest_date'),
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
            .order_by(ProductSupplier.date.desc().nulls_last())
            .limit(limit)
            .all()
        )

        desc_str = ", ".join(filter_desc)
        output = [f"Purchase history search ({desc_str}):"]
        output.append(f"\nFound {total_count:,} records. Showing {'all' if total_count <= limit else f'first {limit}'}:\n")
        output.append("| # | Doc Number | Date | Part Number | Brand | Supplier | Qty | Price | Status |")
        output.append("|---|------------|------|-------------|-------|----------|-----|-------|--------|")

        for idx, row in enumerate(rows, 1):
            date_str = row.date.strftime("%-d %b %Y") if row.date else "N/A"
            price_str = f"${row.price:,.2f}" if row.price is not None else "N/A"
            qty_str = f"{row.quantity:,.0f}" if row.quantity is not None else "N/A"
            output.append(
                f"| {idx} | {row.doc_number or 'N/A'} | {date_str} | {row.part_number} | {row.brand or 'N/A'} | [{row.supplier_name}](/suppliers/{row.supplier_id}) | {qty_str} | {price_str} | {row.status or 'N/A'} |"
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

def _find_product_exact(part_number: str, brand: str = None) -> Optional[dict]:
    """Find a product by exact part number match. Returns dict with id, part_number, brand or None."""
    session = get_session()
    try:
        query = session.query(Product).filter(Product.part_number.ilike(part_number))
        if brand:
            query = query.filter(Product.brand.ilike(brand))
        product = query.first()
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


def _find_product_by_supplier_code(supplier_code: str, brand: str = None) -> Optional[dict]:
    """Find a product by supplier code. Returns dict with id, part_number, brand or None."""
    session = get_session()
    try:
        query = session.query(Product).filter(Product.supplier_code.ilike(supplier_code))
        if brand:
            query = query.filter(Product.brand.ilike(brand))
        product = query.first()
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


def _find_purchase_history_for_part(part_number: str, limit: int = 20) -> list[dict]:
    """Return structured purchase history per supplier for a part number.

    Each dict: supplier_id, supplier_name, contacts, price, date, doc_number, order_count, total_qty
    """
    from sqlalchemy import func, desc, and_

    session = get_session()
    try:
        products = session.query(Product).filter(
            Product.part_number.ilike(part_number)
        ).all()
        if not products:
            return []

        product_ids = [p.id for p in products]

        results = (
            session.query(
                Supplier.id.label("supplier_id"),
                Supplier.name.label("supplier_name"),
                Supplier.contacts.label("supplier_contacts"),
                func.max(ProductSupplier.date).label("most_recent_date"),
                func.sum(ProductSupplier.quantity).label("total_quantity"),
                func.count(ProductSupplier.id).label("order_count"),
            )
            .join(Product, ProductSupplier.product_id == Product.id)
            .join(Supplier, ProductSupplier.supplier_id == Supplier.id)
            .filter(ProductSupplier.product_id.in_(product_ids))
            .group_by(Supplier.id, Supplier.name, Supplier.contacts)
            .order_by(desc("order_count"))
            .limit(limit)
            .all()
        )

        out = []
        for row in results:
            # Get latest price and doc_number
            latest = (
                session.query(ProductSupplier.price, ProductSupplier.doc_number)
                .filter(
                    and_(
                        ProductSupplier.supplier_id == row.supplier_id,
                        ProductSupplier.product_id.in_(product_ids),
                    )
                )
                .order_by(ProductSupplier.date.desc().nulls_last())
                .first()
            )
            contacts = []
            if row.supplier_contacts and isinstance(row.supplier_contacts, list):
                contacts = row.supplier_contacts

            out.append({
                "supplier_id": str(row.supplier_id),
                "name": row.supplier_name,
                "contacts": contacts,
                "price": latest[0] if latest else None,
                "doc_number": latest[1] if latest else None,
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
            .filter(Brand.name.ilike(f"%{brand}%"), Brand.duplicate_of.is_(None))
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