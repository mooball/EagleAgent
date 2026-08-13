"""
sync_feedback_emails.py

Pull all emails with supplier-quote feedback from production into the local DB
for offline analysis. For each feedback email this also syncs:
  - every email in the same Gmail thread (full context)
  - the related RFQ + items (supplier/customer FKs remapped to local UUIDs)

Only the affected RFQs/threads are replaced locally — the rest of the local
DB is left untouched.

Usage:
  uv run python -m scripts.sync_feedback_emails              # sync all feedback
  uv run python -m scripts.sync_feedback_emails --dry-run    # summary only
  uv run python -m scripts.sync_feedback_emails --limit 10   # most recent 10
"""

import argparse
import logging

from sqlalchemy import text

from scripts.sync_prod_mail_data import (
    get_engines,
    _adapt_row,
    _build_id_maps,
    _remap_customer_id,
    _remap_supplier_id,
    _remap_item_suppliers_json,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _excerpt(s: str, n: int = 90) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def main():
    parser = argparse.ArgumentParser(description="Sync feedback emails from production to local DB")
    parser.add_argument("--dry-run", action="store_true", help="Show summary without writing")
    parser.add_argument("--limit", type=int, default=None, help="Limit to N most recent feedback emails")
    args = parser.parse_args()

    prod_engine, local_engine = get_engines()

    with prod_engine.connect() as pc:
        rows = pc.execute(text(
            """
            SELECT et.id, et.gmail_thread_id, et.rfq_token, et.rfq_id, et.subject,
                   et.feedback, et.supplier_pipeline_result,
                   s.name AS supplier_name
            FROM email_tracking et
            LEFT JOIN suppliers s ON s.id = et.supplier_id
            WHERE et.feedback IS NOT NULL
            ORDER BY et.id DESC
            """
        )).mappings().all()

    if not rows:
        logger.info("No feedback emails found in production.")
        return

    if args.limit:
        rows = rows[: args.limit]

    logger.info(f"Feedback emails in production: {len(rows)}\n")
    for r in rows:
        fb = r["feedback"] or {}
        spr = r["supplier_pipeline_result"] or {}
        logger.info(
            f"  #{r['id']}  {r['rfq_token'] or r['rfq_id'] or '(no rfq)'}  |  "
            f"{r['supplier_name'] or '?'}  |  {spr.get('classification') or '?'}\n"
            f"      feedback ({fb.get('user')}): {_excerpt(fb.get('text'))}"
        )

    if args.dry_run:
        return

    thread_ids = sorted({r["gmail_thread_id"] for r in rows if r["gmail_thread_id"]})
    rfq_tokens = sorted({r["rfq_token"] or r["rfq_id"] for r in rows if (r["rfq_token"] or r["rfq_id"])})

    # ── Read everything needed from production ────────────────────────────
    with prod_engine.connect() as pc:
        emails = []
        if thread_ids:
            ph = ",".join(f"'{t}'" for t in thread_ids)
            emails = pc.execute(text(
                f"SELECT * FROM email_tracking WHERE gmail_thread_id IN ({ph})"
            )).mappings().all()

        rfqs = []
        items = []
        if rfq_tokens:
            ph = ",".join(f"'{t}'" for t in rfq_tokens)
            rfqs = pc.execute(text(f"SELECT * FROM rfqs WHERE rfq_number IN ({ph})")).mappings().all()
            # Fall back to UUID id match for any token that didn't match by number
            found_numbers = {r["rfq_number"] for r in rfqs}
            missing = [t for t in rfq_tokens if t not in found_numbers]
            if missing:
                uph = ",".join(f"'{t}'" for t in missing)
                extra = pc.execute(text(
                    f"SELECT * FROM rfqs WHERE id::text IN ({uph})"
                )).mappings().all()
                rfqs.extend(extra)
            if rfqs:
                rid_list = [str(r["id"]) for r in rfqs]
                iph = ",".join(f"'{rid}'" for rid in rid_list)
                items = pc.execute(text(
                    f"SELECT * FROM rfq_items WHERE rfq_id IN ({iph})"
                )).mappings().all()

        with local_engine.connect() as lc:
            supplier_map, customer_map = _build_id_maps(pc, lc)

    logger.info(
        f"\nSyncing: {len(emails)} emails ({len(thread_ids)} threads), "
        f"{len(rfqs)} RFQs, {len(items)} items"
    )

    # ── Write to local (replace only affected rows) ───────────────────────
    n_cust_miss = 0
    with local_engine.begin() as lc:
        for r in rfqs:
            lc.execute(text("DELETE FROM rfq_items WHERE rfq_id IN "
                            "(SELECT id FROM rfqs WHERE rfq_number = :n)"), {"n": r["rfq_number"]})
            lc.execute(text("DELETE FROM rfqs WHERE rfq_number = :n"), {"n": r["rfq_number"]})

            row_dict = dict(r)
            row_dict["opportunity_id"] = None  # opportunities not synced
            if row_dict.get("customer_id"):
                mapped = _remap_customer_id(row_dict["customer_id"], customer_map)
                if not mapped:
                    n_cust_miss += 1
                row_dict["customer_id"] = mapped
            cols = list(row_dict.keys())
            col_names = ", ".join(cols)
            vals = ", ".join(f":{c}" for c in cols)
            lc.execute(text(f"INSERT INTO rfqs ({col_names}) VALUES ({vals})"), _adapt_row(row_dict))

        for item in items:
            row_dict = dict(item)
            row_dict.pop("product_id", None)  # products not synced
            if row_dict.get("suppliers"):
                row_dict["suppliers"] = _remap_item_suppliers_json(row_dict["suppliers"], supplier_map)
            cols = list(row_dict.keys())
            col_names = ", ".join(cols)
            vals = ", ".join(f":{c}" for c in cols)
            lc.execute(text(f"INSERT INTO rfq_items ({col_names}) VALUES ({vals})"), _adapt_row(row_dict))

        for tid in thread_ids:
            lc.execute(text("DELETE FROM email_tracking WHERE gmail_thread_id = :tid"), {"tid": tid})

        n_supp_miss = 0
        for e in emails:
            row_dict = dict(e)
            if row_dict.get("supplier_id"):
                mapped = _remap_supplier_id(row_dict["supplier_id"], supplier_map)
                if not mapped:
                    n_supp_miss += 1
                row_dict["supplier_id"] = mapped
            if row_dict.get("customer_id"):
                row_dict["customer_id"] = _remap_customer_id(row_dict["customer_id"], customer_map)
            cols = list(row_dict.keys())
            col_names = ", ".join(cols)
            vals = ", ".join(f":{c}" for c in cols)
            lc.execute(text(f"INSERT INTO email_tracking ({col_names}) VALUES ({vals})"), _adapt_row(row_dict))

        lc.execute(text(
            "SELECT setval('email_tracking_id_seq', (SELECT COALESCE(MAX(id), 0) + 1 FROM email_tracking), false)"
        ))

    logger.info(f"  ✓ {len(emails)} emails, {len(rfqs)} RFQs, {len(items)} items")
    if n_cust_miss:
        logger.info(f"    ({n_cust_miss} RFQs had unmapped customer_id → NULL)")
    if n_supp_miss:
        logger.info(f"    ({n_supp_miss} emails had unmapped supplier_id → NULL)")
    logger.info("\nDone! Open the RFQ communications tab locally to review each thread.")


if __name__ == "__main__":
    main()
