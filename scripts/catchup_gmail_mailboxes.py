"""One-time Gmail catch-up scan for a date range.

Fills the gap when the mailbox_sync_cursor was lost (e.g., after a table drop).
Uses users.messages.list with a date query instead of the History API,
so it doesn't depend on the cursor being intact.

Usage:
    uv run python -m scripts.catchup_gmail_mailboxes --since "2026-06-17T17:00" [--user EMAIL] [--dry-run]
"""

import argparse
import logging
import sys
from datetime import datetime, timezone

from includes.dashboard.database import get_session
from includes.dashboard.models import MailboxScanConfig
from includes.gmail import get_gmail_client
from includes.gmail.matching import build_domain_index
from scripts.sync_gmail_mailboxes import (
    get_enabled_mailboxes,
    process_message,
    update_cursor,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def catchup_mailbox(session, user_email: str, domain_index: dict, since: str, dry_run: bool = False) -> dict:
    """Scan a mailbox for messages since a given date and process them.

    Args:
        since: ISO datetime string, e.g. '2026-06-17T17:00'
    """
    service = get_gmail_client(user_email)
    counts = {"tier1": 0, "tier2": 0, "tier3": 0, "skip": 0, "error": 0, "total": 0}

    # Build Gmail search query: after YYYY/MM/DD
    dt = datetime.fromisoformat(since)
    query = f"after:{dt.strftime('%Y/%m/%d')}"

    logger.info(f"Scanning {user_email} for messages {query}...")

    page_token = None
    seen_ids = set()

    while True:
        kwargs = {
            "userId": "me",
            "q": query,
            "maxResults": 500,
        }
        if page_token:
            kwargs["pageToken"] = page_token

        response = service.users().messages().list(**kwargs).execute()
        messages = response.get("messages", [])

        for msg_ref in messages:
            msg_id = msg_ref["id"]
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)

            result = process_message(
                session, service, user_email, msg_id, domain_index, dry_run
            )
            counts[result] = counts.get(result, 0) + 1
            counts["total"] += 1

            if counts["total"] % 100 == 0:
                logger.info(f"  {user_email}: processed {counts['total']} messages...")

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    # Update the cursor to current Gmail historyId so normal sync resumes cleanly
    if not dry_run:
        profile = service.users().getProfile(userId="me").execute()
        current_history_id = int(profile.get("historyId", 0))
        update_cursor(session, user_email, current_history_id)
        session.commit()

    return counts


def main():
    parser = argparse.ArgumentParser(description="Catch-up Gmail scan for a date range")
    parser.add_argument("--since", required=True, help="ISO datetime to scan from, e.g. '2026-06-17T17:00'")
    parser.add_argument("--user", help="Scan only a specific mailbox")
    parser.add_argument("--dry-run", action="store_true", help="Show matches without writing")
    args = parser.parse_args()

    session = get_session()
    try:
        domain_index = build_domain_index(session)

        if args.user:
            mailboxes = [args.user]
        else:
            mailboxes = get_enabled_mailboxes(session)

        if not mailboxes:
            logger.info("No enabled mailboxes found")
            return

        logger.info(f"Catching up {len(mailboxes)} mailbox(es) since {args.since}")

        for email in mailboxes:
            try:
                counts = catchup_mailbox(session, email, domain_index, args.since, args.dry_run)
                logger.info(f"  {email}: {counts}")
            except Exception as e:
                logger.error(f"  {email}: FAILED — {e}")

        logger.info("Catch-up complete.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
