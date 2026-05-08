"""
Migrate RFQs from LangGraph BaseStore to SQL tables.

Reads all RFQ records from the `store` table (prefix='rfqs') and inserts them
into the new `rfqs` and `rfq_items` tables. Skips any RFQs that already exist
in the SQL tables (by rfq_number).

Usage:
    uv run python scripts/migrate_rfqs_to_sql.py [--dry-run]
"""

import argparse
import json
import logging
import sys
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, ".")

from includes.dashboard.database import _sync_url
from includes.dashboard.models import RFQ, RFQItem

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def parse_date(val) -> date:
    """Parse a date string or return today."""
    if not val:
        return date.today()
    if isinstance(val, date):
        return val
    try:
        return date.fromisoformat(val[:10])
    except (ValueError, TypeError):
        return date.today()


def migrate(dry_run: bool = False):
    engine = create_engine(_sync_url(), pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Read all RFQs from the BaseStore table
        rows = session.execute(
            text("SELECT key, value FROM store WHERE prefix = 'rfqs' ORDER BY key")
        ).fetchall()

        logger.info(f"Found {len(rows)} RFQ(s) in BaseStore")

        migrated = 0
        skipped = 0

        for row in rows:
            rfq_number = row[0]
            data = row[1] if isinstance(row[1], dict) else json.loads(row[1])

            # Check if already migrated
            existing = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
            if existing:
                logger.info(f"  SKIP {rfq_number} — already in SQL")
                skipped += 1
                continue

            # Build the RFQ model
            rfq = RFQ(
                id=uuid.uuid4(),
                rfq_number=rfq_number,
                customer=data.get("customer", "Unknown"),
                customer_contact=data.get("customer_contact"),
                reference=data.get("reference"),
                netsuite_opportunity=data.get("netsuite_opportunity"),
                hubspot_deal=data.get("hubspot_deal"),
                created_by=data.get("created_by", "unknown"),
                created_date=parse_date(data.get("created_date")),
                assigned_to=data.get("assigned_to"),
                thread_id=data.get("thread_id"),
                status=data.get("status", "draft"),
                notes=data.get("notes") or None,
                history=data.get("history", []),
                updated_at=datetime.now(timezone.utc),
            )

            # Build RFQItem models from embedded items list
            items = data.get("items", [])
            for item_data in items:
                product_id = item_data.get("product_id")
                if product_id:
                    try:
                        product_id = uuid.UUID(product_id)
                    except (ValueError, TypeError):
                        product_id = None

                rfq_item = RFQItem(
                    id=uuid.uuid4(),
                    rfq_id=rfq.id,
                    line=item_data.get("line", 1),
                    input_description=item_data.get("input_description"),
                    input_code=item_data.get("input_code"),
                    part_number=item_data.get("part_number"),
                    brand=item_data.get("brand"),
                    product_id=product_id,
                    quantity=item_data.get("quantity"),
                    uom=item_data.get("uom", "ea"),
                    status=item_data.get("status", "unidentified"),
                    notes=item_data.get("notes") or None,
                    suppliers=item_data.get("suppliers", []),
                )
                session.add(rfq_item)

            session.add(rfq)
            logger.info(f"  MIGRATE {rfq_number} — {data.get('customer')} ({len(items)} items)")
            migrated += 1

        if dry_run:
            logger.info(f"\n[DRY RUN] Would migrate {migrated}, skip {skipped}")
            session.rollback()
        else:
            session.commit()
            logger.info(f"\nDone: migrated {migrated}, skipped {skipped}")

    except Exception as e:
        session.rollback()
        logger.error(f"Migration failed: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate RFQs from BaseStore to SQL")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)
