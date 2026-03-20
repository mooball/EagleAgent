"""
Product and Procurement tools.

Provides simple interfaces for querying the products catalog, performing exact matches,
and running vector similarity searches using pgvector and Gemini.
"""

import logging
from typing import Optional
from langchain_core.tools import tool
from sqlalchemy import create_engine, or_
from sqlalchemy.orm import sessionmaker, aliased
from langchain_google_genai import GoogleGenerativeAIEmbeddings
import asyncio

from config.settings import Config
from includes.db_models import Product, Brand, Supplier, SupplierBrand

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
        _embeddings_model = GoogleGenerativeAIEmbeddings(model=embed_model_name, output_dimensionality=256)
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


def _do_supplier_search(name: Optional[str] = None,
                        brand: Optional[str] = None,
                        country: Optional[str] = None,
                        query: Optional[str] = None,
                        limit: int = 20) -> str:
    """Executes the supplier search synchronously."""
    session = get_session()
    try:
        base_query = session.query(Supplier)

        if name:
            base_query = base_query.filter(Supplier.name.ilike(f"%{name}%"))

        if country:
            base_query = base_query.filter(Supplier.country.ilike(f"%{country}%"))

        if brand:
            # Join through supplier_brands to brands, including duplicate resolution
            base_query = (
                base_query
                .join(SupplierBrand, Supplier.id == SupplierBrand.supplier_id)
                .join(Brand, SupplierBrand.brand_id == Brand.id)
                .filter(Brand.name.ilike(f"%{brand}%"))
            )

        results = []
        seen_ids = set()

        # Stage 1: string match on query (ilike across name, notes, city)
        if query:
            string_query = base_query.filter(
                or_(
                    Supplier.name.ilike(f"%{query}%"),
                    Supplier.notes.ilike(f"%{query}%"),
                    Supplier.city.ilike(f"%{query}%"),
                )
            )
            total = string_query.distinct().count()
            string_results = string_query.distinct().limit(limit).all()
            for r in string_results:
                if r.id not in seen_ids:
                    results.append(r)
                    seen_ids.add(r.id)
        else:
            total = base_query.distinct().count()
            results = base_query.distinct().limit(limit).all()
            for r in results:
                seen_ids.add(r.id)

        # Stage 2: vector fallback if query provided and we need more results
        if query and len(results) < limit:
            try:
                emb_model = get_embeddings_model()
                query_vector = emb_model.embed_query(query)
                vector_query = base_query.filter(
                    Supplier.embedding.isnot(None)
                ).order_by(
                    Supplier.embedding.cosine_distance(query_vector)
                )
                v_results = vector_query.limit(limit * 2).all()
                for r in v_results:
                    if r.id not in seen_ids:
                        results.append(r)
                        seen_ids.add(r.id)
                        if len(results) >= limit:
                            break
            except Exception as e:
                logger.error(f"Failed to compute embedding for supplier search: {e}")

        if not results:
            return "No suppliers found matching those criteria."

        # Fetch linked brand names for each supplier
        supplier_ids = [s.id for s in results]
        brand_links = (
            session.query(SupplierBrand.supplier_id, Brand.name)
            .join(Brand, SupplierBrand.brand_id == Brand.id)
            .filter(SupplierBrand.supplier_id.in_(supplier_ids))
            .all()
        )
        supplier_brands = {}
        for sid, bname in brand_links:
            supplier_brands.setdefault(sid, []).append(bname)

        output_parts = [f"Found {total} matching supplier(s). Displaying {len(results)}:"]
        for s in results:
            item = f"- {s.name}"
            if s.city or s.country:
                location = ", ".join(filter(None, [s.city, s.country]))
                item += f" | Location: {location}"
            if s.url:
                item += f" | URL: {s.url}"
            # Contacts summary
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
            # Brands
            brands = supplier_brands.get(s.id, [])
            if brands:
                item += f" | Brands: {', '.join(sorted(brands))}"
            output_parts.append(item)

        if total > limit:
            output_parts.append(f"\nNote: There are {total - limit} more unshown results. Ask the user if they'd like to see more or refine the search.")

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
                           limit: int = 20) -> str:
    """
    Search the suppliers database.

    You can search by:
    - name: partial match on the supplier name
    - brand: find suppliers that carry a specific brand
    - country: filter by country
    - query: text + semantic search across supplier name, notes, and city.
      Supports natural language descriptions (e.g. 'heavy-duty conveyor components',
      'industrial adhesives manufacturer'). String matches are tried first,
      then vector similarity on supplier notes fills remaining results.

    Provide as many arguments as needed to narrow results.

    Args:
        name: Supplier name to search for (e.g. 'Acme')
        brand: Brand name to filter by (e.g. 'Hilti') — finds suppliers linked to that brand
        country: Country to filter by (e.g. 'Australia')
        query: Text and semantic search across name, notes, and city. Accepts natural language descriptions.
        limit: Maximum number of results to return (default: 20)
    """
    return await asyncio.to_thread(_do_supplier_search, name, brand, country, query, limit)