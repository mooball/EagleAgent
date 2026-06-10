"""Backfill opportunity_id on existing transactions from NetSuite.

Queries NetSuite for all transaction lines that have an opportunity set,
then updates the local product_suppliers records with the resolved opportunity link.

Usage:
    uv run python scripts/backfill_transaction_opportunities.py [--dry-run] [--batch-size N]
"""

import argparse

from sqlalchemy.orm import sessionmaker

from includes.dashboard.models import Opportunity, Transaction
from includes.netsuite.client import NetSuiteClient
from includes.netsuite.sync_utils import get_engine


QUERY = (
    "SELECT tl.uniquekey, t.opportunity "
    "FROM transactionLine tl "
    "INNER JOIN transaction t ON t.id = tl.transaction "
    "WHERE t.opportunity IS NOT NULL "
    "AND t.type IN ('SalesOrd', 'Estimate') "
    "ORDER BY tl.uniquekey"
)


def main():
    parser = argparse.ArgumentParser(description="Backfill opportunity_id on existing transactions.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be updated without writing.")
    parser.add_argument("--batch-size", type=int, default=500, help="Commit every N updates (default 500).")
    args = parser.parse_args()

    engine = get_engine()
    Session = sessionmaker(bind=engine)

    # Build opportunity lookup: netsuite_id -> local UUID
    print("Loading opportunity lookup map...")
    with Session() as session:
        opportunity_map = {}
        for nid, oid in session.query(Opportunity.netsuite_id, Opportunity.id).filter(
            Opportunity.netsuite_id.isnot(None)
        ):
            opportunity_map[str(nid)] = oid
    print(f"  {len(opportunity_map)} opportunities in lookup map.")

    # Query NetSuite for transaction lines with opportunities
    print("Connecting to NetSuite...")
    client = NetSuiteClient()
    print("Fetching transaction lines with opportunities...")

    updated = 0
    skipped_no_txn = 0
    skipped_no_opp = 0
    already_set = 0
    fetched = 0

    with Session() as session:
        for page in client.suiteql_iter(QUERY):
            page = [{k: v for k, v in row.items() if k != "links"} for row in page]
            fetched += len(page)

            for row in page:
                unique_key = str(row.get("uniquekey") or "").strip()
                opp_ns_id = str(row.get("opportunity") or "").strip()

                if not unique_key or not opp_ns_id:
                    skipped_no_txn += 1
                    continue

                opp_uuid = opportunity_map.get(opp_ns_id)
                if not opp_uuid:
                    skipped_no_opp += 1
                    continue

                if args.dry_run:
                    updated += 1
                    continue

                txn = session.query(Transaction).filter(
                    Transaction.netsuite_id == unique_key
                ).first()

                if not txn:
                    skipped_no_txn += 1
                    continue

                if txn.opportunity_id == opp_uuid and txn.netsuite_opportunity_id == opp_ns_id:
                    already_set += 1
                    continue

                txn.netsuite_opportunity_id = opp_ns_id
                txn.opportunity_id = opp_uuid
                updated += 1

            # Batch commit
            if not args.dry_run and updated > 0 and fetched % args.batch_size < len(page):
                session.commit()
                print(f"  Committed batch — {updated} updated so far ({fetched} fetched)...")

        if not args.dry_run:
            session.commit()

    prefix = "DRY RUN — " if args.dry_run else ""
    print(f"\n{prefix}Done!")
    print(f"  Fetched: {fetched} lines from NetSuite")
    print(f"  Updated: {updated}")
    print(f"  Already set: {already_set}")
    print(f"  Skipped (no local transaction): {skipped_no_txn}")
    print(f"  Skipped (opportunity not in DB): {skipped_no_opp}")


if __name__ == "__main__":
    main()
