"""
One-off script to backfill `isinactive = true` for customers, suppliers, products, and brands.

Queries NetSuite SuiteQL for all records where `isinactive = 'T'`, then sets
`isinactive = true` on the matching local database rows.

Usage:
    uv run python -m scripts.backfill_inactive_records
    uv run python -m scripts.backfill_inactive_records --dry-run
"""

import argparse
import logging
import sys

from sqlalchemy import text

from includes.dashboard.database import get_session
from includes.netsuite.client import NetSuiteClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ENTITIES = [
    {
        "name": "customers",
        "table": "customers",
        "suiteql": "SELECT id FROM customer WHERE isinactive = 'T'",
    },
    {
        "name": "suppliers (vendors)",
        "table": "suppliers",
        "suiteql": "SELECT id FROM vendor WHERE isinactive = 'T'",
    },
    {
        "name": "products (items)",
        "table": "products",
        "suiteql": "SELECT id FROM item WHERE itemtype = 'InvtPart' AND isinactive = 'T'",
    },
    {
        "name": "brands",
        "table": "brands",
        "suiteql": "SELECT id FROM customrecord_brands WHERE isinactive = 'T'",
    },
]


def backfill_entity(client: NetSuiteClient, session, entity: dict, dry_run: bool = False) -> int:
    name = entity["name"]
    table = entity["table"]
    query = entity["suiteql"]

    logger.info(f"Fetching inactive {name} from NetSuite...")
    rows = client.suiteql(query)
    ns_ids = [str(r["id"]).strip() for r in rows if r.get("id")]
    logger.info(f"Found {len(ns_ids)} inactive {name} in NetSuite.")

    if not ns_ids:
        return 0

    if dry_run:
        # Check how many would be updated in the DB
        stmt = text(f"SELECT count(*) FROM {table} WHERE netsuite_id = ANY(:ids) AND isinactive = false")
        count = session.execute(stmt, {"ids": ns_ids}).scalar()
        logger.info(f"[DRY-RUN] Would update {count} {table} rows to isinactive = true.")
        return count

    # Batch update in chunks of 1000
    total_updated = 0
    chunk_size = 1000
    for i in range(0, len(ns_ids), chunk_size):
        chunk = ns_ids[i : i + chunk_size]
        stmt = text(f"UPDATE {table} SET isinactive = true WHERE netsuite_id = ANY(:ids) AND isinactive = false")
        result = session.execute(stmt, {"ids": chunk})
        total_updated += result.rowcount

    session.commit()
    logger.info(f"Updated {total_updated} rows in {table} to isinactive = true.")
    return total_updated


def main():
    parser = argparse.ArgumentParser(description="Backfill inactive flags from NetSuite into local DB")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing to DB")
    args = parser.parse_args()

    client = NetSuiteClient()
    session = get_session()

    try:
        total = 0
        for entity in ENTITIES:
            updated = backfill_entity(client, session, entity, dry_run=args.dry_run)
            total += updated
        logger.info(f"Finished! Total records marked inactive: {total}")
    except Exception as e:
        session.rollback()
        logger.exception(f"Backfill failed: {e}")
        sys.exit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()
