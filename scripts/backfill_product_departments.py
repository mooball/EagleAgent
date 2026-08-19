"""One-off backfill: copy NetSuite item departments onto local products.

The SuiteQL REST ``offset`` parameter caps at 4995, so the regular product
sync cannot page the full item table. This script instead walks NetSuite
items with keyset pagination (``id > last_seen ORDER BY id``) using a
two-column query, and writes ``products.department_id`` only where it is
still NULL.

Unknown department IDs are logged and left NULL (they fail loudly instead
of polluting the column).

Usage:
  uv run python -m scripts.backfill_product_departments
  uv run python -m scripts.backfill_product_departments --dry-run
  uv run python -m scripts.backfill_product_departments --max-pages 3
"""

import argparse
import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from config.settings import Config
from includes.netsuite.client import NetSuiteClient
from includes.netsuite.departments import DEPARTMENT_BY_ID

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PAGE_LIMIT = 1000  # SuiteQL max rows per request


def get_engine():
    db_url = Config.DATABASE_URL
    if not db_url:
        raise ValueError("DATABASE_URL is empty. Check your `.env` settings.")

    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return create_engine(db_url)


def build_page_query(last_id: str | None) -> str:
    """SuiteQL for one keyset page of items that have a department."""
    query = "SELECT id, department FROM item WHERE department IS NOT NULL"
    if last_id:
        query += f" AND id > '{last_id}'"
    query += " ORDER BY id ASC"
    return query


def valid_department_id(raw) -> str | None:
    """Return the department ID if it is a known Department enum value."""
    if raw is None:
        return None
    dept = str(raw).strip()
    return dept if dept in DEPARTMENT_BY_ID else None


def fetch_page(client: NetSuiteClient, last_id: str | None) -> list[dict]:
    """Fetch one page of (id, department) rows, dropping the 'links' key."""
    resp = client.post(
        "query/v1/suiteql",
        json={"q": build_page_query(last_id)},
        params={"limit": PAGE_LIMIT, "offset": 0},
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return [{k: v for k, v in item.items() if k != "links"} for item in items]


def main():
    parser = argparse.ArgumentParser(description="Backfill products.department_id from NetSuite.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and report without writing to DB.")
    parser.add_argument("--max-pages", type=int, default=0, help="Stop after N pages (0 = unlimited). For smoke tests.")
    args = parser.parse_args()

    engine = get_engine()
    Session = sessionmaker(bind=engine)
    client = NetSuiteClient()

    last_id: str | None = None
    page_no = 0
    seen = 0
    updated = 0
    unknown = 0

    while True:
        page_no += 1
        items = fetch_page(client, last_id)
        if not items:
            break
        seen += len(items)
        last_id = items[-1]["id"]

        mappings = []
        for item in items:
            dept = valid_department_id(item.get("department"))
            if dept is None:
                unknown += 1
                logger.warning(
                    "Unknown department %r for item %s — leaving NULL",
                    item.get("department"), item.get("id"),
                )
                continue
            mappings.append({"nsid": item["id"], "dept": dept})

        if mappings and not args.dry_run:
            with Session() as session:
                session.execute(
                    text(
                        "UPDATE products SET department_id = :dept "
                        "WHERE netsuite_id = :nsid AND department_id IS NULL"
                    ),
                    mappings,
                )
                session.commit()
            updated += len(mappings)

        print(
            f"page {page_no}: {len(items)} rows | {len(mappings)} mapped | "
            f"cumulative: {seen} seen, {updated} updated, {unknown} unknown"
        )

        if args.max_pages and page_no >= args.max_pages:
            print(f"Stopping at --max-pages={args.max_pages}")
            break
        if len(items) < PAGE_LIMIT:
            break

    print(f"\nBackfill complete: {seen} items seen, {updated} updated, {unknown} unknown skipped.")
    engine.dispose()


if __name__ == "__main__":
    main()
