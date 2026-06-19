"""Rebuild email content for selected email_tracking rows.

Clears body_markdown and attachments_json so the next view of the email
in the dashboard will re-fetch from Gmail with the latest fetch_message_content()
logic (cid: rewriting, decorative image filtering, etc.).

Usage:
    # Rebuild a specific RFQ's emails:
    uv run python -m scripts.rebuild_email_content --rfq RFQ-2026-0212

    # Rebuild specific Gmail message IDs:
    uv run python -m scripts.rebuild_email_content --msg-id 19edd4e92ea9ab9c

    # Rebuild the N most recent rows (test a sample):
    uv run python -m scripts.rebuild_email_content --recent 20

    # Dry run (see what would be rebuilt):
    uv run python -m scripts.rebuild_email_content --rfq RFQ-2026-0212 --dry-run
"""

import argparse
import logging
from sqlalchemy import text

from includes.dashboard.database import get_session

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def rebuild(session, rfq_id: str | None = None, msg_ids: list[str] | None = None,
            recent: int | None = None, dry_run: bool = False):
    """Clear cached content for matching email_tracking rows."""
    where_clauses = ["gmail_message_id IS NOT NULL"]
    params = {}

    if rfq_id:
        where_clauses.append("(rfq_id = :rfq_id OR rfq_token = :rfq_id2)")
        params["rfq_id"] = rfq_id
        params["rfq_id2"] = rfq_id
    elif msg_ids:
        placeholders = ", ".join(f":mid{i}" for i in range(len(msg_ids)))
        where_clauses.append(f"gmail_message_id IN ({placeholders})")
        for i, mid in enumerate(msg_ids):
            params[f"mid{i}"] = mid
    elif recent:
        pass  # handled via LIMIT
    else:
        logger.error("Specify --rfq, --msg-id, or --recent")
        return

    where_sql = " AND ".join(where_clauses)
    order_limit = ""
    if recent:
        order_limit = f"ORDER BY id DESC LIMIT {int(recent)}"

    query = text(f"""
        SELECT id, gmail_message_id, user_email, recipient_email, subject
        FROM email_tracking
        WHERE {where_sql}
        {order_limit}
    """)

    rows = session.execute(query, params).fetchall()
    logger.info(f"Found {len(rows)} row(s) to rebuild")

    if dry_run:
        for r in rows:
            logger.info(f"  [dry-run] Would rebuild id={r[0]}, msg={r[1]}, "
                        f"user={r[2]}, subject={(r[4] or '')[:60]}")
        return

    cleared = 0
    for r in rows:
        session.execute(
            text("UPDATE email_tracking SET body_markdown = NULL, "
                 "body_html = NULL, attachments_json = NULL, "
                 "sender_name = NULL, all_recipients = NULL, "
                 "updated_at = NOW() WHERE id = :id"),
            {"id": r[0]},
        )
        cleared += 1

    session.commit()
    logger.info(f"Cleared cached content for {cleared} row(s). "
                "Next view in the dashboard will re-fetch from Gmail with new logic.")


def main():
    parser = argparse.ArgumentParser(description="Rebuild email content cache")
    parser.add_argument("--rfq", help="RFQ ID (e.g. RFQ-2026-0212)")
    parser.add_argument("--msg-id", action="append", dest="msg_ids",
                        help="Gmail message ID (can repeat)")
    parser.add_argument("--recent", type=int, help="Rebuild N most recent rows")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    session = get_session()
    try:
        rebuild(session, rfq_id=args.rfq, msg_ids=args.msg_ids,
                recent=args.recent, dry_run=args.dry_run)
    finally:
        session.close()


if __name__ == "__main__":
    main()
