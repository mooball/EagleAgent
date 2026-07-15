"""Backfill RFQ-Opportunity links for RFQs with OP numbers but no formal link.

Finds all RFQs where netsuite_opportunity is set but opportunity_id is NULL,
then for each:
  1. Uppercases the netsuite_opportunity value (e.g. 'op1234' → 'OP1234')
  2. Looks up the matching Opportunity by opportunity_number
  3. Sets opportunity_id FK if found
  4. Syncs RFQ status to match Opportunity status (A→in_progress, B→issued_quote, etc.)

This mirrors the logic that runs when a user updates the Opportunity number
on the RFQ edit form (_update_rfq_sync in rfq_crud.py).

Usage:
    # Preview what would change (dry run — safe, no mutations):
    uv run python scripts/backfill_rfq_opp_links.py --dry-run

    # Apply changes:
    uv run python scripts/backfill_rfq_opp_links.py

Requirements:
    - DATABASE_URL must be set in .env (connects to whatever DB the environment
      is configured for — local dev DB or Railway prod DB when deployed)
    - Network access to the target PostgreSQL instance
"""

import os
import sys
import argparse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session

# ── DB connection ────────────────────────────────────────────────────────────

db_url = os.environ.get("DATABASE_URL", "")
if not db_url:
    print("ERROR: DATABASE_URL not found in env")
    sys.exit(1)

# Normalize to psycopg
if "+asyncpg" in db_url:
    db_url = db_url.replace("+asyncpg", "+psycopg")
elif "postgresql://" in db_url and "+psycopg" not in db_url:
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://")

engine = create_engine(db_url)
SessionLocal = sessionmaker(bind=engine)

# ── Status mapping (mirrors _update_rfq_sync) ────────────────────────────────

_OPP_TO_RFQ = {"A": "in_progress", "B": "issued_quote", "C": "closed_won", "D": "closed_lost"}


def backfill(dry_run: bool = False):
    session = SessionLocal()
    try:
        # Find RFQs with OP number but no formal opportunity link
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

        print(f"Found {len(rows)} RFQs with OP number but no opportunity link\n")

        updated = 0
        skipped = 0
        errors = 0

        for r in rows:
            rfq_number = r["rfq_number"]
            old_opp = r["netsuite_opportunity"]
            old_status = r["status"]
            upper_opp = old_opp.strip().upper()

            # Find the matching Opportunity
            opp = session.execute(
                text("""
                    SELECT id, opportunity_number, status
                    FROM opportunities
                    WHERE opportunity_number = :num
                """),
                {"num": upper_opp},
            ).mappings().first()

            if not opp:
                print(f"  {rfq_number}: OP {upper_opp} — Opportunity not found in DB, skipping")
                skipped += 1
                continue

            opp_id = str(opp["id"])
            opp_status = opp["status"]
            new_rfq_status = _OPP_TO_RFQ.get(opp_status) if opp_status else None

            # Build summary
            changes = []
            if old_opp != upper_opp:
                changes.append(f"nsopp {old_opp}→{upper_opp}")
            else:
                changes.append(f"nsopp {upper_opp}")
            changes.append(f"link→{opp_id[:8]}...")
            if new_rfq_status and new_rfq_status != old_status:
                changes.append(f"status {old_status}→{new_rfq_status}")

            prefix = "[DRY-RUN]" if dry_run else "[UPDATE]"
            print(f"  {prefix} {rfq_number}: {', '.join(changes)}")

            if not dry_run:
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
                    print(f"    ERROR: {e}")
                    errors += 1
            else:
                updated += 1

        print(f"\nResults: {updated} updated, {skipped} skipped, {errors} errors")
        if dry_run:
            print("(Dry run — no changes were made)")

    finally:
        session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill RFQ-Opportunity links")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without applying")
    args = parser.parse_args()
    backfill(dry_run=args.dry_run)
