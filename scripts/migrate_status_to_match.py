"""Migrate rfq_items.status column to match.

Renames the 'status' column to 'match' and re-classifies existing items
based on their actual data (part_number, brand, input_description) rather
than the old status values. Preserves 'review' → 'discrepancy' flags.

Usage:
    python scripts/migrate_status_to_match.py              # dry-run (local DB)
    python scripts/migrate_status_to_match.py --apply      # execute (local DB)
    python scripts/migrate_status_to_match.py --prod        # dry-run (production)
    python scripts/migrate_status_to_match.py --prod --apply # execute (production)

Idempotent: safe to re-run. If 'match' column already exists, skips rename.
"""

import os
import sys
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

# =============================================================================
# Configuration
# =============================================================================

DRY_RUN = "--apply" not in sys.argv
USE_PROD = "--prod" in sys.argv

db_url = (
    os.getenv("PROD_DATABASE_URL")
    if USE_PROD
    else os.getenv("DATABASE_URL", "")
)
if not db_url:
    print("ERROR: Neither PROD_DATABASE_URL nor DATABASE_URL is set in .env")
    sys.exit(1)

if USE_PROD and not DRY_RUN:
    print("⚠️  PRODUCTION MODE — this will modify the live production database.")
    print(f"    URL: {db_url[:60]}...")
    resp = input("    Type 'yes' to confirm: ")
    if resp.strip().lower() != "yes":
        print("Aborted.")
        sys.exit(0)

# Normalize driver — scripts use psycopg (sync), not asyncpg
if "+asyncpg" in db_url:
    db_url = db_url.replace("postgresql+asyncpg", "postgresql+psycopg")
elif db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

DRY_RUN = "--apply" not in sys.argv

engine = create_engine(db_url)

# =============================================================================
# Helpers
# =============================================================================


def fmt_count(label: str, count: int) -> str:
    return f"  {label:.<30} {count:>6}"


def run_sql(conn, sql: str, params: dict = None):
    """Execute a statement. In dry-run mode, print it instead."""
    if DRY_RUN:
        printed = sql.strip()
        if params:
            printed += f"\n  -- params: {params}"
        print(f"\n[DRY-RUN] Would execute:\n{printed}\n")
        return None
    return conn.execute(text(sql), params or {})


# =============================================================================
# Main
# =============================================================================

with engine.connect() as conn:
    # --- 1. Check current state -------------------------------------------------
    col_check = conn.execute(text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'rfq_items'
          AND column_name IN ('status', 'match')
        ORDER BY column_name
    """)).fetchall()

    existing_cols = {row[0] for row in col_check}

    if "match" in existing_cols:
        print("✓ Column 'match' already exists. Skipping rename.")
        col_name = "match"
    elif "status" in existing_cols:
        col_name = "status"
        print("Found 'status' column — will rename to 'match'.")
    else:
        print("ERROR: Neither 'status' nor 'match' column found on rfq_items.")
        sys.exit(1)

    # --- 2. Show current distribution -------------------------------------------
    print(f"\nCurrent '{col_name}' distribution:")
    rows = conn.execute(text(f"""
        SELECT {col_name}, COUNT(*) AS cnt
        FROM rfq_items
        GROUP BY {col_name}
        ORDER BY cnt DESC
    """)).fetchall()
    current_total = sum(r[1] for r in rows)
    for val, cnt in rows:
        print(fmt_count(val or "(null)", cnt))
    print(fmt_count("TOTAL", current_total))

    # --- 3. Rename column (if needed) -------------------------------------------
    if col_name == "status":
        print("\n--- Renaming column ---")
        run_sql(conn, 'ALTER TABLE rfq_items RENAME COLUMN status TO "match"')
        if not DRY_RUN:
            conn.commit()
            print("✓ Column renamed: status → match")

    # --- 4. Drop old default (was 'unidentified') --------------------------------
    run_sql(conn, 'ALTER TABLE rfq_items ALTER COLUMN "match" DROP DEFAULT')

    # --- 5. Data migration -------------------------------------------------------
    print("\n--- Data migration ---")

    migration_sql = """
        UPDATE rfq_items SET "match" = CASE
            -- Preserve human review flags (these had notes explaining the issue)
            WHEN "match" = 'review' THEN 'discrepancy'

            -- Classify by available data
            WHEN part_number IS NOT NULL AND part_number != ''
                 AND brand IS NOT NULL AND brand != ''
                 AND input_description IS NOT NULL AND input_description != ''
              THEN 'specific'
            WHEN brand IS NOT NULL AND brand != ''
                 AND input_description IS NOT NULL AND input_description != ''
              THEN 'branded'
            WHEN input_description IS NOT NULL AND input_description != ''
              THEN 'generic'
            ELSE 'unmatched'
        END
    """

    run_sql(conn, migration_sql)
    if not DRY_RUN:
        conn.commit()

    # --- 6. Show new distribution ------------------------------------------------
    if DRY_RUN:
        # Can't query "match" column yet (rename hasn't happened).
        # Instead, run equivalent classification logic as a SELECT to preview.
        print("\nProjected 'match' distribution (from actual data):")
        preview_sql = """
            SELECT
                CASE
                    WHEN {col} = 'review' THEN 'discrepancy'
                    WHEN part_number IS NOT NULL AND part_number != ''
                         AND brand IS NOT NULL AND brand != ''
                         AND input_description IS NOT NULL AND input_description != ''
                      THEN 'specific'
                    WHEN brand IS NOT NULL AND brand != ''
                         AND input_description IS NOT NULL AND input_description != ''
                      THEN 'branded'
                    WHEN input_description IS NOT NULL AND input_description != ''
                      THEN 'generic'
                    ELSE 'unmatched'
                END AS new_match,
                COUNT(*) AS cnt
            FROM rfq_items
            GROUP BY new_match
            ORDER BY cnt DESC
        """.format(col=col_name)
        rows = conn.execute(text(preview_sql)).fetchall()
        new_total = sum(r[1] for r in rows)
        for val, cnt in rows:
            print(fmt_count(val or "(null)", cnt))
        print(fmt_count("TOTAL", new_total))

        if new_total != current_total:
            print(f"\n⚠️  WARNING: Projected row count differs! {current_total} → {new_total}")
    else:
        print("\nNew 'match' distribution:")
        rows = conn.execute(text("""
            SELECT "match", COUNT(*) AS cnt
            FROM rfq_items
            GROUP BY "match"
            ORDER BY cnt DESC
        """)).fetchall()
        new_total = sum(r[1] for r in rows)
        for val, cnt in rows:
            print(fmt_count(val or "(null)", cnt))
        print(fmt_count("TOTAL", new_total))

        if new_total != current_total:
            print(f"\n⚠️  WARNING: Row count changed! {current_total} → {new_total}")
            sys.exit(1)

    # --- 7. Spot-check review → discrepancy items --------------------------------
    if DRY_RUN:
        # In dry-run, query old 'review' items
        review_items = conn.execute(text(f"""
            SELECT line, notes
            FROM rfq_items
            WHERE {col_name} = 'review'
            LIMIT 5
        """)).fetchall()
        if review_items:
            print(f"\n--- Items that will become 'discrepancy' ({len(review_items)} of {sum(1 for r in rows if r[0] == 'review')}) ---")
            for line, notes in review_items:
                note_preview = (notes or "")[:80]
                print(f"  Line {line}: {note_preview}")
    else:
        discrepancy_items = conn.execute(text("""
            SELECT line, notes
            FROM rfq_items
            WHERE "match" = 'discrepancy'
            LIMIT 5
        """)).fetchall()
        if discrepancy_items:
            disco_count = conn.execute(text('SELECT COUNT(*) FROM rfq_items WHERE "match" = \'discrepancy\'')).scalar()
            print(f"\n--- Sample discrepancy items ({len(discrepancy_items)} of {disco_count}) ---")
            for line, notes in discrepancy_items:
                note_preview = (notes or "")[:80]
                print(f"  Line {line}: {note_preview}")

    # --- Done --------------------------------------------------------------------
    if DRY_RUN:
        conn.rollback()
        print("\n🔍 DRY-RUN complete. No changes made. Run with --apply to execute.")
    else:
        print("\n✅ Migration complete.")
