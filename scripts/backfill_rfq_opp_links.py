"""Backfill RFQ-Opportunity links.

Finds all RFQs where netsuite_opportunity is set but opportunity_id is NULL,
then for each:
  1. Uppercases the netsuite_opportunity value (e.g. 'op1234' → 'OP1234')
  2. Looks up the matching Opportunity by opportunity_number
  3. Sets opportunity_id FK if found
  4. Syncs RFQ status to match Opportunity status (A→in_progress, etc.)

This mirrors the logic in the RFQ edit form (_update_rfq_sync in rfq_crud.py).
Safe to run frequently — idempotent, only targets unlinked RFQs.
Runs automatically as part of the 5-minute NetSuite sync.

Usage:
    uv run python scripts/backfill_rfq_opp_links.py --dry-run
    uv run python scripts/backfill_rfq_opp_links.py
"""

import logging
import os
import sys
import argparse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

# ── Status mapping (mirrors _update_rfq_sync) ────────────────────────────────

_OPP_TO_RFQ = {"A": "in_progress", "B": "issued_quote", "C": "closed_won", "D": "closed_lost"}


def _get_db_url() -> str:
    """Get and normalize the database URL, converting asyncpg to psycopg."""
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise RuntimeError("DATABASE_URL not found in env")
    if "+asyncpg" in db_url:
        db_url = db_url.replace("+asyncpg", "+psycopg")
    elif "postgresql://" in db_url and "+psycopg" not in db_url:
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://")
    return db_url


def link_rfq_opportunities(dry_run: bool = False) -> dict:
    """Link RFQs to their NetSuite Opportunities by OP number.

    Returns a dict with counts: {updated, skipped, errors}.
    """
    engine = create_engine(_get_db_url())
    session = sessionmaker(bind=engine)()

    try:
        rows = session.execute(
            text("""
                SELECT id, rfq_number, netsuite_opportunity, status
                FROM rfqs
                WHERE netsuite_opportunity IS NOT NULL
                  AND netsuite_opportunity != ''
                  AND opportunity_id IS NULL
                ORDER BY rfq_number
            """)
        ).mappings().all()

        if not rows:
            logger.info("[rfq-opp-link] No unlinked RFQs found")
            return {"updated": 0, "skipped": 0, "errors": 0}

        logger.info(f"[rfq-opp-link] Found {len(rows)} RFQs with OP number but no opportunity link")

        updated = 0
        skipped = 0
        errors = 0

        for r in rows:
            rfq_number = r["rfq_number"]
            old_opp = r["netsuite_opportunity"]
            old_status = r["status"]
            upper_opp = old_opp.strip().upper()

            opp = session.execute(
                text("""
                    SELECT id, opportunity_number, status
                    FROM opportunities
                    WHERE opportunity_number = :num
                """),
                {"num": upper_opp},
            ).mappings().first()

            if not opp:
                logger.debug(f"[rfq-opp-link] {rfq_number}: OP {upper_opp} — not found, skipping")
                skipped += 1
                continue

            opp_id = str(opp["id"])
            opp_status = opp["status"]
            new_rfq_status = _OPP_TO_RFQ.get(opp_status) if opp_status else None

            if dry_run:
                changes = [f"nsopp {upper_opp}", f"link→{opp_id[:8]}..."]
                if new_rfq_status and new_rfq_status != old_status:
                    changes.append(f"status {old_status}→{new_rfq_status}")
                logger.info(f"[rfq-opp-link] [DRY-RUN] {rfq_number}: {', '.join(changes)}")
                updated += 1
                continue

            try:
                update_sql = """
                    UPDATE rfqs
                    SET netsuite_opportunity = :nsopp,
                        opportunity_id = :opp_id,
                        updated_at = :now
                """
                params = {
                    "nsopp": upper_opp,
                    "opp_id": opp_id,
                    "rfq_id": r["id"],
                    "now": datetime.now(timezone.utc),
                }
                if new_rfq_status and new_rfq_status != old_status:
                    update_sql += ", status = :status"
                    params["status"] = new_rfq_status
                update_sql += " WHERE id = :rfq_id"
                session.execute(text(update_sql), params)
                session.commit()
                updated += 1
            except Exception as e:
                session.rollback()
                logger.warning(f"[rfq-opp-link] {rfq_number}: ERROR {e}")
                errors += 1

        logger.info(
            f"[rfq-opp-link] Done: {updated} updated, {skipped} skipped, {errors} errors"
            + (" (dry run)" if dry_run else "")
        )
        return {"updated": updated, "skipped": skipped, "errors": errors}
    finally:
        session.close()
        engine.dispose()


def main():
    """Entry point for both sync orchestrator and standalone use."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Backfill RFQ-Opportunity links")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without applying")
    args, _ = parser.parse_known_args()
    link_rfq_opportunities(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
