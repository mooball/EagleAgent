"""
sync_netsuite_products.py

Syncs inventory item records from NetSuite into the local products table.
Uses the NetSuite REST API with SuiteQL queries and streaming pagination.

For each item:
  - If it exists locally by netsuite_id: update changed fields
  - If new: insert a new product record
  - EagleAgent-only fields (embedding) are never overwritten

Usage:
  uv run python -m scripts.sync_netsuite_products
  uv run python -m scripts.sync_netsuite_products --since 2026-04-01
  uv run python -m scripts.sync_netsuite_products --since 7d
  uv run python -m scripts.sync_netsuite_products --resume
  uv run python -m scripts.sync_netsuite_products --dry-run
"""

import argparse
import re
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config.settings import Config
from includes.dashboard.models import Product
from includes.netsuite.client import NetSuiteClient
from includes.netsuite.queries import products_updated_since


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
    return value


def get_engine():
    db_url = Config.DATABASE_URL
    if not db_url:
        raise ValueError("DATABASE_URL is empty. Check your `.env` settings.")

    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return create_engine(db_url)


def parse_netsuite_date(date_str: str | None) -> datetime | None:
    """Parse NetSuite date format (d/m/yyyy) to a datetime."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%d/%m/%Y")
    except ValueError:
        return None


def safe_float(value) -> float | None:
    """Convert to float, returning None for missing or zero values."""
    if value is None:
        return None
    try:
        f = float(value)
        return f if f != 0 else None
    except (ValueError, TypeError):
        return None


def map_item_to_product(row: dict) -> dict:
    """Map a NetSuite item row to product column values."""
    description = row.get("description")
    if description and isinstance(description, str):
        description = description.strip()

    return {
        "netsuite_id": str(row.get("id", "")).strip(),
        "part_number": (row.get("itemid") or "").strip(),
        "description": description or None,
        "brand": row.get("brand_name"),
        "weight_kg": safe_float(row.get("weight")),
        "netsuite_last_modified": parse_netsuite_date(row.get("lastmodifieddate")),
    }


# Fields that NetSuite owns — these get overwritten on sync
NETSUITE_OWNED_FIELDS = {
    "part_number", "description", "brand", "weight_kg", "netsuite_last_modified",
}


def main():
    parser = argparse.ArgumentParser(description="Sync products from NetSuite API into the database.")
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Only sync items modified since this date (YYYY-MM-DD or 'Nd'). Default: 30 days.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last synced position (uses MAX netsuite_last_modified from DB).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch and display changes without writing to DB.")
    args = parser.parse_args()

    # Determine the since_date
    engine = get_engine()
    Session = sessionmaker(bind=engine)

    if args.resume:
        with Session() as session:
            from sqlalchemy import func
            max_date = session.query(func.max(Product.netsuite_last_modified)).filter(
                Product.netsuite_id.isnot(None)
            ).scalar()
        if max_date:
            since_date = max_date.strftime("%Y-%m-%d")
            print(f"Resuming from last synced date: {since_date}")
        else:
            since_date = "2014-01-01"
            print("No existing synced products found. Starting full sync from 2014-01-01.")
    elif args.since:
        since_date = parse_since(args.since)
    else:
        since_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    batch_size = Config.NETSUITE_SYNC_BATCH_SIZE

    # Connect to NetSuite
    print("Connecting to NetSuite...")
    client = NetSuiteClient()
    query = products_updated_since(since_date)
    print(f"Fetching items modified since {since_date} (sorted oldest first)...")

    if args.dry_run:
        rows = client.suiteql(query)
        rows = [{k: v for k, v in row.items() if k != "links"} for row in rows]
        print(f"Fetched {len(rows)} item records.\n")
        print("DRY RUN — first 10 items:")
        for row in rows[:10]:
            mapped = map_item_to_product(row)
            print(f"  [{mapped['netsuite_id']}] {mapped['part_number']} — {mapped['brand']}")
        if len(rows) > 10:
            print(f"  ... and {len(rows) - 10} more")
        return

    # Sync using streaming pagination + batch commits
    inserted = 0
    updated = 0
    skipped = 0
    processed = 0
    fetched = 0

    with Session() as session:
        for page in client.suiteql_iter(query):
            page = [{k: v for k, v in row.items() if k != "links"} for row in page]
            fetched += len(page)

            for row in page:
                mapped = map_item_to_product(row)
                netsuite_id = mapped["netsuite_id"]

                if not netsuite_id:
                    skipped += 1
                    processed += 1
                    continue

                # Look up existing product by netsuite_id
                existing = session.query(Product).filter(Product.netsuite_id == netsuite_id).first()

                if existing:
                    # Update only NetSuite-owned fields that have changed
                    changed = False
                    for field in NETSUITE_OWNED_FIELDS:
                        new_value = mapped[field]
                        old_value = getattr(existing, field)

                        if new_value != old_value:
                            setattr(existing, field, new_value)
                            changed = True

                    if changed:
                        updated += 1
                    else:
                        skipped += 1
                else:
                    # Insert new product
                    product = Product(
                        netsuite_id=netsuite_id,
                        part_number=mapped["part_number"],
                        description=mapped["description"],
                        brand=mapped["brand"],
                        weight_kg=mapped["weight_kg"],
                        netsuite_last_modified=mapped["netsuite_last_modified"],
                    )
                    session.add(product)
                    inserted += 1

                processed += 1

                # Batch commit
                if processed % batch_size == 0:
                    session.commit()
                    print(f"  Committed batch ({processed} rows processed, {fetched} fetched)...")

        # Final commit for remaining rows
        session.commit()

    print(f"\nSync complete: {inserted} inserted, {updated} updated, {skipped} unchanged.")

    # Summary
    with Session() as session:
        total = session.query(Product).count()
        print(f"Total products in database: {total}")


if __name__ == "__main__":
    main()
