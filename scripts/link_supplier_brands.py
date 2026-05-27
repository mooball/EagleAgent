"""
link_supplier_brands.py

Scans transactions to discover which brands are purchased from which suppliers,
then ensures each discovered (supplier, brand) pair is linked in the
supplier_brands join table.

Logic:
  Transaction → product_id → Product.brand_id (FK) → Brand
  Fallback: Transaction → product_id → Product.brand (string) → Brand.name
  Transaction → supplier_id → Supplier

For each unique (supplier_id, brand_id) pair found in transactions, insert
into supplier_brands if not already present.

Usage:
  uv run python -m scripts.link_supplier_brands
  uv run python -m scripts.link_supplier_brands --since 7d
  uv run python -m scripts.link_supplier_brands --since 2026-05-01
  uv run python -m scripts.link_supplier_brands --dry-run
"""

import argparse
import re
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import insert

from config.settings import Config
from includes.dashboard.models import SupplierBrand


def get_engine():
    db_url = Config.DATABASE_URL
    if not db_url:
        raise ValueError("DATABASE_URL is empty. Check your `.env` settings.")

    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return create_engine(db_url)


def parse_since(value: str) -> str:
    """Parse a --since value into an ISO date string (YYYY-MM-DD).

    Accepts either:
      - An ISO date: '2026-04-01'
      - A relative period: '7 days', '7days', '7d'
    """
    match = re.match(r"^(\d+)\s*d(?:ays?)?$", value.strip(), re.IGNORECASE)
    if match:
        days = int(match.group(1))
        return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    # Try ISO date
    datetime.strptime(value, "%Y-%m-%d")
    return value


def find_missing_links(session, since_date=None):
    """Find (supplier_id, brand_id) pairs in transactions not yet in supplier_brands.
    
    Returns list of dicts with supplier_id, brand_id, supplier_name, brand_name.
    Uses product.brand_id FK where available, falls back to name matching.
    """
    where_clause = ""
    params = {}
    if since_date:
        where_clause = 'AND t.date >= :since_date'
        params["since_date"] = since_date

    query = text(f"""
        SELECT DISTINCT
            t.supplier_id,
            COALESCE(p.brand_id, b_name.id) AS brand_id,
            s.name AS supplier_name,
            COALESCE(b_fk.name, b_name.name) AS brand_name
        FROM product_suppliers t
        JOIN products p ON p.id = t.product_id
        JOIN suppliers s ON s.id = t.supplier_id
        LEFT JOIN brands b_fk ON b_fk.id = p.brand_id AND b_fk.duplicate_of IS NULL
        LEFT JOIN brands b_name ON LOWER(b_name.name) = LOWER(p.brand) AND b_name.duplicate_of IS NULL AND p.brand_id IS NULL
        WHERE (p.brand_id IS NOT NULL OR p.brand IS NOT NULL)
          {where_clause}
          AND COALESCE(p.brand_id, b_name.id) IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM supplier_brands sb
              WHERE sb.supplier_id = t.supplier_id
                AND sb.brand_id = COALESCE(p.brand_id, b_name.id)
          )
    """)

    result = session.execute(query, params)
    return [dict(row._mapping) for row in result]


def main():
    parser = argparse.ArgumentParser(description="Link suppliers to brands based on transaction history")
    parser.add_argument("--since", type=str, default=None,
                        help="Only scan transactions from this date (ISO date or relative like '7d')")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be linked without making changes")
    args = parser.parse_args()

    since_date = None
    if args.since:
        since_date = parse_since(args.since)
        print(f"Scanning transactions since {since_date}")
    else:
        print("Scanning all transactions")

    engine = get_engine()
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        missing = find_missing_links(session, since_date)

        if not missing:
            print("No new supplier-brand links to create.")
            return

        print(f"Found {len(missing)} new supplier-brand links:")
        for link in missing:
            print(f"  {link['supplier_name']} → {link['brand_name']}")

        if args.dry_run:
            print(f"\n[DRY RUN] Would insert {len(missing)} links.")
            return

        # Bulk upsert (ON CONFLICT DO NOTHING for safety)
        stmt = insert(SupplierBrand).values([
            {"supplier_id": link["supplier_id"], "brand_id": link["brand_id"]}
            for link in missing
        ]).on_conflict_do_nothing(constraint="uq_supplier_brand")

        result = session.execute(stmt)
        session.commit()
        inserted = result.rowcount if result.rowcount >= 0 else len(missing)
        print(f"\nInserted {inserted} new supplier-brand links.")

    finally:
        session.close()


if __name__ == "__main__":
    main()
