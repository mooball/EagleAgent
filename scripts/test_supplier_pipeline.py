"""
test_email_pipeline.py

Scan local email_tracking for N emails that match quote pipeline criteria
(received, linked to RFQ, linked to supplier) and run them through the full
classify → extract → interpret pipeline.

Outputs a detailed report showing:
  - Email details (ID, subject, supplier, RFQ)
  - Classification result
  - Extraction summary (body length, attachments processed)
  - Interpretation result (quotes extracted, confidence levels)
  - Post-pipeline RFQ verification (were supplier fields updated?)

Usage:
  uv run python -m scripts.test_email_pipeline               # process 5 emails
  uv run python -m scripts.test_email_pipeline --count 20    # process 20 emails
  uv run python -m scripts.test_email_pipeline --offset 10   # skip first 10 candidates
  uv run python -m scripts.test_email_pipeline --dry-run     # classify only, don't extract/apply
  uv run python -m scripts.test_email_pipeline --email-id 21363  # test a specific email
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def find_candidates(session, count: int, offset: int) -> list[dict]:
    """Find emails that match pipeline criteria: received + rfq_token + supplier_id."""
    rows = session.execute(text("""
        SELECT
            et.id,
            et.subject,
            et.sender_email,
            et.sender_name,
            et.rfq_token,
            et.user_email,
            et.direction,
            et.created_at,
            et.body_markdown IS NOT NULL AS has_body,
            et.attachments_json IS NOT NULL
                AND jsonb_array_length(et.attachments_json) > 0 AS has_attachments,
            CASE WHEN et.attachments_json IS NOT NULL
                 THEN jsonb_array_length(et.attachments_json)
                 ELSE 0 END AS attachment_count,
            s.name AS supplier_name
        FROM email_tracking et
        LEFT JOIN suppliers s ON et.supplier_id = s.id
        WHERE et.direction = 'received'
          AND et.rfq_token IS NOT NULL
          AND et.supplier_id IS NOT NULL
        ORDER BY et.created_at DESC NULLS LAST
        LIMIT :limit OFFSET :offset
    """), {"limit": count, "offset": offset}).mappings().all()

    return [dict(r) for r in rows]


def verify_rfq_quotes(session, rfq_token: str, supplier_name: str) -> dict:
    """Check the RFQ to see if supplier quote fields have been populated."""
    from includes.tools.rfq_crud import _get_rfq_dict_sync

    rfq = _get_rfq_dict_sync(rfq_token)
    if not rfq:
        return {"error": f"RFQ {rfq_token} not found"}

    result = {
        "rfq_token": rfq_token,
        "items_total": len(rfq.get("items", [])),
        "supplier_quoted_items": 0,
        "supplier_declined_items": 0,
        "supplier_on_items": 0,
        "has_supplier_meta": False,
        "meta": None,
    }

    # Check items for this supplier
    for item in rfq.get("items", []):
        for s in (item.get("suppliers") or []):
            if s.get("name", "").lower() == supplier_name.lower():
                result["supplier_on_items"] += 1
                qs = s.get("quote_status")
                if qs == "quoted":
                    result["supplier_quoted_items"] += 1
                elif qs == "declined":
                    result["supplier_declined_items"] += 1

    # Check supplier meta
    meta = rfq.get("supplier_meta") or {}
    if supplier_name in meta:
        result["has_supplier_meta"] = True
        result["meta"] = meta[supplier_name]

    return result


def run_pipeline(email_id: int, dry_run: bool = False) -> dict:
    """Run the full pipeline on a single email. Returns detailed result."""
    from includes.tools.supplier_quote_pipeline import (
        _classify_supplier_email_sync,
        _extract_email_content_sync,
        _interpret_quote_sync,
        _apply_quote_data,
    )

    result = {
        "email_id": email_id,
        "stages": {},
        "timing": {},
        "success": False,
    }

    # Stage 1: Classify
    t0 = time.time()
    classification = _classify_supplier_email_sync(email_id)
    result["timing"]["classify"] = round(time.time() - t0, 2)
    result["stages"]["classify"] = classification

    if classification["classification"] != "quote_response":
        result["stages"]["outcome"] = f"Skipped: {classification['classification']}"
        return result

    if dry_run:
        result["stages"]["outcome"] = "Dry run — would proceed to extraction"
        return result

    # Stage 2: Extract
    t0 = time.time()
    content_bundle = _extract_email_content_sync(email_id)
    result["timing"]["extract"] = round(time.time() - t0, 2)
    result["stages"]["extract"] = {
        "content_length": len(content_bundle),
        "has_attachments": "## Attachment:" in content_bundle,
        "attachment_sections": content_bundle.count("## Attachment:"),
        "preview": content_bundle[:300] + "..." if len(content_bundle) > 300 else content_bundle,
    }

    if content_bundle.startswith("Error:"):
        result["stages"]["outcome"] = f"Extraction failed: {content_bundle}"
        return result

    # Stage 3: Interpret
    rfq_id = classification.get("rfq_id")
    supplier_name = classification.get("supplier_name") or "Unknown"

    t0 = time.time()
    quote_data = _interpret_quote_sync(rfq_id, supplier_name, content_bundle)
    result["timing"]["interpret"] = round(time.time() - t0, 2)
    result["stages"]["interpret"] = quote_data

    if "error" in quote_data:
        result["stages"]["outcome"] = f"Interpretation failed: {quote_data['error']}"
        return result

    # Stage 4: Apply
    t0 = time.time()
    actions = _apply_quote_data(rfq_id, supplier_name, quote_data, "test-pipeline")
    result["timing"]["apply"] = round(time.time() - t0, 2)
    result["stages"]["apply"] = actions
    result["success"] = True
    result["stages"]["outcome"] = f"Applied {len(actions)} updates"

    return result


def print_report(candidate: dict, pipeline_result: dict, verification: dict | None):
    """Print a formatted report for one email."""
    print("\n" + "=" * 80)
    print(f"EMAIL #{candidate['id']}")
    print("=" * 80)
    print(f"  Subject:    {candidate['subject'] or '(none)'}")
    print(f"  From:       {candidate['sender_email']} ({candidate['supplier_name']})")
    print(f"  RFQ:        {candidate['rfq_token']}")
    print(f"  Has Body:   {candidate['has_body']}")
    print(f"  Attachments:{candidate['attachment_count']}")
    print(f"  Date:       {candidate['created_at']}")

    stages = pipeline_result.get("stages", {})
    timing = pipeline_result.get("timing", {})

    # Classification
    classify = stages.get("classify", {})
    print(f"\n  CLASSIFY:   {classify.get('classification', '?')} "
          f"({classify.get('reason', '')})")
    if timing.get("classify"):
        print(f"              [{timing['classify']}s]")

    # Extraction
    extract = stages.get("extract")
    if extract:
        print(f"\n  EXTRACT:    {extract['content_length']} chars, "
              f"{extract['attachment_sections']} attachment(s) processed")
        if timing.get("extract"):
            print(f"              [{timing['extract']}s]")

    # Interpretation
    interpret = stages.get("interpret")
    if interpret and "error" not in interpret:
        quotes = interpret.get("quotes", [])
        declined = interpret.get("declined_items", [])
        shipping = interpret.get("shipping")
        print(f"\n  INTERPRET:  {len(quotes)} quote(s), {len(declined)} declined")
        for q in quotes:
            conf = q.get("confidence", "?")
            lead = q.get("lead_time", "")
            lead_str = f" — {lead}" if lead else ""
            print(f"              Line {q.get('item_line')}: "
                  f"{q.get('price')} {q.get('currency', 'AUD')} "
                  f"[{conf}]{lead_str}")
        if declined:
            print(f"              Declined: lines {declined}")
        if shipping:
            print(f"              Shipping: {shipping.get('cost')} {shipping.get('currency', 'AUD')}")
        if interpret.get("terms"):
            print(f"              Terms: {interpret['terms']}")
        if interpret.get("warnings"):
            for w in interpret["warnings"]:
                print(f"              ⚠️  {w}")
        if timing.get("interpret"):
            print(f"              [{timing['interpret']}s]")
    elif interpret and "error" in interpret:
        print(f"\n  INTERPRET:  ERROR — {interpret['error']}")

    # Application
    apply_actions = stages.get("apply")
    if apply_actions:
        print(f"\n  APPLY:      {len(apply_actions)} action(s)")
        for a in apply_actions:
            print(f"              {a}")
        if timing.get("apply"):
            print(f"              [{timing['apply']}s]")

    # Verification
    if verification:
        print(f"\n  VERIFY:     {verification.get('supplier_quoted_items', 0)} items quoted, "
              f"{verification.get('supplier_declined_items', 0)} declined, "
              f"meta={'YES' if verification.get('has_supplier_meta') else 'no'}")

    # Outcome
    outcome = stages.get("outcome", "?")
    status = "✓" if pipeline_result.get("success") else "✗"
    total_time = sum(timing.values())
    print(f"\n  RESULT:     {status} {outcome} [{total_time:.1f}s total]")


def main():
    parser = argparse.ArgumentParser(description="Test email quote pipeline on historical data")
    parser.add_argument("--count", "-n", type=int, default=5, help="Number of emails to process")
    parser.add_argument("--offset", type=int, default=0, help="Skip N candidates from the start")
    parser.add_argument("--dry-run", action="store_true", help="Classify only, don't extract or apply")
    parser.add_argument("--email-id", type=int, help="Test a specific email ID")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    args = parser.parse_args()

    from includes.dashboard.database import get_session
    session = get_session()

    try:
        if args.email_id:
            # Single email mode
            candidates = session.execute(text("""
                SELECT
                    et.id, et.subject, et.sender_email, et.sender_name,
                    et.rfq_token, et.user_email, et.direction, et.created_at,
                    et.body_markdown IS NOT NULL AS has_body,
                    et.attachments_json IS NOT NULL
                        AND jsonb_array_length(et.attachments_json) > 0 AS has_attachments,
                    CASE WHEN et.attachments_json IS NOT NULL
                         THEN jsonb_array_length(et.attachments_json)
                         ELSE 0 END AS attachment_count,
                    s.name AS supplier_name
                FROM email_tracking et
                LEFT JOIN suppliers s ON et.supplier_id = s.id
                WHERE et.id = :eid
            """), {"eid": args.email_id}).mappings().all()
            candidates = [dict(r) for r in candidates]
        else:
            candidates = find_candidates(session, args.count, args.offset)

        if not candidates:
            print("No matching emails found.")
            return

        print(f"\n{'='*80}")
        print(f"EMAIL QUOTE PIPELINE TEST — {len(candidates)} candidate(s)")
        print(f"{'='*80}")
        print(f"Mode: {'DRY RUN (classify only)' if args.dry_run else 'FULL PIPELINE'}")

        results = []
        stats = {"total": len(candidates), "quote_response": 0, "not_quote": 0,
                 "needs_review": 0, "clarification_required": 0, "declined": 0,
                 "acknowledgement": 0, "applied": 0, "errors": 0}

        for candidate in candidates:
            pipeline_result = run_pipeline(candidate["id"], dry_run=args.dry_run)

            # Classify stats
            classification = pipeline_result["stages"].get("classify", {}).get("classification", "")
            if classification in stats:
                stats[classification] += 1

            if pipeline_result.get("success"):
                stats["applied"] += 1

            # Verify RFQ state after application
            verification = None
            if pipeline_result.get("success") and not args.dry_run:
                supplier_name = pipeline_result["stages"]["classify"].get("supplier_name", "")
                rfq_token = candidate["rfq_token"]
                if supplier_name and rfq_token:
                    verification = verify_rfq_quotes(session, rfq_token, supplier_name)

            results.append({
                "candidate": candidate,
                "pipeline": pipeline_result,
                "verification": verification,
            })

            if not args.json:
                print_report(candidate, pipeline_result, verification)

        # Summary
        if not args.json:
            print(f"\n{'='*80}")
            print("SUMMARY")
            print(f"{'='*80}")
            print(f"  Total:              {stats['total']}")
            print(f"  Quote Response:     {stats['quote_response']}")
            print(f"  Clarification:      {stats['clarification_required']}")
            print(f"  Declined:           {stats['declined']}")
            print(f"  Acknowledgement:    {stats['acknowledgement']}")
            print(f"  Not Quote:          {stats['not_quote']}")
            print(f"  Needs Review:       {stats['needs_review']}")
            print(f"  Applied:            {stats['applied']}")
            if stats.get("errors"):
                print(f"  Errors:             {stats['errors']}")
            print()

        if args.json:
            # JSON output for programmatic analysis
            output = {
                "stats": stats,
                "results": [
                    {
                        "email_id": r["candidate"]["id"],
                        "subject": r["candidate"]["subject"],
                        "supplier": r["candidate"]["supplier_name"],
                        "rfq": r["candidate"]["rfq_token"],
                        "classification": r["pipeline"]["stages"].get("classify", {}).get("classification"),
                        "success": r["pipeline"].get("success"),
                        "quotes_count": len(r["pipeline"]["stages"].get("interpret", {}).get("quotes", [])),
                        "timing": r["pipeline"].get("timing"),
                        "verification": r["verification"],
                    }
                    for r in results
                ],
            }
            print(json.dumps(output, indent=2, default=str))

    finally:
        session.close()


if __name__ == "__main__":
    main()
