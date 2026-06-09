"""
sync_netsuite_customers.py

Syncs customer records from NetSuite into the local customers table.
Uses the NetSuite REST API with SuiteQL queries.

For each customer:
  - If it exists locally by netsuite_id: update changed fields
  - If new: insert a new customer record
  - Expands contactlist (comma-separated contact IDs) and triggers contact sync

Usage:
  uv run python -m scripts.sync_netsuite_customers
  uv run python -m scripts.sync_netsuite_customers --since 2026-04-01
  uv run python -m scripts.sync_netsuite_customers --since "7 days"
  uv run python -m scripts.sync_netsuite_customers --resume
  uv run python -m scripts.sync_netsuite_customers --dry-run
"""

import argparse
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from config.settings import Config
from includes.dashboard.models import Customer
from includes.netsuite.client import NetSuiteClient
from includes.netsuite.queries import customers_updated_since, contacts_for_ids

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


def parse_netsuite_date(date_str: str | None) -> datetime | None:
    """Parse NetSuite date format (d/m/yyyy) to a datetime."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%d/%m/%Y")
    except ValueError:
        return None


def parse_since(value: str) -> str:
    """Parse a --since value into an ISO date string (YYYY-MM-DD)."""
    match = re.match(r"^(\d+)\s*d(?:ays?)?$", value.strip(), re.IGNORECASE)
    if match:
        days = int(match.group(1))
        return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return value


def get_engine():
    """Get database engine."""
    db_url = Config.DATABASE_URL
    if not db_url:
        raise ValueError("DATABASE_URL is empty. Check your `.env` settings.")

    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return create_engine(db_url)


def parse_contact_list(contactlist_str: str) -> list[str]:
    """Parse comma-separated contact IDs from NetSuite contactlist field.
    
    Args:
        contactlist_str: e.g. "5, 76893, 124327"
    
    Returns:
        List of contact IDs as strings, with whitespace trimmed
    """
    if not contactlist_str:
        return []
    return [cid.strip() for cid in contactlist_str.split(",") if cid.strip()]


def sync_customers(since_date: str, dry_run: bool = False, sync_contacts: bool = True):
    """Sync customers from NetSuite.
    
    Args:
        since_date: ISO date string to query customers modified on/after this date
        dry_run: If True, log what would be synced without writing to DB
        sync_contacts: If True, also sync contacts for each customer
    """
    client = NetSuiteClient()
    engine = get_engine()
    batch_size = Config.NETSUITE_SYNC_BATCH_SIZE
    Session = sessionmaker(bind=engine)

    query = customers_updated_since(since_date)
    print(f"\n📥 Syncing customers since {since_date}...")

    if dry_run:
        total = 0
        for page in client.suiteql_iter(query):
            total += len(page)
            print(f"   DRY RUN: fetched {total} customers so far...")
        print(f"\n   DRY RUN: found {total} total customers — no changes written.")
        return

    all_contact_ids = []
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

                    # Collect contact IDs for later sync
                    contact_ids = parse_contact_list(row.get("contactlist", ""))
                    if contact_ids:
                        all_contact_ids.extend(contact_ids)

                    cust_data = {
                        "entity_code": row.get("entityid"),
                        "companyname": row.get("companyname") or row.get("fullname"),
                        "fullname": row.get("fullname"),
                        "email": row.get("email"),
                        "phone": row.get("phone"),
                        "isinactive": row.get("isinactive") == "T",
                        "currency": row.get("currency"),
                        "netsuite_salesrep_id": str(row.get("salesrep", "")).strip() if row.get("salesrep") else None,
                        "netsuite_last_modified": parse_netsuite_date(row.get("lastmodifieddate")),
                    }

                    existing = session.execute(
                        select(Customer).where(Customer.netsuite_id == netsuite_id)
                    ).scalars().first()

                    if existing:
                        for key, value in cust_data.items():
                            setattr(existing, key, value)
                        updated += 1
                    else:
                        session.add(Customer(netsuite_id=netsuite_id, **cust_data))
                        inserted += 1

                except Exception as e:
                    logger.error(f"Error syncing customer {row.get('id')}: {e}")
                    skipped += 1

                processed += 1
                if processed % batch_size == 0:
                    session.commit()
                    print(f"  Committed batch ({processed} processed, {fetched} fetched)...")

        session.commit()
        print(f"\n✅ Customers sync complete: {inserted} inserted, {updated} updated, {skipped} skipped.")

        if sync_contacts and all_contact_ids:
            print(f"\n📥 Syncing {len(set(all_contact_ids))} unique contacts...")
            sync_contacts_for_ids(session, client, list(set(all_contact_ids)))


def sync_contacts_for_ids(session, client: NetSuiteClient, contact_ids: list[str]):
    """Sync specific contacts by ID.
    
    Args:
        session: SQLAlchemy session
        client: NetSuite API client
        contact_ids: List of NetSuite contact IDs to sync
    """
    from includes.dashboard.models import Contact
    
    if not contact_ids:
        return
    
    try:
        query = contacts_for_ids(contact_ids)
        if not query:
            return

        items = client.suiteql(query)
        items = [{k: v for k, v in row.items() if k != "links"} for row in items]
        print(f"   Found {len(items)} contact records")
        
        inserted = 0
        updated = 0
        
        for row in items:
            try:
                netsuite_id = str(row.get("id", "")).strip()
                if not netsuite_id:
                    continue
                
                # Get customer ID
                customer_netsuite_id = str(row.get("company", "")).strip()
                customer = None
                if customer_netsuite_id:
                    customer = session.execute(
                        select(Customer).where(Customer.netsuite_id == customer_netsuite_id)
                    ).scalars().first()
                
                # Look up existing contact
                existing = session.execute(
                    select(Contact).where(Contact.netsuite_id == netsuite_id)
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
                
                if existing:
                    for key, value in contact_data.items():
                        setattr(existing, key, value)
                    updated += 1
                else:
                    new_contact = Contact(netsuite_id=netsuite_id, **contact_data)
                    session.add(new_contact)
                    inserted += 1
            
            except Exception as e:
                logger.error(f"Error syncing contact {row.get('id')}: {e}")
        
        session.commit()
        print(f"   Contacts: {inserted} inserted, {updated} updated")
    
    except Exception as e:
        logger.error(f"Error syncing contacts: {e}", exc_info=True)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Sync customers from NetSuite"
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Sync customers modified on/after this date (ISO format: YYYY-MM-DD, or relative: '7 days')",
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
    parser.add_argument(
        "--no-contacts",
        action="store_true",
        help="Skip contact expansion and sync",
    )

    args = parser.parse_args()

    engine = get_engine()
    Session = sessionmaker(bind=engine)

    if args.resume:
        with Session() as session:
            from sqlalchemy import func
            max_date = session.query(func.max(Customer.netsuite_last_modified)).filter(
                Customer.netsuite_id.isnot(None)
            ).scalar()
        if max_date:
            since = max_date.strftime("%Y-%m-%d")
            print(f"Resuming from last synced date: {since}")
        else:
            since = "2014-01-01"
            print("No existing synced customers found. Starting full sync from 2014-01-01.")
    elif args.since:
        since = parse_since(args.since)
    else:
        since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    print("\n🔄 NetSuite Customer Sync")
    print("=" * 80)
    print(f"Date: {since}")
    print(f"Dry run: {args.dry_run}")
    print(f"Sync contacts: {not args.no_contacts}")
    print("=" * 80)

    sync_customers(since, dry_run=args.dry_run, sync_contacts=not args.no_contacts)


if __name__ == "__main__":
    main()
