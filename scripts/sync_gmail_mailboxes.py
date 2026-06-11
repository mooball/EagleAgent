"""Gmail mailbox sync job — incremental scanning via History API.

Scans enabled mailboxes for new messages and runs the three-tier
matching pipeline to link emails to RFQs, suppliers, and customers.

Usage:
    uv run python -m scripts.sync_gmail_mailboxes [--init] [--user EMAIL] [--dry-run]

Options:
    --init      Initialize sync cursors for all enabled mailboxes (first run)
    --user      Scan only a specific user's mailbox
    --dry-run   Show matches without writing to database
"""

import argparse
import logging
import sys
from datetime import datetime, timezone

from sqlalchemy import text

from config.settings import Config
from includes.dashboard.database import get_session
from includes.dashboard.models import (
    EmailTracking,
    MailboxScanConfig,
    RFQ,
)
from includes.gmail import get_gmail_client
from includes.gmail.matching import (
    build_domain_index,
    determine_direction,
    extract_domain,
    match_by_contact,
    match_by_id,
    match_by_subject,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Only scan @eagle-exports.com mailboxes
SCAN_DOMAIN = "eagle-exports.com"


def get_enabled_mailboxes(session) -> list[str]:
    """Return list of user emails that should be scanned."""
    configs = session.query(MailboxScanConfig).filter(
        MailboxScanConfig.scan_enabled == True,
        MailboxScanConfig.user_email.like(f'%@{SCAN_DOMAIN}'),
    ).all()
    return [c.user_email for c in configs]


def initialize_cursor(session, user_email: str, dry_run: bool = False) -> int | None:
    """Seed the sync cursor with the user's latest historyId."""
    try:
        service = get_gmail_client(user_email)
        profile = service.users().getProfile(userId="me").execute()
        history_id = int(profile.get("historyId", 0))

        if dry_run:
            logger.info(f"[dry-run] Would initialize cursor for {user_email} at historyId={history_id}")
            return history_id

        session.execute(
            text("""
                INSERT INTO mailbox_sync_cursor (user_email, last_history_id, updated_at)
                VALUES (:email, :hid, NOW())
                ON CONFLICT (user_email) DO UPDATE SET last_history_id = :hid, updated_at = NOW()
            """),
            {"email": user_email, "hid": history_id},
        )
        session.commit()
        logger.info(f"Initialized cursor for {user_email} at historyId={history_id}")
        return history_id
    except Exception as e:
        logger.error(f"Failed to initialize cursor for {user_email}: {e}")
        session.rollback()
        return None


def get_cursor(session, user_email: str) -> int | None:
    """Get the last known historyId for a mailbox."""
    row = session.execute(
        text("SELECT last_history_id FROM mailbox_sync_cursor WHERE user_email = :email"),
        {"email": user_email},
    ).fetchone()
    return row[0] if row else None


def update_cursor(session, user_email: str, history_id: int):
    """Update the sync cursor after successful processing."""
    session.execute(
        text("""
            UPDATE mailbox_sync_cursor
            SET last_history_id = :hid, updated_at = NOW()
            WHERE user_email = :email
        """),
        {"email": user_email, "hid": history_id},
    )


def extract_message_metadata(service, message_id: str) -> dict | None:
    """Fetch message metadata (headers, labels) from Gmail API."""
    try:
        msg = service.users().messages().get(
            userId="me", id=message_id, format="metadata",
            metadataHeaders=["From", "To", "Cc", "Subject",
                            "X-Eagle-RFQ", "X-Eagle-OP", "X-Eagle-Opportunity"],
        ).execute()
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        return {
            "id": msg["id"],
            "threadId": msg["threadId"],
            "historyId": msg.get("historyId"),
            "labelIds": msg.get("labelIds", []),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "cc": headers.get("cc", ""),
            "subject": headers.get("subject", ""),
            "x_eagle_rfq": headers.get("x-eagle-rfq"),
            "x_eagle_op": headers.get("x-eagle-op"),
            "x_eagle_opportunity": headers.get("x-eagle-opportunity"),
        }
    except Exception as e:
        logger.warning(f"Failed to fetch message {message_id}: {e}")
        return None


def extract_email_address(header_value: str) -> str:
    """Extract bare email from a header value like 'Name <email@domain.com>'."""
    if '<' in header_value and '>' in header_value:
        return header_value.split('<')[1].split('>')[0].strip().lower()
    return header_value.strip().lower()


def extract_all_addresses(msg_meta: dict) -> list[str]:
    """Extract all email addresses from From, To, Cc headers (excluding @eagle-exports.com)."""
    addresses = []
    for field in ("from", "to", "cc"):
        raw = msg_meta.get(field, "")
        if not raw:
            continue
        # Split multiple recipients
        for part in raw.split(','):
            addr = extract_email_address(part)
            if addr and '@' in addr and not addr.endswith(f'@{SCAN_DOMAIN}'):
                addresses.append(addr)
    return addresses


def process_message(
    session,
    service,
    user_email: str,
    message_id: str,
    domain_index: dict,
    dry_run: bool = False,
) -> str:
    """Process a single message through the matching pipeline.

    Returns: 'tier1' | 'tier2' | 'tier3' | 'skip' | 'error'
    """
    msg_meta = extract_message_metadata(service, message_id)
    if not msg_meta:
        return "error"

    thread_id = msg_meta["threadId"]
    subject = msg_meta["subject"]
    from_addr = extract_email_address(msg_meta["from"])
    external_addresses = extract_all_addresses(msg_meta)
    direction = determine_direction(user_email, from_addr)

    # --- Tier 1: ID match ---
    # Check if this exact message is already tracked
    existing_msg = session.query(EmailTracking).filter(
        EmailTracking.gmail_message_id == msg_meta["id"]
    ).first()
    if existing_msg:
        # Already tracked — nothing to do
        return "tier1"

    # Check if this thread is tracked (matches an existing record)
    existing_thread = session.query(EmailTracking).filter(
        EmailTracking.gmail_thread_id == thread_id
    ).first()
    if existing_thread:
        if dry_run:
            logger.info(
                f"  [T1] Thread match: thread={thread_id}, direction={direction}, "
                f"subject='{subject[:60]}'"
            )
        else:
            # Draft → sent transition: update original record
            if existing_thread.direction == "draft" and direction == "sent" and existing_thread.gmail_message_id is None:
                existing_thread.direction = "sent"
                existing_thread.gmail_message_id = msg_meta["id"]
                existing_thread.sent_at = datetime.now(timezone.utc)
                existing_thread.sent_confirmed = True
                existing_thread.updated_at = datetime.now(timezone.utc)
            else:
                # New message on tracked thread — create a new row inheriting the RFQ link
                tracking = EmailTracking(
                    gmail_thread_id=thread_id,
                    gmail_message_id=msg_meta["id"],
                    user_email=user_email,
                    rfq_id=existing_thread.rfq_id,
                    rfq_token=existing_thread.rfq_token,
                    opportunity_id=existing_thread.opportunity_id,
                    supplier_id=existing_thread.supplier_id,
                    customer_id=existing_thread.customer_id,
                    match_type=existing_thread.match_type,
                    direction=direction,
                    subject=subject,
                    recipient_email=msg_meta.get("to", "").split(",")[0].strip(),
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                session.add(tracking)
        return "tier1"

    # --- Tier 2: Subject pattern match ---
    subject_match = match_by_subject(subject)
    if subject_match:
        rfq_number = subject_match.get("rfq_number")
        op_number = subject_match.get("opportunity_number")

        # Verify RFQ exists in DB (rfq_number in DB is 'RFQ-2026-0032' format)
        rfq = None
        if rfq_number:
            full_rfq_number = f"RFQ-{rfq_number}" if not rfq_number.upper().startswith("RFQ-") else rfq_number
            rfq = session.query(RFQ).filter(RFQ.rfq_number == full_rfq_number).first()

        if rfq or op_number:
            if dry_run:
                logger.info(
                    f"  [T2] Subject match: '{subject}' → "
                    f"rfq={rfq_number}, op={op_number}, direction={direction}"
                )
            else:
                tracking = EmailTracking(
                    gmail_thread_id=thread_id,
                    gmail_message_id=msg_meta["id"],
                    user_email=user_email,
                    rfq_id=str(rfq.id) if rfq else None,
                    rfq_token=f"RFQ-{rfq_number}" if rfq_number else None,
                    opportunity_id=op_number,
                    direction=direction,
                    subject=subject,
                    recipient_email=msg_meta.get("to", "").split(",")[0].strip(),
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                session.add(tracking)
            return "tier2"

    # --- Tier 3: Contact/domain match ---
    contact_match = match_by_contact(session, external_addresses, domain_index)
    if contact_match["match_type"]:
        if dry_run:
            logger.info(
                f"  [T3] {contact_match['match_type']} match: "
                f"supplier={contact_match['supplier_id']}, "
                f"customer={contact_match['customer_id']}, "
                f"direction={direction}, subject='{subject[:60]}'"
            )
        else:
            tracking = EmailTracking(
                gmail_thread_id=thread_id,
                gmail_message_id=msg_meta["id"],
                user_email=user_email,
                direction=direction,
                subject=subject,
                recipient_email=msg_meta.get("to", "").split(",")[0].strip(),
                supplier_id=contact_match["supplier_id"],
                customer_id=contact_match["customer_id"],
                match_type=contact_match["match_type"],
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add(tracking)
        return "tier3"

    return "skip"


def sync_mailbox(session, user_email: str, domain_index: dict, dry_run: bool = False) -> dict:
    """Run incremental sync for a single mailbox.

    Returns:
        dict with counts: {tier1, tier2, tier3, skipped, errors, new_history_id}
    """
    cursor = get_cursor(session, user_email)
    if cursor is None:
        logger.info(f"No cursor for {user_email} — auto-initializing")
        cursor = initialize_cursor(session, user_email, dry_run)
        if cursor is None:
            return {"error": "init_failed"}
        # First run after init: cursor points to current state, nothing to process yet
        return {"tier1": 0, "tier2": 0, "tier3": 0, "skipped": 0, "errors": 0, "initialized": True}

    service = get_gmail_client(user_email)
    counts = {"tier1": 0, "tier2": 0, "tier3": 0, "skipped": 0, "errors": 0}
    new_history_id = cursor
    seen_message_ids = set()

    try:
        # Fetch history delta
        page_token = None
        while True:
            kwargs = {
                "userId": "me",
                "startHistoryId": cursor,
                "historyTypes": ["messageAdded"],
            }
            if page_token:
                kwargs["pageToken"] = page_token

            response = service.users().history().list(**kwargs).execute()
            history_records = response.get("history", [])
            new_history_id = int(response.get("historyId", cursor))

            for record in history_records:
                for msg_added in record.get("messagesAdded", []):
                    msg = msg_added.get("message", {})
                    msg_id = msg.get("id")
                    if not msg_id or msg_id in seen_message_ids:
                        continue
                    seen_message_ids.add(msg_id)

                    result = process_message(
                        session, service, user_email, msg_id, domain_index, dry_run
                    )
                    counts[result] = counts.get(result, 0) + 1

            page_token = response.get("nextPageToken")
            if not page_token:
                break

    except Exception as e:
        error_msg = str(e)
        # historyId too old — re-initialize
        if "notFound" in error_msg or "historyId" in error_msg.lower():
            logger.warning(f"History expired for {user_email}, re-initializing cursor")
            initialize_cursor(session, user_email, dry_run)
            return {"error": "history_expired", "reinitialized": True}
        logger.error(f"Error syncing {user_email}: {e}")
        counts["errors"] += 1

    # Update cursor
    if not dry_run and new_history_id > cursor:
        update_cursor(session, user_email, new_history_id)
        session.commit()

    counts["new_history_id"] = new_history_id
    return counts


def main():
    parser = argparse.ArgumentParser(description="Sync Gmail mailboxes and match emails")
    parser.add_argument("--init", action="store_true", help="Initialize sync cursors for all enabled mailboxes")
    parser.add_argument("--user", type=str, help="Scan only a specific user's mailbox")
    parser.add_argument("--dry-run", action="store_true", help="Show matches without writing to database")
    args = parser.parse_args()

    session = get_session()

    try:
        if args.user:
            mailboxes = [args.user]
        else:
            mailboxes = get_enabled_mailboxes(session)

        if not mailboxes:
            logger.warning("No enabled mailboxes found. Add entries to mailbox_scan_config.")
            return

        logger.info(f"Processing {len(mailboxes)} mailbox(es)")

        if args.init:
            for email in mailboxes:
                initialize_cursor(session, email, args.dry_run)
            if not args.dry_run:
                session.commit()
            logger.info("Cursor initialization complete")
            return

        # Build domain index once (shared across all mailboxes)
        logger.info("Building domain index...")
        domain_index = build_domain_index(session)
        logger.info(f"Domain index: {len(domain_index)} domains mapped")

        # Sync each mailbox
        total = {"tier1": 0, "tier2": 0, "tier3": 0, "skipped": 0, "errors": 0}
        for email in mailboxes:
            logger.info(f"Syncing {email}...")
            result = sync_mailbox(session, email, domain_index, args.dry_run)
            if "error" in result:
                logger.warning(f"  {email}: {result}")
                continue
            for key in ("tier1", "tier2", "tier3", "skipped", "errors"):
                total[key] += result.get(key, 0)
            logger.info(
                f"  {email}: T1={result.get('tier1',0)} T2={result.get('tier2',0)} "
                f"T3={result.get('tier3',0)} skip={result.get('skipped',0)}"
            )

        logger.info(
            f"TOTAL: T1={total['tier1']} T2={total['tier2']} T3={total['tier3']} "
            f"skipped={total['skipped']} errors={total['errors']}"
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
