"""Populate supplier_match_keys for every supplier.

Run after deploying the supplier dedup schema (S0) and any time a supplier
write path bypasses rebuild_match_keys() (e.g. raw-SQL syncs). Batched,
re-runnable, and safe: it only touches the supplier_match_keys table.

Usage:
    uv run python -m scripts.backfill_supplier_match_keys [--dry-run] [--batch-size 500]
"""

import argparse
import logging

from includes.dashboard.database import get_session
from includes.dashboard.models import Supplier
from includes.dashboard.supplier_matching import rebuild_match_keys, supplier_match_keys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill supplier match keys.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be built without writing.")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="Suppliers per commit batch (default 500).")
    args = parser.parse_args()

    session = get_session()
    try:
        total = session.query(Supplier).count()
        logger.info(f"{total} suppliers to process (dry_run={args.dry_run})")

        processed = 0
        keys_built = 0
        empty = 0
        offset = 0

        while True:
            suppliers = (
                session.query(Supplier)
                .order_by(Supplier.name)
                .offset(offset)
                .limit(args.batch_size)
                .all()
            )
            if not suppliers:
                break

            for sup in suppliers:
                if not args.dry_run:
                    rebuild_match_keys(session, sup)
                keys_built += len(supplier_match_keys(sup))
                if not sup.name or not sup.name.strip():
                    empty += 1
                processed += 1

            if not args.dry_run:
                session.commit()
            logger.info(
                f"  processed {processed}/{total} suppliers, "
                f"{keys_built} keys so far"
            )
            offset += args.batch_size

        logger.info(
            f"Done: {processed} suppliers, {keys_built} match keys, "
            f"{empty} with no name."
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
