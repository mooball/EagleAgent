"""Diagnostic tool for email pipeline failure analysis.

Connects to the PRODUCTION database (via PROD_DATABASE_URL env var) and
prints a diagnostic report on the supplier quote pipeline's health:

  1. RFQ item details for a specific RFQ (edit RFQ-2026-0602 to your target)
  2. The last 10 DEADLINE_EXCEEDED pipeline failures with:
     - Email metadata (sender, subject, timestamp)
     - Linked RFQ and item count
     - Attachment breakdown (count, sizes, inline vs non-inline)
     - The classify step's recommended quote_attachments (if any)
  3. Aggregate error type breakdown (DEADLINE_EXCEEDED, RESOURCE_EXHAUSTED, etc.)
  4. Overall pipeline success rate (succeeded / total processed)

Usage:
    uv run python scripts/debug_pipeline_failures.py

Requirements:
    - PROD_DATABASE_URL must be set in .env
    - Network access to the production PostgreSQL instance

This script is READ-ONLY — it performs only SELECT queries, no mutations.
Safe to run against production at any time.

Typical workflow:
    1. Run this script to see which emails are failing and why
    2. Note the email_tracking.id of a failing email
    3. Run the pipeline manually for that ID via the dashboard "Run" button
    4. Check the known_image_signatures table to see what was cached
    5. Re-run this script to confirm the failure is no longer in the list
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()
from sqlalchemy import create_engine, text

db_url = os.environ['PROD_DATABASE_URL']
if '+asyncpg' in db_url:
    db_url = db_url.replace('+asyncpg', '+psycopg')
elif 'postgresql://' in db_url and '+psycopg' not in db_url:
    db_url = db_url.replace('postgresql://', 'postgresql+psycopg://')
engine = create_engine(db_url)

with engine.connect() as conn:
    # Check RFQ items for RFQ-2026-0602
    items = conn.execute(text("""
        SELECT ri.line, ri.input_description, ri.part_number, ri.brand, ri.quantity
        FROM rfq_items ri
        JOIN rfqs r ON ri.rfq_id = r.id
        WHERE r.rfq_number = 'RFQ-2026-0602'
        ORDER BY ri.line
    """)).fetchall()
    print(f"Items for RFQ-2026-0602: {len(items)}")
    for i in items:
        print(f"  Line {i[0]}: {i[1]} | PN: {i[2]} | Brand: {i[3]} | Qty: {i[4]}")

    # DEADLINE_EXCEEDED failures with details
    print("\n=== DEADLINE_EXCEEDED FAILURES (details) ===")
    failures = conn.execute(text("""
        SELECT et.id, et.rfq_id, et.body_markdown, et.attachments_json,
               et.supplier_pipeline_result,
               et.sent_at, et.subject, et.sender_email
        FROM email_tracking et
        WHERE et.supplier_pipeline_result->>'error' IS NOT NULL
        AND et.supplier_pipeline_result->>'error' LIKE :pattern
        ORDER BY et.sent_at DESC
        LIMIT 10
    """), {'pattern': '%DEADLINE%'}).fetchall()
    
    for f in failures:
        body_len = len(f[2] or '')
        atts = f[3] or []
        num_att = len(atts) if isinstance(atts, list) else 0
        total_att_size = sum(a.get('size', 0) for a in atts if isinstance(a, dict)) if isinstance(atts, list) else 0
        inline_count = sum(1 for a in atts if isinstance(a, dict) and a.get('inline')) if isinstance(atts, list) else 0
        non_inline_count = num_att - inline_count
        pipeline_result = f[4] or {}
        quote_atts = pipeline_result.get('quote_attachments', [])
        
        # Get RFQ item count
        rfq_id = f[1]
        item_count = 0
        if rfq_id:
            ic = conn.execute(text("""
                SELECT count(*) FROM rfq_items ri JOIN rfqs r ON ri.rfq_id = r.id 
                WHERE r.rfq_number = :rfq
            """), {'rfq': rfq_id}).fetchone()
            item_count = ic[0] if ic else 0
        
        print(f"#{f[0]} | {f[7][:35]} | {f[5]}")
        print(f"  Subject: {(f[6] or '')[:80]}")
        print(f"  RFQ: {rfq_id} ({item_count} items)")
        print(f"  Body: {body_len} chars | Attachments: {non_inline_count} regular + {inline_count} inline ({total_att_size/1024:.0f}KB total)")
        if quote_atts:
            print(f"  quote_attachments filter: {quote_atts}")
        # Show attachment details
        if isinstance(atts, list):
            for a in atts:
                if isinstance(a, dict):
                    print(f"    - {a.get('filename', '?')} ({a.get('mime_type', '?')}) {a.get('size', 0)/1024:.0f}KB inline={a.get('inline', False)}")
        print()

    # Error type summary
    print("\n=== ALL PIPELINE ERROR TYPES ===")
    error_types = conn.execute(text("""
        SELECT 
            CASE 
                WHEN supplier_pipeline_result->>'error' LIKE '%DEADLINE%' THEN 'DEADLINE_EXCEEDED'
                WHEN supplier_pipeline_result->>'error' LIKE '%RESOURCE_EXHAUSTED%' THEN 'RESOURCE_EXHAUSTED'
                WHEN supplier_pipeline_result->>'error' LIKE '%empty response%' THEN 'EMPTY_RESPONSE'
                WHEN supplier_pipeline_result->>'error' LIKE '%MAX_TOKENS%' THEN 'MAX_TOKENS'
                ELSE 'OTHER: ' || left(supplier_pipeline_result->>'error', 60)
            END as error_type,
            count(*) as cnt
        FROM email_tracking
        WHERE supplier_pipeline_result->>'error' IS NOT NULL
        GROUP BY 1
        ORDER BY 2 DESC
    """)).fetchall()
    for et in error_types:
        print(f"  {et[0]}: {et[1]}")
    
    # Success stats
    print("\n=== PIPELINE SUCCESS STATS ===")
    stats = conn.execute(text("""
        SELECT 
            count(*) FILTER (WHERE supplier_pipeline_result IS NOT NULL) as total_processed,
            count(*) FILTER (WHERE supplier_pipeline_result->>'error' IS NULL AND supplier_pipeline_result IS NOT NULL) as succeeded,
            count(*) FILTER (WHERE supplier_pipeline_result->>'error' IS NOT NULL) as failed
        FROM email_tracking
        WHERE supplier_pipeline_result IS NOT NULL
    """)).fetchone()
    print(f"  Total processed: {stats[0]}")
    print(f"  Succeeded: {stats[1]}")
    print(f"  Failed: {stats[2]}")
    print(f"  Success rate: {stats[1]/stats[0]*100:.1f}%" if stats[0] > 0 else "  N/A")
