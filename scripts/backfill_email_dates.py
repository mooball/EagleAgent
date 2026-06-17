"""Backfill missing sent_at dates on email_tracking records.

For every email_tracking row with a null sent_at and valid gmail_message_id,
fetches just the Date header from Gmail API (metadata-only, lightweight)
and updates the record.

Usage:
    uv run python -m scripts.backfill_email_dates [--dry-run] [--limit N]
"""

import argparse
import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from sqlalchemy import text

from includes.dashboard.database import get_session
from includes.gmail import get_gmail_client

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Delay between Gmail API requests to stay under quota (250 units/sec, 5 units/req = 50 req/sec)
_REQUEST_DELAY = 0.03  # ~33 req/sec — well under the 50/sec limit
_BATCH_SIZE = 100  # commit every N records


def backfill(session, dry_run: bool = False, limit: int | None = None):
    """Backfill sent_at for all email_tracking records that are missing it."""
    query = text("""
        SELECT id, gmail_message_id, user_email, gmail_thread_id
        FROM email_tracking
        WHERE sent_at IS NULL AND gmail_message_id IS NOT NULL
        ORDER BY id
    """)
    if limit:
        query = text(f"""
            SELECT id, gmail_message_id, user_email, gmail_thread_id
            FROM email_tracking
            WHERE sent_at IS NULL AND gmail_message_id IS NOT NULL
            ORDER BY id
            LIMIT {limit}
        """)

    rows = session.execute(query).fetchall()
    total = len(rows)
    logger.info(f"Found {total} records with missing sent_at")

    if dry_run:
        logger.info("[dry-run] Would update {} records. Exiting.".format(total))
        return

    updated = 0
    skipped_404 = 0
    skipped_no_date = 0
    errors = 0
    gmail_clients: dict[str, object] = {}  # user_email -> Gmail client

    for i, row in enumerate(rows):
        record_id = row[0]
        message_id = row[1]
        user_email = row[2]

        try:
            # Get or reuse Gmail client for this user
            if user_email not in gmail_clients:
                gmail_clients[user_email] = get_gmail_client(user_email)
            service = gmail_clients[user_email]

            # Fetch just the Date header (metadata-only — 5 quota units)
            msg = service.users().messages().get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["Date"],
            ).execute()

            headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
            date_header = headers.get("date", "")

            if date_header:
                sent_at = parsedate_to_datetime(date_header)
                session.execute(
                    text("UPDATE email_tracking SET sent_at = :sent_at WHERE id = :id"),
                    {"sent_at": sent_at, "id": record_id},
                )
                updated += 1
            else:
                skipped_no_date += 1

            time.sleep(_REQUEST_DELAY)

        except Exception as e:
            error_str = str(e)
            if "404" in error_str or "not found" in error_str.lower():
                skipped_404 += 1
            else:
                logger.warning(f"  Error on id={record_id}, msg={message_id}: {e}")
                errors += 1

        # Progress & batch commit
        if (i + 1) % _BATCH_SIZE == 0:
            session.commit()
            logger.info(f"  Progress: {i + 1}/{total} — {updated} updated, {skipped_404} not found, {skipped_no_date} no date, {errors} errors")

    session.commit()
    logger.info(f"Backfill complete: {updated} updated, {skipped_404} not found, {skipped_no_date} no date, {errors} errors")


def main():
    parser = argparse.ArgumentParser(description="Backfill missing sent_at dates on email_tracking")
    parser.add_argument("--dry-run", action="store_true", help="Count records without updating")
    parser.add_argument("--limit", type=int, help="Limit to N records (for testing)")
    args = parser.parse_args()

    session = get_session()
    try:
        backfill(session, dry_run=args.dry_run, limit=args.limit)
    finally:
        session.close()


if __name__ == "__main__":
    main()
