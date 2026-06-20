"""Backfill sender_email on email_tracking records.

For records where sender_email is NULL, populates it from existing data:
- If user_email is @eagle-exports.com → sender_email = user_email (normal case)
- If user_email is external → sender_email = user_email, then swap user_email = recipient_email

Usage:
    uv run python -m scripts.backfill_sender_email [--dry-run] [--limit N]
"""

import argparse
import logging
from sqlalchemy import text

from includes.dashboard.database import get_session

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def backfill(session, dry_run: bool = False, limit: int | None = None):
    """Backfill sender_email for records missing it, in small batches."""
    query = text("""
        SELECT id, user_email, recipient_email, direction
        FROM email_tracking
        WHERE sender_email IS NULL
        ORDER BY id
    """)
    if limit:
        query = text(f"""
            SELECT id, user_email, recipient_email, direction
            FROM email_tracking
            WHERE sender_email IS NULL
            ORDER BY id
            LIMIT {int(limit)}
        """)

    rows = session.execute(query).fetchall()
    total = len(rows)
    logger.info(f"Found {total} records with NULL sender_email (processing in batches of 50)")

    if dry_run:
        case1 = sum(1 for r in rows if "eagle-exports" in (r[1] or ""))
        case2 = sum(1 for r in rows if "eagle-exports" not in (r[1] or ""))
        logger.info(f"  [dry-run] Case 1 (user_email is Eagle): {case1}")
        logger.info(f"  [dry-run] Case 2 (user_email is external): {case2}")
        return

    updated = 0
    swapped = 0
    batch = 0
    BATCH_SIZE = 50

    for i, row in enumerate(rows):
        record_id = row[0]
        user_email = row[1]
        recipient_email = row[2]
        direction = row[3]

        if not user_email:
            continue

        if direction in ("sent", "draft"):
            # Sender is the Eagle user, recipient stays as external
            session.execute(
                text("UPDATE email_tracking SET sender_email = :ue WHERE id = :id"),
                {"ue": user_email, "id": record_id},
            )
            updated += 1
        elif direction == "received":
            # Sender is the external party (stored in recipient_email)
            # Recipient is the Eagle user (stored in user_email)
            session.execute(
                text("""
                    UPDATE email_tracking
                    SET sender_email = recipient_email,
                        recipient_email = user_email
                    WHERE id = :id
                """),
                {"id": record_id},
            )
            swapped += 1
        else:
            # Unknown direction — just copy
            session.execute(
                text("UPDATE email_tracking SET sender_email = :ue WHERE id = :id"),
                {"ue": user_email, "id": record_id},
            )
            updated += 1

        batch += 1
        if batch >= BATCH_SIZE:
            session.commit()
            logger.info(f"  Progress: {i + 1}/{total} ({updated} updated, {swapped} swapped)")
            batch = 0

    if batch > 0:
        session.commit()

    logger.info(f"Done: {updated} updated in place, {swapped} swapped")


def main():
    parser = argparse.ArgumentParser(description="Backfill sender_email on email_tracking")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    session = get_session()
    try:
        backfill(session, dry_run=args.dry_run, limit=args.limit)
    finally:
        session.close()


if __name__ == "__main__":
    main()
