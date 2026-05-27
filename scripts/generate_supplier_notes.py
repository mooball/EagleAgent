"""
generate_supplier_notes.py

Background job that researches suppliers via search-grounded Gemini and generates
structured notes optimized for vector-search retrieval.

Notes are designed so that embedding them enables semantic matching of the form:
  "find me a supplier who can supply [product/part/brand]"

Also captures alternative company names and domain variants for improved
supplier deduplication.

By default, only processes suppliers with no existing notes.
Use --force to regenerate notes for all suppliers.
Use --limit N to cap the number of suppliers processed per run.

Usage:
  uv run python -m scripts.generate_supplier_notes
  uv run python -m scripts.generate_supplier_notes --limit 50
  uv run python -m scripts.generate_supplier_notes --force --limit 100
  uv run python -m scripts.generate_supplier_notes --dry-run
"""

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

from google import genai
from google.genai import types
from sqlalchemy import func

from config import config as app_config
from includes.dashboard.database import get_session
from includes.dashboard.models import Supplier, Transaction


NOTES_PROMPT_TEMPLATE = """You are a procurement research assistant. Your task is to research a supplier \
and produce a structured description that will be used for semantic search.

The description must help answer queries like:
- "Find a supplier for [product name / part number / brand]"
- "Who supplies [category of goods] in [country]?"

## Supplier to Research

- **Name:** {name}
- **Website:** {url}
- **Location:** {city}, {country}
- **Purchase history with us:** {purchase_count} transactions

## Instructions

1. Search the web for this supplier — visit their website if available.
2. Identify what they actually sell or manufacture, their specialisations, key brands they carry or represent, and the industries they serve.
3. Note any alternative company names, trading names, or abbreviations they use.
4. Note any alternative website domains (e.g. .com.au, .au, .com, .net.au variants).

## Required Output Format

Respond with ONLY a JSON object (no markdown fences, no extra text):
{{
  "summary": "<1-2 sentences: what the company does and their core specialisation>",
  "products": ["<product category 1>", "<product category 2>", ...],
  "services": ["<service 1>", "<service 2>", ...],
  "industries": ["<industry 1>", "<industry 2>", ...],
  "brands_carried": ["<brand 1>", "<brand 2>", ...],
  "alt_names": ["<alternative name 1>", ...],
  "alt_domains": ["<alternative domain 1>", ...]
}}

Rules:
- products: List specific product categories they sell (e.g. "Hydraulic Cylinders", "Diesel Engine Parts", "Conveyor Belts"). Be specific enough for search matching. 5-20 items.
- services: List services they provide (e.g. "Engine Rebuilding", "On-site Installation"). Empty array if none.
- industries: List industries they serve (e.g. "Mining", "Construction", "Agriculture", "Transport"). 1-5 items.
- brands_carried: Major brands they distribute/represent. Empty array if not applicable (e.g. if they are an OEM).
- alt_names: Other names this company is known by (abbreviations, former names, trading names). Empty array if none found.
- alt_domains: Other website domains they use (without https://). Empty array if none found.
- summary: Focus on WHAT they supply and to whom. Do not repeat their location.
- If you cannot find any information about the supplier, set summary to "No information available" and leave other arrays empty."""


def get_suppliers_to_process(force: bool = False, limit: int | None = None) -> list[dict]:
    """Fetch suppliers from DB that need notes generated."""
    session = get_session()
    try:
        query = session.query(
            Supplier,
            func.count(Transaction.id).label("purchase_count"),
        ).outerjoin(
            Transaction, Transaction.supplier_id == Supplier.id
        ).group_by(Supplier.id)

        if not force:
            # Pick up suppliers with no notes_updated_at — either missing notes
            # or notes flagged as bad quality (notes_updated_at left NULL)
            query = query.filter(Supplier.notes_updated_at.is_(None))

        # Prioritise suppliers with more purchases (more likely to be important)
        query = query.order_by(func.count(Transaction.id).desc())

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


def build_prompt(supplier: dict) -> str:
    """Build the research prompt for a single supplier."""
    return NOTES_PROMPT_TEMPLATE.format(
        name=supplier.get("name", "Unknown"),
        url=supplier.get("url") or "No URL available",
        city=supplier.get("city") or "Unknown",
        country=supplier.get("country") or "Unknown",
        purchase_count=supplier.get("purchase_count", 0),
    )


def parse_response(text: str) -> dict:
    """Parse the LLM response, handling markdown fences if present."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    return json.loads(text)


def format_notes(result: dict) -> str:
    """Convert structured JSON result into a notes string optimized for embedding.

    Format:
        <summary sentence(s)>
        Products: <comma-separated list>
        Services: <comma-separated list>
        Industries: <comma-separated list>
        Brands: <comma-separated list>
    """
    parts = []

    summary = result.get("summary", "").strip()
    if summary and summary != "No information available":
        parts.append(summary)

    products = result.get("products", [])
    if products:
        parts.append(f"Products: {', '.join(products)}.")

    services = result.get("services", [])
    if services:
        parts.append(f"Services: {', '.join(services)}.")

    industries = result.get("industries", [])
    if industries:
        parts.append(f"Industries: {', '.join(industries)}.")

    brands = result.get("brands_carried", [])
    if brands:
        parts.append(f"Brands: {', '.join(brands)}.")

    return " ".join(parts) if parts else ""


def save_to_db(supplier_id: str, result: dict, notes_text: str) -> None:
    """Write notes and alt fields to the database."""
    session = get_session()
    try:
        supplier = session.query(Supplier).filter(Supplier.id == supplier_id).first()
        if not supplier:
            return

        supplier.notes = notes_text
        # Clear embedding so it gets regenerated on next embedding run
        supplier.embedding = None

        alt_names = result.get("alt_names", [])
        if alt_names:
            supplier.alt_names = alt_names

        alt_domains = result.get("alt_domains", [])
        if alt_domains:
            supplier.alt_domains = alt_domains

        supplier.modified_at = datetime.now(timezone.utc)
        supplier.modified_by = "ai:notes_generator"
        supplier.notes_updated_at = datetime.now(timezone.utc)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def research_supplier(client: genai.Client, model: str, supplier: dict) -> dict:
    """Research a single supplier using search-grounded Gemini."""
    prompt = build_prompt(supplier)

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
        return parse_response(raw_text)
    except (json.JSONDecodeError, KeyError) as e:
        return {
            "summary": f"PARSE_ERROR: {e}",
            "products": [],
            "services": [],
            "industries": [],
            "brands_carried": [],
            "alt_names": [],
            "alt_domains": [],
            "_raw": raw_text[:500],
        }


def main():
    parser = argparse.ArgumentParser(
        description="Generate supplier notes via search-grounded Gemini research."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Regenerate notes for all suppliers, not just those missing notes."
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
        help="Show what would be processed without calling the API."
    )
    args = parser.parse_args()

    suppliers = get_suppliers_to_process(force=args.force, limit=args.limit)
    total = len(suppliers)

    mode = "all" if args.force else "missing notes only"
    print(f"Found {total} suppliers to research ({mode})")

    if total == 0:
        print("Nothing to do.")
        return

    if args.dry_run:
        for i, s in enumerate(suppliers[:10], 1):
            print(f"  {i}. {s['name']} ({s.get('url', 'no URL')}) — {s['purchase_count']} purchases")
        if total > 10:
            print(f"  ... and {total - 10} more")
        return

    client = genai.Client()
    results = Counter()

    for i, supplier in enumerate(suppliers, 1):
        print(f"\n[{i}/{total}] {supplier['name']} ({supplier.get('url', 'no URL')})...")

        try:
            result = research_supplier(client, args.model, supplier)
            summary = result.get("summary", "")

            if "PARSE_ERROR" in summary:
                print(f"  ✗ Parse error: {summary[:100]}")
                results["ERROR"] += 1
            elif summary == "No information available":
                print(f"  ⚠ No info found — skipping DB write")
                results["NO_INFO"] += 1
            else:
                notes_text = format_notes(result)
                products_count = len(result.get("products", []))
                alt_count = len(result.get("alt_names", []))

                print(f"  → {summary[:100]}")
                print(f"    {products_count} products, {len(result.get('services', []))} services, {alt_count} alt names")

                save_to_db(supplier["id"], result, notes_text)
                results["OK"] += 1

        except Exception as e:
            print(f"  ✗ ERROR: {e}")
            results["ERROR"] += 1

        # Rate limiting
        if i < total:
            time.sleep(args.delay)

    # Summary
    print(f"\n{'='*60}")
    print(f"Done! Processed {total} suppliers.")
    print(f"\nResults:")
    for status, count in results.most_common():
        print(f"  {status}: {count}")


if __name__ == "__main__":
    main()
