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
# Suppress noisy Google API client log messages
for _name in ("googleapiclient.discovery_cache", "googleapiclient", "google.auth", "google_auth_httplib2", "urllib3"):
    logging.getLogger(_name).setLevel(logging.ERROR)

# Delay between Gmail API requests to stay under quota (250 units/sec, 5 units/req = 50 req/sec)
_REQUEST_DELAY = 0.03  # ~33 req/sec — well under the 50/sec limit
_BATCH_SIZE = 100  # commit every N records

# Gmail auth errors that mean the service account will NEVER be able to impersonate this user.
# Don't retry these — they're permanent.
_PERMANENT_AUTH_ERRORS = (
    "invalid_grant", "access_denied",
    "Invalid email", "Requested client not authorized",
)

# Auth errors that may be transient — retry once before giving up
_TRANSIENT_AUTH_ERRORS = (
    "unauthorized_client",
)


def backfill(session, dry_run: bool = False, limit: int | None = None):
    """Backfill sent_at for all email_tracking records that are missing it."""
    query = text("""
        SELECT id, gmail_message_id, user_email, recipient_email, gmail_thread_id
        FROM email_tracking
        WHERE sent_at IS NULL AND gmail_message_id IS NOT NULL
        ORDER BY id
    """)
    if limit:
        query = text(f"""
            SELECT id, gmail_message_id, user_email, recipient_email, gmail_thread_id
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
    skipped_no_access = 0
    errors = 0
    gmail_clients: dict[str, object] = {}  # user_email -> Gmail client
    no_access_users: set[str] = set()  # users the service account can't impersonate (permanent)
    transient_failures: dict[str, int] = {}  # user_email -> retry count

    def _is_eagle(email: str) -> bool:
        return "@eagle-exports.com" in (email or "")

    def _try_get_client(email: str) -> object | None:
        """Try to get or reuse a Gmail client. Returns client or None if blacklisted."""
        if not email or email in no_access_users:
            return None
        if email not in gmail_clients:
            try:
                gmail_clients[email] = get_gmail_client(email)
            except Exception as e:
                if any(err in str(e) for err in _PERMANENT_AUTH_ERRORS):
                    no_access_users.add(email)
                    logger.warning(f"  BLACKLISTED: {email} — {str(e)[:100]}")
                    return None
                raise
        return gmail_clients[email]

    def _get_client(email: str, recipient: str) -> object | None:
        """Get a Gmail client, trying the most likely eagle-exports.com address first."""
        # Try the record's user_email first
        client = _try_get_client(email)
        if client:
            return client
        # Try the recipient if it's an eagle address
        if _is_eagle(recipient):
            logger.debug(f"  Retrying with recipient: {recipient} (user_email {email} failed)")
            return _try_get_client(recipient)
        # If user_email is external, try the recipient anyway (one of them must be eagle)
        if email and not _is_eagle(email) and recipient:
            return _try_get_client(recipient)
        return None

    for i, row in enumerate(rows):
        record_id = row[0]
        message_id = row[1]
        user_email = row[2]
        recipient_email = row[3]

        try:
            # Skip if both user_email and recipient are blacklisted
            if user_email in no_access_users and (not recipient_email or recipient_email in no_access_users):
                skipped_no_access += 1
                continue

            # Get Gmail client — try user_email first, then recipient
            service = _get_client(user_email, recipient_email)
            if not service:
                skipped_no_access += 1
                continue

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
            if any(err in error_str for err in _PERMANENT_AUTH_ERRORS):
                no_access_users.add(user_email)
                skipped_no_access += 1
                logger.warning(f"  BLACKLISTED (permanent): {user_email} — {error_str[:120]}")
            elif any(err in error_str for err in _TRANSIENT_AUTH_ERRORS):
                transient_failures[user_email] = transient_failures.get(user_email, 0) + 1
                if transient_failures[user_email] >= 2:
                    no_access_users.add(user_email)
                    skipped_no_access += 1
                    logger.warning(f"  BLACKLISTED (transient×2): {user_email} — {error_str[:120]}")
                else:
                    errors += 1
            elif "404" in error_str or "not found" in error_str.lower():
                skipped_404 += 1
            else:
                logger.warning(f"  Error on id={record_id}, msg={message_id}: {e}")
                errors += 1

        # Progress & batch commit
        if (i + 1) % _BATCH_SIZE == 0:
            session.commit()
            logger.info(f"  Progress: {i + 1}/{total} — {updated} updated, {skipped_404} not found, {skipped_no_access} no access, {skipped_no_date} no date, {errors} errors")

    session.commit()
    logger.info(f"Backfill complete: {updated} updated, {skipped_404} not found, {skipped_no_access} no access, {skipped_no_date} no date, {errors} errors")
    if no_access_users:
        logger.info(f"Blacklisted addresses ({len(no_access_users)}):")
        for addr in sorted(no_access_users):
            logger.info(f"  {addr}")


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
