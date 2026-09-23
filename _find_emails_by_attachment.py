"""READ-ONLY prod probe: find emails by attachment count / attachment filename.

Usage:
    uv run python _find_emails_by_attachment.py                # top 20 by attachment count
    uv run python _find_emails_by_attachment.py QBRI1207       # emails whose attachments match
"""
import json
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DB_URL = os.environ["PROD_DATABASE_URL"]
if "+asyncpg" in DB_URL:
    DB_URL = DB_URL.replace("postgresql+asyncpg", "postgresql+psycopg")
elif DB_URL.startswith("postgresql://"):
    DB_URL = DB_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(DB_URL)

term = sys.argv[1] if len(sys.argv) > 1 else None

with engine.connect() as conn:
    if term:
        rows = conn.execute(text("""
            SELECT id, subject, sender_email, sent_at, rfq_token,
                   jsonb_array_length(attachments_json) AS n_att,
                   rfq_creation_result, supplier_pipeline_result
            FROM email_tracking
            WHERE attachments_json::text ILIKE :t
            ORDER BY jsonb_array_length(attachments_json) DESC
        """), {"t": f"%{term}%"}).fetchall()
        print(f"=== emails with an attachment matching '{term}': {len(rows)} ===\n")
    else:
        rows = conn.execute(text("""
            SELECT id, subject, sender_email, sent_at, rfq_token,
                   jsonb_array_length(attachments_json) AS n_att,
                   rfq_creation_result, supplier_pipeline_result
            FROM email_tracking
            WHERE attachments_json IS NOT NULL
              AND jsonb_array_length(attachments_json) > 0
            ORDER BY jsonb_array_length(attachments_json) DESC
            LIMIT 20
        """)).fetchall()
        print("=== top 20 emails by attachment count ===\n")

    for r in rows:
        (eid, subject, sender, sent_at, token, n_att,
         rcr, spr) = r
        print(f"id={eid}  attachments={n_att}  {sent_at}")
        print(f"  from    : {sender}")
        print(f"  subject : {(subject or '')[:110]}")
        print(f"  rfq     : {token or (rcr or {}).get('rfq_number')}")
        print(f"  rcr     : status={(rcr or {}).get('status')} "
              f"items={(rcr or {}).get('item_count')} "
              f"error={(rcr or {}).get('error')}")
        if spr and spr.get("error"):
            print(f"  quote   : ERROR {spr['error'][:80]}")
        print()
