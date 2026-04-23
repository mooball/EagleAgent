"""
categorize_suppliers.py

Uses a search-grounded Gemini LLM to categorize suppliers according to
the supply chain taxonomy defined in docs/supplier-categorization-taxonomy.md.

Reads a JSON list of suppliers (from extract_top_suppliers.py) and outputs
categorization results.

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
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

from google import genai
from google.genai import types


TAXONOMY_PATH = Path(__file__).parent.parent / "docs" / "supplier-categorization-taxonomy.md"

VALID_CATEGORIES = [
    "OEM",
    "Aftermarket Manufacturer",
    "Trade Wholesaler",
    "Authorized Dealer",
    "Retail / Trade Outlet",
    "Online Distributor",
    "Service Exchange (SX) Provider",
    "Sourcing Broker",
]

VALID_TIERS = ["A", "B", "C", "D"]


def load_taxonomy() -> str:
    """Load the taxonomy markdown file."""
    with open(TAXONOMY_PATH, "r") as f:
        return f.read()


def build_prompt(taxonomy: str, supplier: dict) -> str:
    """Build the categorization prompt for a single supplier."""
    name = supplier["name"]
    url = supplier.get("url") or "No URL available"
    city = supplier.get("city") or "Unknown"
    country = supplier.get("country") or "Unknown"
    purchase_count = supplier.get("purchase_count", 0)

    return f"""You are an industrial procurement analyst. Your task is to categorize a supplier
into one of the roles defined in the taxonomy below.

## Taxonomy

{taxonomy}

## Supplier to Categorize

- **Name:** {name}
- **Website:** {url}
- **Location:** {city}, {country}
- **Purchase history:** {purchase_count} transactions in our records

## Instructions

1. Search the web for this supplier's website and any available information about them.
2. Analyze their website, products, pricing model, and business model.
3. Apply the categorization decision logic from the taxonomy step by step.
4. Assign exactly ONE category from this list:
   - OEM
   - Aftermarket Manufacturer
   - Trade Wholesaler
   - Authorized Dealer
   - Retail / Trade Outlet
   - Online Distributor
   - Service Exchange (SX) Provider
   - Sourcing Broker
5. Assign the tier letter (A, B, C, or D).
6. Provide a confidence score from 1-5 per the scoring rubric.
7. Provide brief reasoning (2-3 sentences) explaining your classification.

## Required Output Format

Respond with ONLY a JSON object (no markdown fences, no extra text):
{{
  "category": "<one of the 8 categories above>",
  "tier": "<A, B, C, or D>",
  "confidence": <1-5>,
  "reasoning": "<2-3 sentence explanation>"
}}"""


def parse_response(text: str) -> dict:
    """Parse the LLM response, handling markdown fences if present."""
    text = text.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (fences)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    return json.loads(text)


def categorize_supplier(client: genai.Client, model: str, taxonomy: str, supplier: dict) -> dict:
    """Send a categorization request to Gemini with Google Search grounding."""
    prompt = build_prompt(taxonomy, supplier)

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.2,
        ),
    )

    raw_text = response.text
    try:
        result = parse_response(raw_text)
    except (json.JSONDecodeError, KeyError) as e:
        result = {
            "category": "PARSE_ERROR",
            "tier": "?",
            "confidence": 0,
            "reasoning": f"Failed to parse LLM response: {e}. Raw: {raw_text[:500]}",
        }

    # Validate category
    if result.get("category") not in VALID_CATEGORIES and result.get("category") != "PARSE_ERROR":
        result["reasoning"] = f"WARNING: Invalid category '{result.get('category')}'. " + result.get("reasoning", "")

    # Attach supplier metadata
    result["supplier_id"] = supplier["id"]
    result["supplier_name"] = supplier["name"]
    result["supplier_url"] = supplier.get("url")
    result["model"] = model

    return result


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
