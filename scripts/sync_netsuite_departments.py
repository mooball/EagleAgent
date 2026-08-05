"""Sync NetSuite departments into the system_settings table.

Queries the department table via SuiteQL and stores the result
under the "departments" key in system_settings.

Usage:
    uv run python -m scripts.sync_netsuite_departments
    uv run python -m scripts.sync_netsuite_departments --dry-run
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config.settings import Config
from includes.netsuite.client import NetSuiteClient
from includes.system_settings import set_setting

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _get_db_url() -> str:
    db_url = Config.DATABASE_URL
    if not db_url:
        raise RuntimeError("DATABASE_URL not found in env")
    if "+asyncpg" in db_url:
        db_url = db_url.replace("+asyncpg", "+psycopg")
    elif "postgresql://" in db_url and "+psycopg" not in db_url:
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://")
    return db_url


def fetch_departments(client: NetSuiteClient) -> list[dict]:
    """Fetch all departments from NetSuite via SuiteQL."""
    query = "SELECT id, name, fullname, isinactive, subsidiary FROM department ORDER BY id"

    logger.info("Fetching departments from NetSuite...")
    resp = client.post(
        "query/v1/suiteql",
        json={"q": query},
        params={"limit": 100, "offset": 0},
    )
    items = resp.json().get("items", [])

    departments = []
    for item in items:
        departments.append({
            "netsuite_id": item["id"],
            "name": item["name"],
            "fullname": item.get("fullname", item["name"]),
            "isinactive": item.get("isinactive", "F") == "T",
            "subsidiary": item.get("subsidiary"),
        })

    logger.info(f"Fetched {len(departments)} departments")
    return departments


def main():
    parser = argparse.ArgumentParser(description="Sync NetSuite departments to system_settings")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and display without saving")
    args = parser.parse_args()

    client = NetSuiteClient()
    departments = fetch_departments(client)

    if not departments:
        logger.warning("No departments found")
        return

    print("\nDepartments from NetSuite:")
    for d in departments:
        status = "INACTIVE" if d["isinactive"] else "active"
        print(f"  NS ID: {d['netsuite_id']:4} | {status:9} | {d['name']}")

    if args.dry_run:
        print("\n[Dry run — not saved]")
        return

    # Save to system_settings
    engine = create_engine(_get_db_url())
    session = sessionmaker(bind=engine)()

    try:
        set_setting(
            session,
            "departments",
            departments,
            description="NetSuite department list (synced from SuiteQL)",
            updated_by="netsuite_sync",
        )
        session.commit()
        logger.info("Saved departments to system_settings")
        print(f"\nSaved {len(departments)} departments to system_settings")
    except Exception as e:
        session.rollback()
        logger.error(f"Failed to save: {e}")
        raise
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    main()
