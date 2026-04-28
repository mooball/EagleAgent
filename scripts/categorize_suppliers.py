"""
categorize_suppliers.py

CLI tool to categorize suppliers from a JSON file using search-grounded Gemini.
Outputs results to a JSON file. Useful for R&D / batch testing.

For DB-integrated categorization, use: scripts/categorize_suppliers_job.py

Usage:
  uv run python -m scripts.categorize_suppliers
  uv run python -m scripts.categorize_suppliers --model gemini-3.1-pro-preview
  uv run python -m scripts.categorize_suppliers --dry-run
  uv run python -m scripts.categorize_suppliers --input data/top_50_suppliers.json --output data/supplier_categories.json
"""

import os
import json
import time
import argparse
from dotenv import load_dotenv
load_dotenv()

from google import genai

from includes.supplier_categorization import (
    build_prompt,
    categorize_supplier,
    load_taxonomy,
)


def main():
    parser = argparse.ArgumentParser(description="Categorize suppliers using search-grounded Gemini LLM.")
    parser.add_argument("--input", type=str, default="data/top_50_suppliers.json", help="Input JSON file of suppliers.")
    parser.add_argument("--output", type=str, default="data/supplier_categories.json", help="Output JSON file for results.")
    parser.add_argument("--model", type=str, default="gemini-3-flash-preview", help="Gemini model to use.")
    parser.add_argument("--dry-run", action="store_true", help="Show the prompt for the first supplier without calling the API.")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay in seconds between API calls (default: 2.0).")
    parser.add_argument("--start", type=int, default=0, help="Index to start from (for resuming interrupted runs).")
    args = parser.parse_args()

    # Load input
    with open(args.input, "r") as f:
        suppliers = json.load(f)
    print(f"Loaded {len(suppliers)} suppliers from {args.input}")

    taxonomy = load_taxonomy()

    if args.dry_run:
        supplier = suppliers[0]
        prompt = build_prompt(taxonomy, supplier)
        print(f"\n{'='*80}")
        print(f"DRY RUN — Prompt for: {supplier['name']}")
        print(f"{'='*80}")
        print(prompt)
        print(f"{'='*80}")
        print(f"Prompt length: {len(prompt)} chars")
        return

    client = genai.Client()

    # Load existing results if resuming
    results = []
    if args.start > 0 and os.path.exists(args.output):
        with open(args.output, "r") as f:
            results = json.load(f)
        print(f"Loaded {len(results)} existing results, resuming from index {args.start}")

    total = len(suppliers)
    for i, supplier in enumerate(suppliers[args.start:], start=args.start):
        print(f"\n[{i+1}/{total}] Categorizing: {supplier['name']} ({supplier.get('url', 'no URL')})...")

        try:
            result = categorize_supplier(client, args.model, taxonomy, supplier)
            results.append(result)
            print(f"  → {result['category']} (Tier {result['tier']}, Confidence: {result['confidence']})")
            print(f"    {result['reasoning'][:120]}")
        except Exception as e:
            print(f"  ✗ ERROR: {e}")
            results.append({
                "supplier_id": supplier["id"],
                "supplier_name": supplier["name"],
                "supplier_url": supplier.get("url"),
                "category": "ERROR",
                "tier": "?",
                "confidence": 0,
                "reasoning": str(e)[:500],
                "model": args.model,
            })

        # Save after each result (crash-safe)
        os.makedirs(os.path.dirname(args.output), exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)

        # Rate limiting
        if i < total - 1:
            time.sleep(args.delay)

    print(f"\n{'='*80}")
    print(f"Done! {len(results)} results saved to {args.output}")

    # Summary
    from collections import Counter
    cats = Counter(r["category"] for r in results)
    print(f"\nCategory distribution:")
    for cat, count in cats.most_common():
        print(f"  {cat}: {count}")
    avg_conf = sum(r.get("confidence", 0) for r in results) / max(len(results), 1)
    print(f"Average confidence: {avg_conf:.1f}")


if __name__ == "__main__":
    main()
