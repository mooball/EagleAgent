"""
sync_netsuite_brands.py

Syncs brand records from NetSuite (custom record type 165) into the local
brands table. Uses the NetSuite REST API rather than CSV imports.

For each brand:
  - If it exists locally by netsuite_id: update name and last_modified if changed
  - If new: insert with netsuite_id, name, and last_modified
  - If a local brand matches by name but has no netsuite_id: backfill the ID

Usage:
  uv run python -m scripts.sync_netsuite_brands
  uv run python -m scripts.sync_netsuite_brands --resume
  uv run python -m scripts.sync_netsuite_brands --since 2026-04-01
  uv run python -m scripts.sync_netsuite_brands --since "7 days"
  uv run python -m scripts.sync_netsuite_brands --dry-run
"""

import argparse
import re
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import insert

from config.settings import Config
from includes.dashboard.models import Brand
from includes.netsuite.client import NetSuiteClient
from includes.netsuite.queries import all_brands


# Regex whitelist: keep standard Latin letters (including accented), digits, and common punctuation
ALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9\s\-&.'\/,()#+À-ÖØ-öø-ÿ]")


def clean_brand_name(name: str) -> str:
    """Clean a brand name: strip, collapse whitespace, remove non-standard characters."""
    if not name or not isinstance(name, str):
        return name
    name = name.strip()
    name = ALLOWED_CHARS.sub("", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip()


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
    return value


def parse_netsuite_date(date_str: str | None) -> datetime | None:
    """Parse NetSuite date format (d/m/yyyy) to a datetime."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%d/%m/%Y")
    except ValueError:
        return None


def main():
    parser = argparse.ArgumentParser(description="Sync brands from NetSuite API into the database.")
    parser.add_argument(
        "--since", type=str, default=None,
        help="Only sync brands modified since this date (YYYY-MM-DD or 'N days'). Default: full sync.",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume from last synced position (uses MAX netsuite_last_modified from DB).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch and display brands without writing to DB.")
    args = parser.parse_args()

    # Determine the since_date
    engine = get_engine()
    Session = sessionmaker(bind=engine)

    if args.resume:
        with Session() as session:
            from sqlalchemy import func
            max_date = session.query(func.max(Brand.netsuite_last_modified)).filter(
                Brand.netsuite_id.isnot(None)
            ).scalar()
        if max_date:
            since_date = max_date.strftime("%Y-%m-%d")
            print(f"Resuming from last synced date: {since_date}")
        else:
            since_date = "2014-01-01"
            print("No existing synced brands found. Starting full sync from 2014-01-01.")
    elif args.since:
        since_date = parse_since(args.since)
    else:
        since_date = None  # full sync
    batch_size = Config.NETSUITE_SYNC_BATCH_SIZE

    # Connect to NetSuite
    print("Connecting to NetSuite...")
    client = NetSuiteClient()
    query = all_brands(since_date=since_date)
    label = f" (modified since {since_date})" if since_date else ""
    print(f"Fetching brands from NetSuite{label}...")

    if args.dry_run:
        rows = client.suiteql(query)
        print(f"Fetched {len(rows)} brands.\n")
        print("DRY RUN — first 20 brands:")
        for row in rows[:20]:
            netsuite_id = str(row.get("id", "")).strip()
            name = clean_brand_name(str(row.get("name", "")).strip())
            print(f"  [{netsuite_id}] {name}")
        if len(rows) > 20:
            print(f"  ... and {len(rows) - 20} more")
        return

    # Connect to database
    print("Connecting to database...")
    engine = get_engine()
    Session = sessionmaker(bind=engine)

    with Session() as session:
        # First pass: backfill netsuite_id for brands that match by name but lack an ID
        # We need a full fetch for the name-matching pass, so collect all records via streaming
        all_records = []
        fetched = 0

        for page in client.suiteql_iter(query):
            fetched += len(page)
            for row in page:
                netsuite_id = str(row.get("id", "")).strip()
                name = str(row.get("name", "")).strip()
                last_modified = parse_netsuite_date(row.get("lastmodified"))

                if not netsuite_id:
                    continue

                cleaned_name = clean_brand_name(name)
                if not cleaned_name:
                    continue

                all_records.append({
                    "netsuite_id": netsuite_id,
                    "name": cleaned_name,
                    "netsuite_last_modified": last_modified,
                    "isinactive": row.get("isinactive") == "T",
                })

        skipped = fetched - len(all_records)
        print(f"Fetched {fetched} brands from NetSuite, prepared {len(all_records)} valid records ({skipped} skipped).\n")

        if not all_records:
            print("No valid brands to sync. Exiting.")
            return

        # Backfill pass
        existing_brands = session.query(Brand).filter(Brand.netsuite_id.is_(None)).all()
        name_to_netsuite_id = {r["name"].lower(): r["netsuite_id"] for r in all_records}
        backfilled = 0
        for brand in existing_brands:
            if brand.name and brand.name.lower() in name_to_netsuite_id:
                brand.netsuite_id = name_to_netsuite_id[brand.name.lower()]
                backfilled += 1
        if backfilled:
            session.commit()
            print(f"Backfilled netsuite_id on {backfilled} existing brands.\n")

        # Second pass: upsert all brands in batches
        inserted = 0
        updated = 0
        processed = 0

        for i in range(0, len(all_records), batch_size):
            batch = all_records[i : i + batch_size]

            # Count existing for summary
            netsuite_ids = [r["netsuite_id"] for r in batch]
            existing_ids = {
                b.netsuite_id
                for b in session.query(Brand.netsuite_id)
                .filter(Brand.netsuite_id.in_(netsuite_ids))
                .all()
            }

            for record in batch:
                if record["netsuite_id"] in existing_ids:
                    updated += 1
                else:
                    inserted += 1

            stmt = insert(Brand).values(batch)
            stmt = stmt.on_conflict_do_update(
                index_elements=["netsuite_id"],
                set_={
                    "name": stmt.excluded.name,
                    "netsuite_last_modified": stmt.excluded.netsuite_last_modified,
                    "isinactive": stmt.excluded.isinactive,
                },
            )
            session.execute(stmt)
            session.commit()
            processed += len(batch)

            if processed % (batch_size * 5) == 0:
                print(f"  Committed {processed} brands...")

        print(f"\nSync complete: {inserted} inserted, {updated} updated, {skipped} skipped.")

        # Summary: total brands in DB now
        total = session.query(Brand).count()
        print(f"Total brands in database: {total}")

    engine.dispose()


if __name__ == "__main__":
    main()
