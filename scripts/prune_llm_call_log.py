#!/usr/bin/env python
"""Prune old rows from llm_call_log.

Telemetry is append-only and every LLM call writes a row, so the table grows
without bound. Retention defaults to Config.LLM_TELEMETRY_RETENTION_DAYS (30).

Runs in bounded batches so a first prune of a large table cannot hold a long
transaction or bloat WAL on the production database.

Usage:
    uv run python scripts/prune_llm_call_log.py --dry-run
    uv run python scripts/prune_llm_call_log.py --days 30 --yes
    uv run python scripts/prune_llm_call_log.py --stats
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from sqlalchemy import text  # noqa: E402

from config.settings import Config  # noqa: E402
from includes.dashboard.database import get_session  # noqa: E402


def show_stats(session) -> None:
    rows = session.execute(
        text(
            """
            SELECT
                count(*)                                        AS total,
                min(ts)                                         AS oldest,
                max(ts)                                         AS newest,
                count(*) FILTER (WHERE status <> 'ok')          AS failures,
                count(DISTINCT model)                           AS models
            FROM llm_call_log
            """
        )
    ).fetchone()
    if rows is None or not rows.total:
        print("llm_call_log is empty.")
        return
    print(f"  rows      : {rows.total:,}")
    print(f"  oldest    : {rows.oldest}")
    print(f"  newest    : {rows.newest}")
    print(f"  failures  : {rows.failures:,}")
    print(f"  models    : {rows.models}")
    by_model = session.execute(
        text(
            """
            SELECT model, count(*) AS calls,
                   round(avg(latency_ms)) AS avg_ms,
                   round(percentile_cont(0.9) WITHIN GROUP (ORDER BY latency_ms)) AS p90_ms
            FROM llm_call_log
            WHERE ts > now() - interval '24 hours'
            GROUP BY model
            ORDER BY calls DESC
            LIMIT 12
            """
        )
    ).fetchall()
    if by_model:
        print("\n  last 24h by model:")
        print(f"    {'model':<26}{'calls':>8}{'avg ms':>9}{'p90 ms':>9}")
        for row in by_model:
            print(f"    {row.model:<26}{row.calls:>8}{row.avg_ms or 0:>9}{row.p90_ms or 0:>9}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Prune llm_call_log.")
    parser.add_argument(
        "--days", type=int, default=Config.LLM_TELEMETRY_RETENTION_DAYS,
        help=f"retain this many days (default: {Config.LLM_TELEMETRY_RETENTION_DAYS})",
    )
    parser.add_argument("--batch-size", type=int, default=10000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stats", action="store_true", help="print a summary and exit")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    session = get_session()
    try:
        if args.stats:
            show_stats(session)
            return 0

        cutoff_sql = text(
            "SELECT count(*) FROM llm_call_log WHERE ts < now() - make_interval(days => :days)"
        )
        cutoff = session.execute(cutoff_sql, {"days": args.days}).scalar() or 0

        if not cutoff:
            print(f"Nothing older than {args.days} days. llm_call_log is within retention.")
            show_stats(session)
            return 0

        print(f"Rows older than {args.days} days: {cutoff:,}")
        if args.dry_run:
            print("Dry run — nothing deleted.")
            return 0

        if not args.yes:
            reply = input(f"Delete {cutoff:,} rows? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                print("Aborted.")
                return 1

        # Delete in batches so we never hold one long transaction.
        deleted_total = 0
        while True:
            result = session.execute(
                text(
                    """
                    DELETE FROM llm_call_log
                    WHERE id IN (
                        SELECT id FROM llm_call_log
                        WHERE ts < now() - make_interval(days => :days)
                        LIMIT :batch
                    )
                    """
                ),
                {"days": args.days, "batch": args.batch_size},
            )
            session.commit()
            deleted = result.rowcount or 0
            deleted_total += deleted
            if deleted:
                print(f"  deleted {deleted_total:,}...", flush=True)
            if deleted < args.batch_size:
                break

        print(f"Deleted {deleted_total:,} rows.")
        show_stats(session)
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
