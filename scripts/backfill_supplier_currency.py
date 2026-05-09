"""
backfill_supplier_currency.py

One-off script to populate the `currency` field on all suppliers
by querying NetSuite for vendor currency codes.

Usage:
  uv run python -m scripts.backfill_supplier_currency
  uv run python -m scripts.backfill_supplier_currency --dry-run
"""

import argparse

from sqlalchemy.orm import sessionmaker

from includes.dashboard.models import Supplier
from includes.netsuite.client import NetSuiteClient
from scripts.sync_netsuite_suppliers import get_engine


QUERY = (
    "SELECT v.id, BUILTIN.DF(v.currency) AS currency "
    "FROM vendor v "
    "WHERE v.isinactive = 'F' "
    "ORDER BY v.id"
)


def main():
    parser = argparse.ArgumentParser(description="Backfill supplier currency from NetSuite")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing to DB")
    args = parser.parse_args()

    engine = get_engine()
    Session = sessionmaker(bind=engine)
    session = Session()

    client = NetSuiteClient()

    # Build lookup: netsuite_id -> currency
    print("Fetching vendor currencies from NetSuite...")
    currency_map = {}
    row_count = 0
    for page in client.suiteql_iter(QUERY):
        for row in page:
            netsuite_id = str(row.get("id", "")).strip()
            currency = row.get("currency")
            if netsuite_id and currency:
                currency_map[netsuite_id] = currency
            row_count += 1
    print(f"  Fetched {row_count} vendors, {len(currency_map)} with currency set")

    # Update local suppliers
    suppliers = session.query(Supplier).filter(Supplier.netsuite_id.isnot(None)).all()
    updated = 0
    skipped = 0
    for sup in suppliers:
        new_currency = currency_map.get(sup.netsuite_id)
        if not new_currency:
            skipped += 1
            continue
        if sup.currency == new_currency:
            skipped += 1
            continue
        old = sup.currency or "(none)"
        if args.dry_run:
            print(f"  [DRY RUN] {sup.name}: {old} -> {new_currency}")
        else:
            sup.currency = new_currency
        updated += 1

    if not args.dry_run:
        session.commit()

    print(f"Done. Updated: {updated}, Skipped: {skipped}")
    session.close()


if __name__ == "__main__":
    main()
