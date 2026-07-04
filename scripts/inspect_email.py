"""Inspect email records from the production database.

Usage:
    python scripts/inspect_email.py 16640              # Single email by ID
    python scripts/inspect_email.py 16640 16641 16642  # Multiple emails
    python scripts/inspect_email.py --gmail 19f20eb067e18759  # By Gmail message ID
    python scripts/inspect_email.py --rfq OP71449       # All emails for an RFQ
    python scripts/inspect_email.py --recent 20          # Last N received emails
    python scripts/inspect_email.py --summary 16640      # Compact summary view
    python scripts/inspect_email.py --attachments 16640  # Show attachment details only

Requires PROD_DATABASE_URL to be set in .env.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DB_URL = os.getenv("PROD_DATABASE_URL", "")
if not DB_URL:
    print("ERROR: PROD_DATABASE_URL not set in .env")
    sys.exit(1)

# Ensure sync driver
if "+asyncpg" in DB_URL:
    DB_URL = DB_URL.replace("postgresql+asyncpg", "postgresql+psycopg")
elif DB_URL.startswith("postgresql://"):
    DB_URL = DB_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(DB_URL)


def _fmt_ts(ts):
    if not ts:
        return "—"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def _fmt_size(size_bytes):
    if size_bytes is None:
        return "?"
    if size_bytes < 1024:
        return f"{size_bytes}B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    return f"{size_bytes / (1024 * 1024):.1f}MB"


def _parse_attachments(attachments_json):
    """Return (real_attachments, inline_attachments) from JSONB field."""
    real = []
    inline = []
    items = attachments_json or []
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except json.JSONDecodeError:
            items = []
    for a in items:
        if not isinstance(a, dict):
            continue
        if a.get("inline"):
            inline.append(a)
        else:
            real.append(a)
    return real, inline


def fetch_by_ids(ids: list[int]):
    """Fetch email records by internal ID."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, gmail_message_id, gmail_thread_id, direction, email_type,
                       subject, sender_email, sender_name, recipient_email,
                       sent_at, attachments_json,
                       pg_column_size(attachments_json) as att_size
                FROM email_tracking
                WHERE id = ANY(:ids)
                ORDER BY id
            """),
            {"ids": ids},
        ).fetchall()
    return rows


def fetch_by_gmail_id(gmail_message_id: str):
    """Fetch a single email by Gmail message ID."""
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT id, gmail_message_id, gmail_thread_id, direction, email_type,
                       subject, sender_email, sender_name, recipient_email,
                       sent_at, attachments_json,
                       pg_column_size(attachments_json) as att_size
                FROM email_tracking
                WHERE gmail_message_id = :mid
            """),
            {"mid": gmail_message_id},
        ).fetchone()
    return [row] if row else []


def fetch_by_rfq(rfq_id: str):
    """Fetch all emails linked to an RFQ."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, gmail_message_id, gmail_thread_id, direction, email_type,
                       subject, sender_email, sender_name, recipient_email,
                       sent_at, attachments_json,
                       pg_column_size(attachments_json) as att_size
                FROM email_tracking
                WHERE rfq_id = :rfq
                ORDER BY sent_at DESC NULLS LAST
            """),
            {"rfq": rfq_id},
        ).fetchall()
    return rows


def fetch_recent(limit: int):
    """Fetch last N received emails."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, gmail_message_id, gmail_thread_id, direction, email_type,
                       subject, sender_email, sender_name, recipient_email,
                       sent_at, attachments_json,
                       pg_column_size(attachments_json) as att_size
                FROM email_tracking
                WHERE direction = 'received'
                ORDER BY sent_at DESC NULLS LAST
                LIMIT :limit
            """),
            {"limit": limit},
        ).fetchall()
    return rows


def print_full(rows):
    """Print full details for each email."""
    for r in rows:
        real, inline = _parse_attachments(r[10])
        print("=" * 80)
        print(f"ID: {r[0]}  |  Gmail Msg: {r[1]}  |  Thread: {r[2]}")
        print(f"Direction: {r[3]}  |  Type: {r[4] or '—'}")
        print(f"Subject: {r[5] or '—'}")
        print(f"From: {r[6] or '—'}  ({r[7] or 'unknown'})")
        print(f"To: {r[8] or '—'}")
        print(f"Sent: {_fmt_ts(r[9])}")
        print(f"Attachments JSON size: {r[11]} bytes")
        if real:
            print(f"\n  📎 Real attachments ({len(real)}):")
            for a in real:
                print(f"     • {a.get('filename', '?')}  ({a.get('mime_type', '?')}, {_fmt_size(a.get('size'))})")
        if inline:
            print(f"\n  🖼️  Inline/embedded ({len(inline)}):")
            for a in inline:
                print(f"     • {a.get('filename', '?')}  ({a.get('mime_type', '?')}, {_fmt_size(a.get('size'))})")
        if not real and not inline:
            print("\n  📎 No attachments")
        print()


def print_summary(rows):
    """Print a compact one-line summary per email."""
    header = f"{'ID':>6}  {'Dir':>4}  {'Sent':<20}  {'Attach':<10}  Subject"
    print(header)
    print("-" * len(header))
    for r in rows:
        real, inline = _parse_attachments(r[10])
        attach_summary = ""
        if real and inline:
            attach_summary = f"{len(real)} real + {len(inline)} inl"
        elif real:
            attach_summary = f"{len(real)} real"
        elif inline:
            attach_summary = f"{len(inline)} inl"
        else:
            attach_summary = "none"
        print(f"{r[0]:>6}  {r[3]:>4}  {_fmt_ts(r[9]):<20}  {attach_summary:<10}  {r[5] or '—'}")


def print_attachments(rows):
    """Print attachment details only, with Gmail IDs for further inspection."""
    for r in rows:
        real, inline = _parse_attachments(r[10])
        print(f"Email ID {r[0]}  |  Subject: {r[5] or '—'}")
        all_att = real + inline
        if not all_att:
            print("  (no attachments)")
            continue
        for a in all_att:
            tag = "📎" if not a.get("inline") else "🖼️ "
            gmail_id = a.get("gmail_attachment_id", "?")
            print(f"  {tag} {a.get('filename','?')}  ({a.get('mime_type','?')}, {_fmt_size(a.get('size'))})")
            print(f"     Gmail attachmentId: {gmail_id}")
            if a.get("inline"):
                print(f"     ⚠️  Flagged inline — hidden in UI")
        print()


def main():
    parser = argparse.ArgumentParser(description="Inspect email records from production DB")
    parser.add_argument("ids", nargs="*", type=int, help="Email ID(s) to inspect")
    parser.add_argument("--gmail", type=str, help="Gmail message ID to look up")
    parser.add_argument("--rfq", type=str, help="RFQ number to find emails for")
    parser.add_argument("--recent", type=int, help="Show last N received emails")
    parser.add_argument("--summary", nargs="*", type=int, help="Compact summary for given IDs")
    parser.add_argument("--attachments", nargs="*", type=int, help="Show attachment details only")
    args = parser.parse_args()

    rows = []

    if args.gmail:
        rows = fetch_by_gmail_id(args.gmail)
        print_full(rows)
        return

    if args.rfq:
        rows = fetch_by_rfq(args.rfq)
        print_full(rows)
        return

    if args.recent:
        rows = fetch_recent(args.recent)
        print_full(rows)
        return

    if args.summary is not None:
        if args.summary:
            rows = fetch_by_ids(args.summary)
        else:
            rows = fetch_recent(20)
        print_summary(rows)
        return

    if args.attachments is not None:
        if args.attachments:
            rows = fetch_by_ids(args.attachments)
        else:
            rows = fetch_recent(20)
        print_attachments(rows)
        return

    if args.ids:
        rows = fetch_by_ids(args.ids)
        print_full(rows)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
