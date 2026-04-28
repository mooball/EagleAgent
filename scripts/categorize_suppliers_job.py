"""
categorize_suppliers_job.py

Background job that categorizes suppliers by reading from the database,
running search-grounded Gemini categorization, and writing results back.

By default, only processes suppliers with no existing categorization.
Use --force to re-categorize all suppliers.
Use --limit N to cap the number of suppliers processed.

Usage:
  uv run python -m scripts.categorize_suppliers_job
  uv run python -m scripts.categorize_suppliers_job --force
  uv run python -m scripts.categorize_suppliers_job --limit 50
  uv run python -m scripts.categorize_suppliers_job --force --limit 100
"""

import argparse
import time
from collections import Counter

from dotenv import load_dotenv
load_dotenv()

from google import genai
from sqlalchemy import func

from config import config as app_config
from includes.dashboard.database import get_session
from includes.dashboard.models import Supplier, ProductSupplier
from includes.supplier_categorization import (
    categorize_supplier,
    load_taxonomy,
    save_categorization_to_db,
)


def get_suppliers_to_categorize(force: bool = False, limit: int | None = None) -> list[dict]:
    """Fetch suppliers from DB that need categorization.

    Args:
        force: If True, return all suppliers. If False, only those without a category.
        limit: Max number of suppliers to return. None = no limit.

    Returns:
        List of supplier dicts sorted by purchase count descending.
    """
    session = get_session()
    try:
        query = session.query(
            Supplier,
            func.count(ProductSupplier.id).label("purchase_count"),
        ).outerjoin(
            ProductSupplier, ProductSupplier.supplier_id == Supplier.id
        ).group_by(Supplier.id)

        if not force:
            # Only suppliers without a categorization
            query = query.filter(
                Supplier.supply_chain_position.is_(None)
                | ~Supplier.supply_chain_position.has_key("category")
            )

        query = query.order_by(func.count(ProductSupplier.id).desc())

        if limit:
            query = query.limit(limit)

        rows = query.all()

        suppliers = []
        for s, pc in rows:
            suppliers.append({
                "id": str(s.id),
                "name": s.name,
                "url": s.url,
                "city": s.city,
                "country": s.country,
                "purchase_count": pc,
            })
        return suppliers
    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser(
        description="Categorize suppliers from the database using search-grounded Gemini."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-categorize all suppliers, not just empty ones."
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max number of suppliers to process."
    )
    parser.add_argument(
        "--model", type=str, default=app_config.DEFAULT_MODEL,
        help=f"Gemini model to use (default: {app_config.DEFAULT_MODEL})."
    )
    parser.add_argument(
        "--delay", type=float, default=2.0,
        help="Delay in seconds between API calls (default: 2.0)."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be categorized without calling the API."
    )
    args = parser.parse_args()

    # Fetch suppliers from DB
    suppliers = get_suppliers_to_categorize(force=args.force, limit=args.limit)
    total = len(suppliers)

    mode = "all" if args.force else "uncategorized only"
    print(f"Found {total} suppliers to categorize ({mode})")

    if total == 0:
        print("Nothing to do.")
        return

    if args.dry_run:
        for i, s in enumerate(suppliers[:10], 1):
            print(f"  {i}. {s['name']} ({s.get('url', 'no URL')}) — {s['purchase_count']} purchases")
        if total > 10:
            print(f"  ... and {total - 10} more")
        return

    taxonomy = load_taxonomy()
    client = genai.Client()
    results = Counter()

    for i, supplier in enumerate(suppliers, 1):
        print(f"\n[{i}/{total}] {supplier['name']} ({supplier.get('url', 'no URL')})...")

        try:
            result = categorize_supplier(client, args.model, taxonomy, supplier)
            category = result.get("category", "?")
            tier = result.get("tier", "?")
            confidence = result.get("confidence", 0)
            print(f"  → Tier {tier} — {category} (confidence: {confidence}/5)")
            print(f"    {result.get('reasoning', '')[:120]}")

            # Write to DB
            save_categorization_to_db(supplier["id"], result)
            results[category] += 1

        except Exception as e:
            print(f"  ✗ ERROR: {e}")
            results["ERROR"] += 1

        # Rate limiting
        if i < total:
            time.sleep(args.delay)

    # Summary
    print(f"\n{'='*60}")
    print(f"Done! Processed {total} suppliers.")
    print(f"\nCategory distribution:")
    for cat, count in results.most_common():
        print(f"  {cat}: {count}")


if __name__ == "__main__":
    main()
