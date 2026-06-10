"""
sync_netsuite_sales_orders.py

Syncs Sales Order line items from NetSuite into the local transactions table.
Uses streaming pagination with batch commits and ASC sort for resumability.

For each line item:
  - If it exists locally by netsuite_id (tl.uniquekey): update changed fields
  - If new: insert a new Transaction record with doc_type='SalesOrder'
  - Lines without a matching product or supplier are skipped (logged)

Lesson learned: Sort ASC by lastmodifieddate so --resume always makes forward
progress. If a session breaks mid-way, re-running with --resume picks up from
the last committed batch without re-processing earlier records.

Usage:
  uv run python -m scripts.sync_netsuite_sales_orders
  uv run python -m scripts.sync_netsuite_sales_orders --since 2026-04-01
  uv run python -m scripts.sync_netsuite_sales_orders --since 7d
  uv run python -m scripts.sync_netsuite_sales_orders --resume
  uv run python -m scripts.sync_netsuite_sales_orders --dry-run
"""

import argparse
import re
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config.settings import Config
from includes.dashboard.models import Opportunity, Product, Supplier, Transaction
from includes.netsuite.client import NetSuiteClient
from includes.netsuite.queries import sales_orders_updated_since
from includes.netsuite.sync_utils import normalize_currency


DOC_TYPE = "SalesOrder"


def parse_since(value: str) -> str:
    """Parse a --since value into an ISO date string (YYYY-MM-DD)."""
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


def build_lookup_maps(session):
    """Build netsuite_id -> local UUID lookup maps for products, suppliers, and opportunities."""
    product_map = {}
    for nid, pid in session.query(Product.netsuite_id, Product.id).filter(
        Product.netsuite_id.isnot(None)
    ):
        product_map[str(nid)] = pid

    supplier_map = {}
    for nid, sid in session.query(Supplier.netsuite_id, Supplier.id).filter(
        Supplier.netsuite_id.isnot(None)
    ):
        supplier_map[str(nid)] = sid

    opportunity_map = {}
    for nid, oid in session.query(Opportunity.netsuite_id, Opportunity.id).filter(
        Opportunity.netsuite_id.isnot(None)
    ):
        opportunity_map[str(nid)] = oid

    return product_map, supplier_map, opportunity_map


def map_line_to_transaction(row: dict, product_map: dict, supplier_map: dict, opportunity_map: dict) -> dict | None:
    """Map a NetSuite transaction line row to Transaction column values.

    Returns None if the product or supplier can't be resolved locally.
    """
    item_id = str(row.get("item") or "").strip()
    vendor_id = str(row.get("custcol_po_vendor") or "").strip()

    product_uuid = product_map.get(item_id)
    supplier_uuid = supplier_map.get(vendor_id)

    if not product_uuid or not supplier_uuid:
        return None

    quantity = row.get("quantity")
    if quantity is not None:
        try:
            quantity = abs(float(quantity))
        except (ValueError, TypeError):
            quantity = None

    rate = row.get("rate")
    if rate is not None:
        try:
            rate = float(rate)
        except (ValueError, TypeError):
            rate = None

    cost = row.get("custcol_po_rate")
    if cost is not None:
        try:
            cost = float(cost)
        except (ValueError, TypeError):
            cost = None

    # Opportunity link
    opp_ns_id = str(row.get("opportunity") or "").strip() or None
    opportunity_uuid = opportunity_map.get(opp_ns_id) if opp_ns_id else None

    return {
        "netsuite_id": str(row.get("uniquekey", "")).strip(),
        "doc_type": DOC_TYPE,
        "doc_number": (row.get("tranid") or "").strip(),
        "date": parse_netsuite_date(row.get("trandate")),
        "product_id": product_uuid,
        "supplier_id": supplier_uuid,
        "quantity": quantity,
        "price": rate,
        "cost": cost,
        "cost_currency": normalize_currency(row.get("currency_name")),
        "status": (row.get("status") or "").strip() or None,
        "netsuite_last_modified": parse_netsuite_date(row.get("lastmodifieddate")),
        "netsuite_opportunity_id": opp_ns_id,
        "opportunity_id": opportunity_uuid,
    }


# Fields that get overwritten on sync (netsuite_id is the upsert key)
SYNC_FIELDS = {
    "doc_number", "date", "product_id", "supplier_id",
    "quantity", "price", "cost", "cost_currency", "status",
    "netsuite_last_modified", "netsuite_opportunity_id", "opportunity_id",
}


def main():
    parser = argparse.ArgumentParser(description="Sync Sales Order lines from NetSuite into the database.")
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Only sync lines modified since this date (YYYY-MM-DD or 'Nd'). Default: 30 days.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last synced position (uses MAX netsuite_last_modified for SalesOrder rows).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch and display changes without writing to DB.")
    args = parser.parse_args()

    engine = get_engine()
    Session = sessionmaker(bind=engine)

    # Determine the since_date
    if args.resume:
        with Session() as session:
            from sqlalchemy import func
            max_date = session.query(func.max(Transaction.netsuite_last_modified)).filter(
                Transaction.doc_type == DOC_TYPE,
                Transaction.netsuite_id.isnot(None),
            ).scalar()
        if max_date:
            since_date = max_date.strftime("%Y-%m-%d")
            print(f"Resuming from last synced date: {since_date}")
        else:
            since_date = "2014-01-01"
            print("No existing SalesOrder transactions found. Starting full sync from 2014-01-01.")
    elif args.since:
        since_date = parse_since(args.since)
    else:
        since_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    batch_size = Config.NETSUITE_SYNC_BATCH_SIZE

    # Build lookup maps
    print("Loading product, supplier, and opportunity lookup maps...")
    with Session() as session:
        product_map, supplier_map, opportunity_map = build_lookup_maps(session)
    print(f"  {len(product_map)} products, {len(supplier_map)} suppliers, {len(opportunity_map)} opportunities in lookup maps.")

    # Connect to NetSuite
    print("Connecting to NetSuite...")
    client = NetSuiteClient()
    query = sales_orders_updated_since(since_date)
    print(f"Fetching Sales Order lines modified since {since_date} (sorted oldest first)...")

    if args.dry_run:
        rows = client.suiteql(query)
        rows = [{k: v for k, v in row.items() if k != "links"} for row in rows]
        print(f"Fetched {len(rows)} line items.\n")
        print("DRY RUN — first 10 lines:")
        for row in rows[:10]:
            print(f"  [{row.get('uniquekey')}] {row.get('tranid')} — {row.get('item_name')} — vendor: {row.get('vendor_name')}")
        if len(rows) > 10:
            print(f"  ... and {len(rows) - 10} more")
        return

    # Sync using streaming pagination + batch commits
    inserted = 0
    updated = 0
    skipped = 0
    unresolved = 0
    processed = 0
    fetched = 0

    with Session() as session:
        for page in client.suiteql_iter(query):
            page = [{k: v for k, v in row.items() if k != "links"} for row in page]
            fetched += len(page)

            for row in page:
                mapped = map_line_to_transaction(row, product_map, supplier_map, opportunity_map)

                if mapped is None:
                    unresolved += 1
                    processed += 1
                    continue

                netsuite_id = mapped["netsuite_id"]
                if not netsuite_id:
                    skipped += 1
                    processed += 1
                    continue

                # Upsert by netsuite_id
                existing = session.query(Transaction).filter(
                    Transaction.netsuite_id == netsuite_id
                ).first()

                if existing:
                    changed = False
                    for field in SYNC_FIELDS:
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
                    txn = Transaction(**mapped)
                    session.add(txn)
                    inserted += 1

                processed += 1

                # Batch commit
                if processed % batch_size == 0:
                    session.commit()
                    print(f"  Committed batch ({processed} rows processed, {fetched} fetched)...")

        # Final commit
        session.commit()

    print(f"\nSync complete: {inserted} inserted, {updated} updated, {skipped} unchanged, {unresolved} unresolved (missing product/supplier).")

    with Session() as session:
        from sqlalchemy import func
        total = session.query(func.count(Transaction.id)).filter(
            Transaction.doc_type == DOC_TYPE
        ).scalar()
        print(f"Total SalesOrder lines in database: {total}")

    engine.dispose()


if __name__ == "__main__":
    main()
