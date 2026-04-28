"""
Supplier categorization service.

Provides a reusable function to categorize one or more suppliers using
search-grounded Gemini LLM against the supply chain taxonomy.

Can be called from:
- The background job script (scripts/categorize_suppliers_job.py)
- The original CLI script (scripts/categorize_suppliers.py)
- Any future agent tool or API endpoint
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from google import genai
from google.genai import types

from config import config as app_config

logger = logging.getLogger(__name__)

TAXONOMY_PATH = Path(__file__).parent.parent / "config" / "supplier-categorization-taxonomy.md"

VALID_CATEGORIES = app_config.get_valid_categories()
VALID_TIERS = app_config.get_valid_tiers()

_taxonomy_cache: str | None = None


def load_taxonomy() -> str:
    """Load and cache the taxonomy markdown file."""
    global _taxonomy_cache
    if _taxonomy_cache is None:
        with open(TAXONOMY_PATH, "r") as f:
            _taxonomy_cache = f.read()
    return _taxonomy_cache


def _build_category_list() -> str:
    """Build the category list dynamically from config."""
    return "\n".join(f"   - {cat}" for cat in VALID_CATEGORIES)


def _build_tier_list() -> str:
    """Build the tier list dynamically from config."""
    return ", ".join(VALID_TIERS)


def build_prompt(taxonomy: str, supplier: dict) -> str:
    """Build the categorization prompt for a single supplier."""
    name = supplier.get("name", "Unknown")
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
{_build_category_list()}
5. Assign the tier letter ({_build_tier_list()}).
6. Provide a confidence score from 1-5 per the scoring rubric.
7. Provide brief reasoning (2-3 sentences) explaining your classification.

## Required Output Format

Respond with ONLY a JSON object (no markdown fences, no extra text):
{{
  "category": "<one of the categories above>",
  "tier": "<{_build_tier_list()}>",
  "confidence": <1-5>,
  "reasoning": "<2-3 sentence explanation>"
}}"""


def parse_response(text: str) -> dict:
    """Parse the LLM response, handling markdown fences if present."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    return json.loads(text)


def categorize_supplier(
    client: genai.Client,
    model: str,
    taxonomy: str,
    supplier: dict,
) -> dict:
    """Categorize a single supplier using search-grounded Gemini.

    Args:
        client: google.genai.Client instance
        model: Gemini model name
        taxonomy: Full taxonomy markdown text
        supplier: Dict with at least {name}, optionally {url, city, country, purchase_count}

    Returns:
        Dict with {category, tier, confidence, reasoning} plus supplier metadata.
    """
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
        result["reasoning"] = (
            f"WARNING: Invalid category '{result.get('category')}'. "
            + result.get("reasoning", "")
        )

    # Attach supplier metadata
    result["supplier_id"] = supplier.get("id")
    result["supplier_name"] = supplier.get("name")
    result["supplier_url"] = supplier.get("url")
    result["model"] = model

    return result


def save_categorization_to_db(supplier_id: str, result: dict) -> None:
    """Write categorization result into the supplier's supply_chain_position JSONB.

    Sets modified_by = "ai:categorizer" and modified_at = now.
    """
    from includes.dashboard.database import get_session
    from includes.dashboard.models import Supplier

    session = get_session()
    try:
        supplier = session.query(Supplier).filter(Supplier.id == supplier_id).first()
        if not supplier:
            logger.warning(f"Supplier {supplier_id} not found, skipping DB write")
            return

        # Merge with existing data (preserve any manual overrides)
        existing = dict(supplier.supply_chain_position or {})
        existing.update({
            "category": result.get("category"),
            "tier": result.get("tier"),
            "confidence": result.get("confidence"),
            "reasoning": result.get("reasoning"),
        })
        supplier.supply_chain_position = existing
        supplier.modified_at = datetime.now(timezone.utc)
        supplier.modified_by = "ai:categorizer"
        session.commit()
        logger.info(f"Saved categorization for {supplier_id}: {result.get('category')}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
