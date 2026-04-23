"""
extract_top_suppliers.py

Extracts the top N suppliers by purchase/order transaction count and writes
them to a JSON file for use by the categorization script.

Usage:
  uv run python -m scripts.extract_top_suppliers
  uv run python -m scripts.extract_top_suppliers --production --limit 100
"""

import os
import json
import argparse
from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import create_engine, func, desc
from sqlalchemy.orm import sessionmaker

from config.settings import Config
from includes.db_models import Supplier, ProductSupplier


def get_engine(is_prod: bool = False):
    db_url = Config.PROD_DATABASE_URL if is_prod else Config.DATABASE_URL
    if not db_url:
        raise ValueError("Database URL is empty. Check your .env settings.")
    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
    elif db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1)
    return create_engine(db_url, pool_size=2, max_overflow=0, pool_pre_ping=True)


def main():
    parser = argparse.ArgumentParser(description="Extract top suppliers by purchase transaction count.")
    parser.add_argument("--production", action="store_true", help="Query the PRODUCTION database.")
    parser.add_argument("--limit", type=int, default=50, help="Number of top suppliers to extract (default: 50).")
    parser.add_argument("--output", type=str, default="data/top_50_suppliers.json", help="Output JSON file path.")
    args = parser.parse_args()

    engine = get_engine(args.production)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        rows = (
            session.query(
                Supplier.id,
                Supplier.name,
                Supplier.url,
                Supplier.city,
                Supplier.country,
                func.count(ProductSupplier.id).label("purchase_count"),
                func.max(ProductSupplier.date).label("last_purchase_date"),
            )
            .outerjoin(ProductSupplier, Supplier.id == ProductSupplier.supplier_id)
            .group_by(Supplier.id, Supplier.name, Supplier.url, Supplier.city, Supplier.country)
            .order_by(desc("purchase_count"))
            .limit(args.limit)
            .all()
        )

        suppliers = []
        for row in rows:
            suppliers.append({
                "id": str(row.id),
                "name": row.name,
                "url": row.url,
                "city": row.city,
                "country": row.country,
                "purchase_count": row.purchase_count,
                "last_purchase_date": row.last_purchase_date.isoformat() if row.last_purchase_date else None,
            })

        os.makedirs(os.path.dirname(args.output), exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(suppliers, f, indent=2)

        print(f"Extracted {len(suppliers)} suppliers to {args.output}")
        print(f"Top 5:")
        for s in suppliers[:5]:
            print(f"  {s['name']} — {s['purchase_count']} purchases, URL: {s['url'] or 'N/A'}")

    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    main()
