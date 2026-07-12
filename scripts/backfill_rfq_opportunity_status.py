"""
backfill_rfq_opportunity_status.py

One-off script to align legacy RFQ status values with linked Opportunity statuses.

For each RFQ linked to an Opportunity (via netsuite_opportunity field or
opportunity_id FK), looks up the Opportunity's status and updates the RFQ
status to match using the same mapping as the NetSuite sync.

Mapping:
  Opportunity A (In Progress)  → RFQ in_progress
  Opportunity B (Issued Quote) → RFQ issued_quote
  Opportunity C (Closed Won)   → RFQ closed_won
  Opportunity D (Closed Lost)  → RFQ closed_lost

Draft RFQs that are NOT linked to an Opportunity are left untouched.

Usage:
  uv run python -m scripts.backfill_rfq_opportunity_status
  uv run python -m scripts.backfill_rfq_opportunity_status --dry-run
"""

import argparse
import logging
import sys

from sqlalchemy import text

from includes.dashboard.database import get_session

logger = logging.getLogger(__name__)

# Map Opportunity status codes to RFQ status values
_OPPORTUNITY_TO_RFQ_STATUS = {
    "A": "in_progress",
    "B": "issued_quote",
    "C": "closed_won",
    "D": "closed_lost",
}


def backfill(session, dry_run: bool = False):
    """Find all RFQs linked to Opportunities and sync their status."""
    
    # Find RFQs linked via netsuite_opportunity (opportunity number string)
    rows_by_number = session.execute(text("""
        SELECT r.rfq_number, r.status AS rfq_status, r.netsuite_opportunity,
               o.status AS opp_status, o.opportunity_number
        FROM rfqs r
        JOIN opportunities o ON o.opportunity_number = r.netsuite_opportunity
        WHERE r.netsuite_opportunity IS NOT NULL
          AND r.netsuite_opportunity != ''
    """)).mappings().all()
    
    # Find RFQs linked via opportunity_id (UUID FK)
    rows_by_id = session.execute(text("""
        SELECT r.rfq_number, r.status AS rfq_status, r.netsuite_opportunity,
               o.status AS opp_status, o.opportunity_number
        FROM rfqs r
        JOIN opportunities o ON o.id = r.opportunity_id
        WHERE r.opportunity_id IS NOT NULL
    """)).mappings().all()
    
    # Merge both result sets (deduplicate by rfq_number)
    seen = set()
    all_rows = []
    for row in rows_by_number:
        if row["rfq_number"] not in seen:
            seen.add(row["rfq_number"])
            all_rows.append(row)
    for row in rows_by_id:
        if row["rfq_number"] not in seen:
            seen.add(row["rfq_number"])
            all_rows.append(row)
    
    if not all_rows:
        logger.info("No RFQs linked to Opportunities found.")
        return
    
    logger.info(f"Found {len(all_rows)} RFQ(s) linked to Opportunities.")
    
    updated = 0
    skipped = 0
    
    for row in all_rows:
        rfq_number = row["rfq_number"]
        current_status = row["rfq_status"]
        opp_status_code = row["opp_status"]
        opp_number = row["opportunity_number"]
        
        if not opp_status_code:
            logger.warning(f"  {rfq_number}: Opportunity {opp_number} has no status — skipping")
            skipped += 1
            continue
        
        new_status = _OPPORTUNITY_TO_RFQ_STATUS.get(opp_status_code)
        if not new_status:
            logger.warning(f"  {rfq_number}: unknown Opportunity status '{opp_status_code}' — skipping")
            skipped += 1
            continue
        
        if current_status == new_status:
            logger.debug(f"  {rfq_number}: already {new_status} — skipping")
            skipped += 1
            continue
        
        if dry_run:
            logger.info(f"  [dry-run] {rfq_number}: {current_status} → {new_status} (Opp {opp_number} status {opp_status_code})")
            updated += 1
            continue
        
        session.execute(text("""
            UPDATE rfqs SET status = :new_status
            WHERE rfq_number = :rfq_number
        """), {"new_status": new_status, "rfq_number": rfq_number})
        logger.info(f"  {rfq_number}: {current_status} → {new_status} (Opp {opp_number} status {opp_status_code})")
        updated += 1
    
    if not dry_run:
        session.commit()
    
    logger.info(f"Done: {updated} updated, {skipped} skipped (already correct or unlinked).")


def main():
    parser = argparse.ArgumentParser(
        description="Backfill RFQ statuses to match linked Opportunity statuses"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = parser.parse_args()
    
    session = get_session()
    try:
        backfill(session, dry_run=args.dry_run)
    finally:
        session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
