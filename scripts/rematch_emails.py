"""Re-match existing email_tracking records against RFQs and contacts.

Scans all email_tracking rows and re-runs the subject + contact matching
pipeline to fill in missing rfq_id, opportunity_id, supplier_id, customer_id.

Usage:
    uv run python -m scripts.rematch_emails [--dry-run] [--unlinked-only]

Options:
    --dry-run         Show what would be updated without writing to database
    --unlinked-only   Only process records that have no rfq_id AND no supplier_id
    --force           Re-match ALL records (overwrite existing links)
"""

import argparse
import logging
import sys

from sqlalchemy import text

from config.settings import Config
from includes.dashboard.database import get_session
from includes.dashboard.models import (
    EmailTracking,
    RFQ,
)
from includes.gmail.matching import (
    build_domain_index,
    extract_domain,
    match_by_contact,
    match_by_subject,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def extract_email_address(header_value: str) -> str:
    """Extract bare email from a header value like 'Name <email@domain.com>'."""
    if not header_value:
        return ""
    if '<' in header_value and '>' in header_value:
        return header_value.split('<')[1].split('>')[0].strip().lower()
    return header_value.strip().lower()


# Company-owned domains to exclude from external address extraction
_OWN_DOMAINS = frozenset({"eagle-exports.com", "eaglexp.com.au"})


def extract_external_addresses(record: EmailTracking) -> list[str]:
    """Extract external email addresses from an email_tracking record."""
    addresses = []

    # Use all_recipients if available (Phase 6.2 enriched records)
    if record.all_recipients:
        for r in record.all_recipients:
            email = r.get("email", "").lower().strip()
            if email and '@' in email:
                domain = email.rsplit('@', 1)[1]
                if domain not in _OWN_DOMAINS:
                    addresses.append(email)
        return addresses

    # Fallback to recipient_email + user_email
    for raw in [record.recipient_email, record.user_email]:
        if not raw:
            continue
        for part in raw.split(','):
            addr = extract_email_address(part)
            if addr and '@' in addr:
                domain = addr.rsplit('@', 1)[1]
                if domain not in _OWN_DOMAINS:
                    addresses.append(addr)
    return addresses


def rematch_all(dry_run: bool = False, unlinked_only: bool = False, force: bool = False):
    """Re-run matching on existing email_tracking records."""
    session = get_session()
    try:
        domain_index = build_domain_index(session)
        logger.info(f"Domain index: {len(domain_index)} domains")

        query = session.query(EmailTracking)
        if unlinked_only:
            query = query.filter(
                EmailTracking.rfq_id.is_(None),
                EmailTracking.supplier_id.is_(None),
                EmailTracking.customer_id.is_(None),
            )

        records = query.order_by(EmailTracking.id).all()
        logger.info(f"Processing {len(records)} email records...")

        stats = {
            "total": len(records),
            "rfq_linked": 0,
            "op_linked": 0,
            "supplier_linked": 0,
            "customer_linked": 0,
            "skipped": 0,
            "already_linked": 0,
        }

        for record in records:
            updated = False

            # --- Subject matching (RFQ / OP) ---
            subject_match = match_by_subject(record.subject or "")
            if subject_match:
                rfq_number = subject_match.get("rfq_number")
                op_number = subject_match.get("opportunity_number")

                rfq = None
                full_rfq_number = None

                if rfq_number:
                    full_rfq_number = f"RFQ-{rfq_number}" if not rfq_number.upper().startswith("RFQ-") else rfq_number
                    rfq = session.query(RFQ).filter(RFQ.rfq_number == full_rfq_number).first()

                # Try OP lookup if no RFQ found by number
                if not rfq and op_number:
                    rfq = session.query(RFQ).filter(RFQ.netsuite_opportunity == op_number).first()
                    if rfq:
                        full_rfq_number = rfq.rfq_number

                # Update rfq_id if missing or force
                if rfq and (force or not record.rfq_id):
                    if record.rfq_id != full_rfq_number:
                        record.rfq_id = full_rfq_number
                        record.rfq_token = full_rfq_number
                        updated = True
                        stats["rfq_linked"] += 1
                        if dry_run:
                            logger.info(f"  [RFQ] #{record.id} '{record.subject[:50]}' → {full_rfq_number}")

                # Update opportunity_id if missing or force
                if op_number and (force or not record.opportunity_id):
                    if record.opportunity_id != op_number:
                        record.opportunity_id = op_number
                        updated = True
                        stats["op_linked"] += 1
                        if dry_run:
                            logger.info(f"  [OP]  #{record.id} '{record.subject[:50]}' → {op_number}")

            # --- Contact matching (Supplier / Customer) ---
            if force or not record.supplier_id or not record.customer_id:
                external_addresses = extract_external_addresses(record)
                if external_addresses:
                    contact_match = match_by_contact(session, external_addresses, domain_index)
                    if contact_match.get("supplier_id") and (force or not record.supplier_id):
                        if record.supplier_id != contact_match["supplier_id"]:
                            record.supplier_id = contact_match["supplier_id"]
                            record.match_type = contact_match.get("match_type")
                            updated = True
                            stats["supplier_linked"] += 1
                            if dry_run:
                                logger.info(f"  [SUP] #{record.id} '{record.subject[:50]}' → supplier {contact_match['supplier_id']}")
                    if contact_match.get("customer_id") and (force or not record.customer_id):
                        if record.customer_id != contact_match["customer_id"]:
                            record.customer_id = contact_match["customer_id"]
                            record.match_type = contact_match.get("match_type")
                            updated = True
                            stats["customer_linked"] += 1
                            if dry_run:
                                logger.info(f"  [CUS] #{record.id} '{record.subject[:50]}' → customer {contact_match['customer_id']}")

            if not updated:
                if record.rfq_id or record.supplier_id:
                    stats["already_linked"] += 1
                else:
                    stats["skipped"] += 1

        if not dry_run:
            session.commit()
            logger.info("Changes committed.")
        else:
            logger.info("DRY RUN — no changes written.")

        logger.info(
            f"Results: {stats['total']} total, "
            f"{stats['rfq_linked']} RFQ linked, "
            f"{stats['op_linked']} OP linked, "
            f"{stats['supplier_linked']} supplier linked, "
            f"{stats['customer_linked']} customer linked, "
            f"{stats['already_linked']} already linked, "
            f"{stats['skipped']} unmatched"
        )

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser(description="Re-match email_tracking records against RFQs and contacts")
    parser.add_argument("--dry-run", action="store_true", help="Show matches without writing to database")
    parser.add_argument("--unlinked-only", action="store_true", help="Only process records with no existing links")
    parser.add_argument("--force", action="store_true", help="Re-match ALL records, overwriting existing links")
    args = parser.parse_args()

    if args.force and args.unlinked_only:
        print("Error: --force and --unlinked-only are mutually exclusive.")
        sys.exit(1)

    rematch_all(dry_run=args.dry_run, unlinked_only=args.unlinked_only, force=args.force)


if __name__ == "__main__":
    main()
