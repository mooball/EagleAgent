"""
sync_prod_mail_data.py

One-shot sync of RFQ + email_tracking data from production into the local database.
Useful for getting realistic test data for the email quote pipeline.

Suppliers and customers are already in the local DB (from NetSuite sync), but may
have different UUIDs than production. This script builds a UUID mapping via netsuite_id
and remaps all FK references before inserting.

What it syncs:
  - rfqs + rfq_items (full replace, FKs remapped to local UUIDs)
  - email_tracking (full replace, FKs remapped to local UUIDs)

Usage:
  uv run python -m scripts.sync_prod_mail_data
  uv run python -m scripts.sync_prod_mail_data --limit 50    # only most recent 50 RFQs
  uv run python -m scripts.sync_prod_mail_data --dry-run     # show counts without writing
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

PROD_URL = os.getenv("PROD_DATABASE_URL", "")
LOCAL_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/eagleagent")

# Normalize to psycopg driver
if "+asyncpg" in LOCAL_URL:
    LOCAL_URL = LOCAL_URL.replace("+asyncpg", "+psycopg")
elif "postgresql://" in LOCAL_URL and "+psycopg" not in LOCAL_URL:
    LOCAL_URL = LOCAL_URL.replace("postgresql://", "postgresql+psycopg://")

if "postgresql://" in PROD_URL and "+psycopg" not in PROD_URL:
    PROD_URL = PROD_URL.replace("postgresql://", "postgresql+psycopg://")


def get_engines():
    if not PROD_URL:
        logger.error("PROD_DATABASE_URL not set in .env")
        sys.exit(1)
    prod = create_engine(PROD_URL, echo=False)
    local = create_engine(LOCAL_URL, echo=False)
    return prod, local


def _adapt_row(row: dict) -> dict:
    """Convert JSONB dicts/lists to psycopg-compatible Jsonb wrappers."""
    from psycopg.types.json import Jsonb

    adapted = {}
    for k, v in row.items():
        if isinstance(v, (dict, list)):
            adapted[k] = Jsonb(v)
        else:
            adapted[k] = v
    return adapted


def _build_id_maps(prod_conn, local_conn) -> tuple[dict, dict]:
    """Build prod UUID → local UUID mappings for suppliers and customers via netsuite_id."""
    # Suppliers: prod netsuite_id → prod id, local netsuite_id → local id
    prod_suppliers = prod_conn.execute(text(
        "SELECT id, netsuite_id FROM suppliers WHERE netsuite_id IS NOT NULL"
    )).fetchall()
    local_suppliers = local_conn.execute(text(
        "SELECT id, netsuite_id FROM suppliers WHERE netsuite_id IS NOT NULL"
    )).fetchall()

    # Map: prod netsuite_id → prod UUID
    prod_ns_to_id = {r[1]: str(r[0]) for r in prod_suppliers}
    # Map: local netsuite_id → local UUID
    local_ns_to_id = {r[1]: str(r[0]) for r in local_suppliers}

    # Final map: prod UUID → local UUID
    supplier_map = {}
    for ns_id, prod_id in prod_ns_to_id.items():
        if ns_id in local_ns_to_id:
            supplier_map[prod_id] = local_ns_to_id[ns_id]

    # Customers: same approach
    prod_customers = prod_conn.execute(text(
        "SELECT id, netsuite_id FROM customers WHERE netsuite_id IS NOT NULL"
    )).fetchall()
    local_customers = local_conn.execute(text(
        "SELECT id, netsuite_id FROM customers WHERE netsuite_id IS NOT NULL"
    )).fetchall()

    prod_ns_to_id = {r[1]: str(r[0]) for r in prod_customers}
    local_ns_to_id = {r[1]: str(r[0]) for r in local_customers}

    customer_map = {}
    for ns_id, prod_id in prod_ns_to_id.items():
        if ns_id in local_ns_to_id:
            customer_map[prod_id] = local_ns_to_id[ns_id]

    return supplier_map, customer_map


def _remap_supplier_id(val, supplier_map: dict):
    """Remap a supplier UUID, returning None if not found locally."""
    if val is None:
        return None
    key = str(val)
    return supplier_map.get(key)  # None if not in local DB


def _remap_customer_id(val, customer_map: dict):
    """Remap a customer UUID, returning None if not found locally."""
    if val is None:
        return None
    key = str(val)
    return customer_map.get(key)  # None if not in local DB


def _remap_item_suppliers_json(suppliers_json, supplier_map: dict):
    """Remap supplier_id references inside rfq_items.suppliers JSONB array."""
    if not suppliers_json:
        return suppliers_json
    remapped = []
    for s in suppliers_json:
        s_copy = dict(s)
        if s_copy.get("supplier_id"):
            new_id = supplier_map.get(str(s_copy["supplier_id"]))
            s_copy["supplier_id"] = new_id  # None if not mapped
        remapped.append(s_copy)
    return remapped


def main():
    parser = argparse.ArgumentParser(description="Sync RFQ + email data from production to local DB")
    parser.add_argument("--limit", type=int, default=None, help="Limit to N most recent RFQs (emails scale proportionally)")
    parser.add_argument("--dry-run", action="store_true", help="Show counts without writing")
    args = parser.parse_args()

    prod_engine, local_engine = get_engines()

    logger.info("Connecting to production...")
    with prod_engine.connect() as prod_conn:
        # Count what's available
        rfq_count = prod_conn.execute(text("SELECT COUNT(*) FROM rfqs")).scalar()
        email_count = prod_conn.execute(text("SELECT COUNT(*) FROM email_tracking")).scalar()
        logger.info(f"  Production has {rfq_count} RFQs, {email_count} emails")

        if args.dry_run:
            effective = args.limit or rfq_count
            logger.info(f"  Would sync ~{effective} RFQs + proportional emails")
            return

        # Build UUID mappings (prod → local) via netsuite_id
        logger.info("Building ID mappings (prod → local via netsuite_id)...")
        with local_engine.connect() as local_conn:
            supplier_map, customer_map = _build_id_maps(prod_conn, local_conn)
        logger.info(f"  Mapped {len(supplier_map)} suppliers, {len(customer_map)} customers")

        # Read RFQ data from production
        logger.info("Reading RFQ data from production...")
        limit_clause = f"LIMIT {args.limit}" if args.limit else ""
        rfqs = prod_conn.execute(text(f"SELECT * FROM rfqs ORDER BY created_date DESC {limit_clause}")).mappings().all()

        items = []
        if rfqs:
            rfq_ids = [str(r["id"]) for r in rfqs]
            placeholders = ",".join(f"'{rid}'" for rid in rfq_ids)
            items = prod_conn.execute(text(f"SELECT * FROM rfq_items WHERE rfq_id IN ({placeholders})")).mappings().all()
        logger.info(f"  {len(rfqs)} RFQs, {len(items)} items")

        # Read emails from production
        logger.info("Reading email_tracking from production...")
        email_limit = f"LIMIT {args.limit * 20}" if args.limit else ""
        emails = prod_conn.execute(text(f"SELECT * FROM email_tracking ORDER BY created_at DESC NULLS LAST {email_limit}")).mappings().all()
        logger.info(f"  {len(emails)} emails")

    # Write to local (prod connection no longer needed)
    logger.info("Writing RFQs and items to local (remapping FKs)...")
    n_cust_miss = 0
    n_supp_miss = 0
    with local_engine.begin() as local_conn:
        local_conn.execute(text("DELETE FROM rfq_items"))
        local_conn.execute(text("DELETE FROM rfqs"))

        for r in rfqs:
            row_dict = dict(r)
            row_dict["opportunity_id"] = None  # opportunities not synced

            # Remap customer_id
            if row_dict.get("customer_id"):
                mapped = _remap_customer_id(row_dict["customer_id"], customer_map)
                if not mapped:
                    n_cust_miss += 1
                row_dict["customer_id"] = mapped

            cols = list(row_dict.keys())
            vals = ", ".join(f":{c}" for c in cols)
            col_names = ", ".join(cols)
            local_conn.execute(text(f"INSERT INTO rfqs ({col_names}) VALUES ({vals})"), _adapt_row(row_dict))

        for item in items:
            row_dict = dict(item)
            row_dict.pop("product_id", None)  # products not synced

            # Remap supplier_id references inside suppliers JSONB
            if row_dict.get("suppliers"):
                row_dict["suppliers"] = _remap_item_suppliers_json(row_dict["suppliers"], supplier_map)

            cols = list(row_dict.keys())
            vals = ", ".join(f":{c}" for c in cols)
            col_names = ", ".join(cols)
            local_conn.execute(text(f"INSERT INTO rfq_items ({col_names}) VALUES ({vals})"), _adapt_row(row_dict))

    logger.info(f"  ✓ {len(rfqs)} RFQs, {len(items)} items")
    if n_cust_miss:
        logger.info(f"    ({n_cust_miss} RFQs had unmapped customer_id → set to NULL)")

    logger.info("Writing email_tracking to local (remapping FKs)...")
    with local_engine.begin() as local_conn:
        local_conn.execute(text("DELETE FROM email_tracking"))

        for e in emails:
            row_dict = dict(e)
            # Remap supplier_id and customer_id
            if row_dict.get("supplier_id"):
                mapped = _remap_supplier_id(row_dict["supplier_id"], supplier_map)
                if not mapped:
                    n_supp_miss += 1
                row_dict["supplier_id"] = mapped
            if row_dict.get("customer_id"):
                mapped = _remap_customer_id(row_dict["customer_id"], customer_map)
                row_dict["customer_id"] = mapped

            cols = list(row_dict.keys())
            vals = ", ".join(f":{c}" for c in cols)
            col_names = ", ".join(cols)
            local_conn.execute(text(f"INSERT INTO email_tracking ({col_names}) VALUES ({vals})"), _adapt_row(row_dict))

        # Fix sequence
        local_conn.execute(text(
            "SELECT setval('email_tracking_id_seq', (SELECT COALESCE(MAX(id), 0) + 1 FROM email_tracking), false)"
        ))

    logger.info(f"  ✓ {len(emails)} emails")
    if n_supp_miss:
        logger.info(f"    ({n_supp_miss} emails had unmapped supplier_id → set to NULL)")

    logger.info("\nDone! Local DB now has production RFQ + email data.")


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
