"""Prune old LangGraph checkpoints, keeping only the N most recent per thread.

Checkpoint IDs are UUIDv7 (time-ordered), so we sort by checkpoint_id DESC
and keep the first N per (thread_id, checkpoint_ns) pair.

Uses the standard DATABASE_URL from the environment — points to local in dev,
production when deployed to Railway.

Usage:
    uv run python -m scripts.prune_checkpoints              # keep 5 (default)
    uv run python -m scripts.prune_checkpoints --keep 3     # keep 3
    uv run python -m scripts.prune_checkpoints --dry-run    # preview only
"""

import argparse
import logging
import sys

from includes.dashboard.database import get_session
from sqlalchemy import text

logger = logging.getLogger(__name__)


def prune(session, keep: int = 5, dry_run: bool = False):
    """Delete all but the `keep` most recent checkpoints per thread."""
    # Find all (thread_id, checkpoint_ns) pairs with more than `keep` checkpoints
    over_limit = session.execute(text("""
        SELECT thread_id, checkpoint_ns, count(*) AS cnt
        FROM checkpoints
        GROUP BY 1, 2
        HAVING count(*) > :keep
    """), {"keep": keep}).fetchall()

    if not over_limit:
        logger.info("No threads exceed the checkpoint limit. Nothing to do.")
        return

    logger.info(f"Found {len(over_limit)} thread(s) with > {keep} checkpoints.")

    total_deleted_checkpoints = 0
    total_deleted_blobs = 0
    total_deleted_writes = 0

    for thread_id, checkpoint_ns, cnt in over_limit:
        # Get the checkpoint_ids to KEEP (most recent `keep` by UUIDv7 order)
        keep_ids = session.execute(text("""
            SELECT checkpoint_id
            FROM checkpoints
            WHERE thread_id = :tid AND checkpoint_ns = :ns
            ORDER BY checkpoint_id DESC
            LIMIT :keep
        """), {"tid": thread_id, "ns": checkpoint_ns, "keep": keep}).fetchall()
        keep_id_list = [r[0] for r in keep_ids]

        if dry_run:
            logger.info(f"  [dry-run] {thread_id[:12]}... ns={checkpoint_ns!r:.20s}: "
                        f"would keep {len(keep_id_list)}, delete {cnt - len(keep_id_list)} checkpoints")
            continue

        # Delete checkpoint_writes for old checkpoints
        result_w = session.execute(text("""
            DELETE FROM checkpoint_writes
            WHERE thread_id = :tid AND checkpoint_ns = :ns
              AND checkpoint_id != ALL(:kids)
        """), {"tid": thread_id, "ns": checkpoint_ns, "kids": keep_id_list})
        deleted_w = result_w.rowcount

        # Delete checkpoint_blobs not referenced by any kept checkpoint.
        # Blobs are keyed by (thread_id, checkpoint_ns, channel, version);
        # kept checkpoints reference them via checkpoint->'channel_versions' JSONB.
        result_b = session.execute(text("""
            DELETE FROM checkpoint_blobs b
            WHERE b.thread_id = :tid AND b.checkpoint_ns = :ns
              AND NOT EXISTS (
                SELECT 1 FROM checkpoints c,
                jsonb_each_text(c.checkpoint -> 'channel_versions') cv
                WHERE c.thread_id = b.thread_id
                  AND c.checkpoint_ns = b.checkpoint_ns
                  AND c.checkpoint_id = ANY(:kids)
                  AND cv.key = b.channel
                  AND cv.value = b.version
              )
        """), {"tid": thread_id, "ns": checkpoint_ns, "kids": keep_id_list})
        deleted_b = result_b.rowcount

        # Delete old checkpoints
        result_c = session.execute(text("""
            DELETE FROM checkpoints
            WHERE thread_id = :tid AND checkpoint_ns = :ns
              AND checkpoint_id != ALL(:kids)
        """), {"tid": thread_id, "ns": checkpoint_ns, "kids": keep_id_list})
        deleted_c = result_c.rowcount

        total_deleted_checkpoints += deleted_c
        total_deleted_blobs += deleted_b
        total_deleted_writes += deleted_w

        logger.info(f"  {thread_id[:12]}... ns={checkpoint_ns!r:.20s}: "
                    f"deleted {deleted_c} checkpoints, {deleted_b} blobs, {deleted_w} writes")

    session.commit()

    logger.info(f"Total: deleted {total_deleted_checkpoints} checkpoints, "
                f"{total_deleted_blobs} blobs, {total_deleted_writes} writes")


def main():
    parser = argparse.ArgumentParser(description="Prune old LangGraph checkpoints")
    parser.add_argument("--keep", type=int, default=5, help="Checkpoints to keep per thread (default: 5)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no deletes")
    args = parser.parse_args()

    session = get_session()
    try:
        prune(session, keep=args.keep, dry_run=args.dry_run)
    finally:
        session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
