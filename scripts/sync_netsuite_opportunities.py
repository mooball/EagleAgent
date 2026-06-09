"""
sync_netsuite_opportunities.py

Syncs opportunity records from NetSuite into the local opportunities table.
Uses the NetSuite REST API with SuiteQL queries.

For each opportunity:
  - If it exists locally by netsuite_id: update changed fields
  - If new: insert a new opportunity record
  - Validates that customer_id exists before linking

Usage:
  uv run python -m scripts.sync_netsuite_opportunities
  uv run python -m scripts.sync_netsuite_opportunities --since 2026-04-01
  uv run python -m scripts.sync_netsuite_opportunities --since "7 days"
  uv run python -m scripts.sync_netsuite_opportunities --resume
  uv run python -m scripts.sync_netsuite_opportunities --dry-run
"""

import argparse
from datetime import datetime, timedelta

from sqlalchemy import select, func
from sqlalchemy.orm import sessionmaker

from includes.dashboard.models import Opportunity, Customer
from includes.netsuite.client import NetSuiteClient
from includes.netsuite.queries import opportunities_updated_since
from includes.netsuite.sync_utils import parse_netsuite_date, parse_since, get_engine


def sync_opportunities(since_date: str, dry_run: bool = False):
    """Sync opportunities from NetSuite.
    
    Args:
        since_date: ISO date string to query opportunities modified on/after this date
        dry_run: If True, log what would be synced without writing to DB
    """
    client = NetSuiteClient()
    engine = get_engine()
    batch_size = Config.NETSUITE_SYNC_BATCH_SIZE
    Session = sessionmaker(bind=engine)

    query = opportunities_updated_since(since_date)
    print(f"\n📥 Syncing opportunities since {since_date}...")

    if dry_run:
        total = 0
        for page in client.suiteql_iter(query):
            total += len(page)
            print(f"   DRY RUN: fetched {total} opportunities so far...")
        print(f"\n   DRY RUN: found {total} total opportunities — no changes written.")
        return

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
                try:
                    netsuite_id = str(row.get("id", "")).strip()
                    if not netsuite_id:
                        skipped += 1
                        processed += 1
                        continue

                    customer_netsuite_id = str(row.get("entity", "")).strip()
                    customer = None
                    if customer_netsuite_id:
                        customer = session.execute(
                            select(Customer).where(Customer.netsuite_id == customer_netsuite_id)
                        ).scalars().first()

                    opp_data = {
                        "opportunity_number": row.get("tranid"),
                        "title": row.get("title"),
                        "status": row.get("status"),
                        "total": row.get("total"),
                        "currency": row.get("currency"),
                        "netsuite_customer_id": customer_netsuite_id,
                        "customer_id": customer.id if customer else None,
                        "netsuite_salesrep_id": str(row.get("salesrep", "")).strip() if row.get("salesrep") else None,
                        "netsuite_last_modified": parse_netsuite_date(row.get("lastmodifieddate")),
                    }

                    existing = session.execute(
                        select(Opportunity).where(Opportunity.netsuite_id == netsuite_id)
                    ).scalars().first()

                    if existing:
                        for key, value in opp_data.items():
                            setattr(existing, key, value)
                        updated += 1
                    else:
                        session.add(Opportunity(netsuite_id=netsuite_id, **opp_data))
                        inserted += 1

                except Exception as e:
                    session.rollback()
                    print(f"  ⚠ Error syncing opportunity {row.get('id')}: {e}")
                    skipped += 1

                processed += 1
                if processed % batch_size == 0:
                    session.commit()
                    print(f"  Committed batch ({processed} processed, {fetched} fetched)...")

        session.commit()

    print(f"\n✅ Sync complete: {inserted} inserted, {updated} updated, {skipped} skipped.")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Sync opportunities from NetSuite"
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Sync opportunities modified on/after this date (ISO format: YYYY-MM-DD, or relative: '7 days')",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last synced position (uses MAX netsuite_last_modified from DB).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be synced without writing to DB",
    )

    args = parser.parse_args()

    engine = get_engine()
    Session = sessionmaker(bind=engine)

    if args.resume:
        with Session() as session:
            max_date = session.query(func.max(Opportunity.netsuite_last_modified)).filter(
                Opportunity.netsuite_id.isnot(None)
            ).scalar()
        if max_date:
            since = max_date.strftime("%Y-%m-%d")
            print(f"Resuming from last synced date: {since}")
        else:
            since = "2014-01-01"
            print("No existing synced opportunities found. Starting full sync from 2014-01-01.")
    elif args.since:
        since = parse_since(args.since)
    else:
        since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    print("\n🔄 NetSuite Opportunity Sync")
    print("=" * 80)
    print(f"Date: {since}")
    print(f"Dry run: {args.dry_run}")
    print("=" * 80)

    sync_opportunities(since, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
