"""
sync_netsuite_contacts.py

Syncs contact records from NetSuite into the local contacts table.
Uses the NetSuite REST API with SuiteQL queries and streaming pagination.

For each contact:
  - If it exists locally by netsuite_id: update changed fields
  - If new: insert a new contact record
  - Resolves customer_id from the contact's company field

Usage:
  uv run python -m scripts.sync_netsuite_contacts
  uv run python -m scripts.sync_netsuite_contacts --since 2026-04-01
  uv run python -m scripts.sync_netsuite_contacts --since "7 days"
  uv run python -m scripts.sync_netsuite_contacts --resume
  uv run python -m scripts.sync_netsuite_contacts --customer-ids "1,2,3"
  uv run python -m scripts.sync_netsuite_contacts --dry-run
"""

import argparse
from datetime import datetime, timedelta

from sqlalchemy import select, func
from sqlalchemy.orm import sessionmaker

from includes.dashboard.models import Contact, Customer
from includes.netsuite.client import NetSuiteClient
from includes.netsuite.queries import contacts_for_ids, contacts_updated_since
from includes.netsuite.sync_utils import parse_netsuite_date, parse_since, get_engine


def sync_contacts(since_date: str | None = None, customer_ids: list[str] | None = None, dry_run: bool = False):
    """Sync contacts from NetSuite.
    
    Args:
        since_date: ISO date string to query contacts modified on/after this date (if not using customer_ids)
        customer_ids: Optional list of customer NetSuite IDs to fetch contacts for
        dry_run: If True, log what would be synced without writing to DB
    """
    client = NetSuiteClient()
    engine = get_engine()
    batch_size = Config.NETSUITE_SYNC_BATCH_SIZE
    Session = sessionmaker(bind=engine)

    print(f"\n📥 Syncing contacts...")
    
    # Build query
    if customer_ids:
        # Fetch contacts for specific customers
        query = contacts_for_ids(customer_ids)
        print(f"   Fetching contacts for {len(customer_ids)} customer(s)...")
        if not query:
            print("   No customer IDs provided. Exiting.")
            return
    else:
        # Fetch all contacts modified since date
        query = contacts_updated_since(since_date)
        print(f"   Fetching contacts modified since {since_date}...")

    if dry_run:
        total = 0
        for page in client.suiteql_iter(query):
            total += len(page)
            print(f"   DRY RUN: fetched {total} contacts so far...")
        print(f"\n   DRY RUN: found {total} total contacts — no changes written.")
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

                    # Resolve customer_id from the contact's company field
                    customer_netsuite_id = str(row.get("company", "")).strip()
                    customer = None
                    if customer_netsuite_id:
                        customer = session.execute(
                            select(Customer).where(Customer.netsuite_id == customer_netsuite_id)
                        ).scalars().first()

                    contact_data = {
                        "firstname": row.get("firstname"),
                        "lastname": row.get("lastname"),
                        "fullname": f"{row.get('firstname', '')} {row.get('lastname', '')}".strip(),
                        "email": row.get("email"),
                        "phone": row.get("phone"),
                        "customer_id": customer.id if customer else None,
                        "isinactive": row.get("isinactive") == "T",
                        "netsuite_last_modified": parse_netsuite_date(row.get("lastmodifieddate")),
                    }

                    existing = session.execute(
                        select(Contact).where(Contact.netsuite_id == netsuite_id)
                    ).scalars().first()

                    if existing:
                        for key, value in contact_data.items():
                            setattr(existing, key, value)
                        updated += 1
                    else:
                        session.add(Contact(netsuite_id=netsuite_id, **contact_data))
                        inserted += 1

                except Exception as e:
                    session.rollback()
                    print(f"  ⚠ Error syncing contact {row.get('id')}: {e}")
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
        description="Sync contacts from NetSuite"
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Sync contacts modified on/after this date (ISO format: YYYY-MM-DD, or relative: '7 days'). Default: 30 days.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last synced position (uses MAX netsuite_last_modified from DB).",
    )
    parser.add_argument(
        "--customer-ids",
        type=str,
        default=None,
        help="Sync contacts for specific customer NetSuite IDs (comma-separated, e.g. '1,2,3'). If provided, --since is ignored.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be synced without writing to DB",
    )

    args = parser.parse_args()

    engine = get_engine()
    Session = sessionmaker(bind=engine)

    # Determine sync mode
    if args.customer_ids:
        customer_ids = [cid.strip() for cid in args.customer_ids.split(",") if cid.strip()]
        since_date = None
        mode = f"for {len(customer_ids)} customer(s)"
    elif args.resume:
        with Session() as session:
            max_date = session.query(func.max(Contact.netsuite_last_modified)).filter(
                Contact.netsuite_id.isnot(None)
            ).scalar()
        if max_date:
            since_date = max_date.strftime("%Y-%m-%d")
            print(f"Resuming from last synced date: {since_date}")
        else:
            since_date = "2014-01-01"
            print("No existing synced contacts found. Starting full sync from 2014-01-01.")
        customer_ids = None
        mode = f"since {since_date}"
    elif args.since:
        since_date = parse_since(args.since)
        customer_ids = None
        mode = f"since {since_date}"
    else:
        since_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        customer_ids = None
        mode = f"since {since_date}"

    print("\n🔄 NetSuite Contact Sync")
    print("=" * 80)
    print(f"Mode: {mode}")
    print(f"Dry run: {args.dry_run}")
    print("=" * 80)

    sync_contacts(since_date=since_date, customer_ids=customer_ids, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
