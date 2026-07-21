"""
consolidate_categories.py — Phase 2.5 of Product Category Discovery

Loads the 100 labelled clusters from Phase 2, sends the full category list to
Gemini for two passes:

  Pass A — Merge near-duplicate top-level category names
           (many were named in isolation and are semantically identical)

  Pass B — Group the consolidated categories into 10-20 super-categories
           for the final 2-level hierarchy

Usage:
  uv run python -m scripts.consolidate_categories
  uv run python -m scripts.consolidate_categories --dry-run
"""

import json
import time
import argparse
from pathlib import Path

from google import genai
from google.genai import types

from config.settings import Config

# ---------------------------------------------------------------------------
# Pass A: merge near-duplicate top-level category names
# ---------------------------------------------------------------------------

PASS_A_PROMPT = """You are a procurement data analyst for a mining and building
supply company. You have 100 product clusters, each with a top-level category
and subcategory assigned by a previous LLM pass — **but each was named in
isolation, so many top-level names are near-duplicates.**

## Your Task

Review the 100 clusters below. Identify top-level category names that refer to
**the same type of products** and should be merged into a single name.

Rules:
- The subcategories (the K-Means clusters) stay distinct — do NOT merge
  subcategories, only unify their top-level parent name.
- If two top-level names are clearly the same thing, pick the best/most
  industry-standard one and assign both clusters to it.
- If they overlap but are genuinely different (e.g. "Engine Components" vs
  "Engine Cooling Systems"), keep them separate.
- The output should map every cluster to its **final, deduplicated top-level
  category name**.

## Cluster List

Cluster ID | Current Category | Subcategory | Product Count | Top Sample
--- | --- | --- | --- | ---
{cluster_table}

## Required Output

Respond with ONLY a JSON object (no markdown fences):

{{
  "merged_categories": [
    {{
      "final_name": "Fasteners & Hardware",
      "original_names": ["Fasteners & Hardware", "Fasteners & Mechanical Hardware"],
      "cluster_ids": [2, 5, 20, 39, 41, 43, 88],
      "rationale": "All refer to bolts, screws, washers, nuts and general fasteners"
    }},
    ...
  ],
  "unmerged": [
    {{"cluster_id": 0, "category": "Welding & Industrial Equipment"}},
    ...
  ]
}}

Include ALL 100 clusters — either in a merged group or as unmerged singletons."""

# ---------------------------------------------------------------------------
# Pass B: group into super-categories
# ---------------------------------------------------------------------------

PASS_B_PROMPT = """You are a procurement data analyst for a mining and building
supply company. You have a set of product categories that need to be organised
into a **2-level hierarchy**.

## Current State

After deduplication, we have these top-level categories (each containing
multiple subcategories / product clusters):

{category_list}

## Your Task

Group these categories into **10-20 high-level super-categories** suitable for
a procurement business in mining, construction, and heavy industry.

Rules:
- Each category must belong to exactly one super-category.
- Super-categories should be broad but meaningful (e.g. "Powertrain & Engine",
  "Fluid Power & Hydraulics", "Safety & PPE", "Electrical & Automation").
- Don't create a catch-all "Miscellaneous" — distribute everything.
- Name super-categories with industry-standard terminology.

## Required Output

Respond with ONLY a JSON object:

{{
  "super_categories": [
    {{
      "name": "Powertrain & Engine Components",
      "categories": ["Engine Components", "Engine & Cooling Systems"],
      "rationale": "Both relate to internal engine parts, pistons, bearings, cooling"
    }},
    ...
  ]
}}

Every category must appear in exactly one super-category."""


def build_cluster_table(clusters: list[dict]) -> str:
    """Build a markdown table of all clusters for the LLM prompt."""
    lines = []
    for c in clusters:
        cluster_id = c["cluster_id"]
        category = c.get("category", "")
        subcategory = c.get("subcategory", "")
        size = c.get("size", 0)
        top_sample = ""
        samples = c.get("samples", [])
        if samples:
            s = samples[0]
            desc = (s.get("description") or s.get("part_number") or "")[:80]
            top_sample = desc.replace("\n", " ").replace("|", "/")
        lines.append(
            f"{cluster_id} | {category} | {subcategory} | {size:,} | {top_sample}"
        )
    return "\n".join(lines)


def build_category_list(merged: list[dict], unmerged: list[dict]) -> str:
    """Build a list of consolidated categories for Pass B."""
    lines = []
    for group in merged:
        name = group["final_name"]
        subs = group.get("subcategories", [])
        total = group.get("total_products", 0)
        lines.append(f"- **{name}** ({total:,} products)")
        # Show up to 3 subcategories
        shown = 0
        for sub in subs:
            if shown >= 3:
                break
            if sub:
                lines.append(f"  - {sub}")
                shown += 1
        if len(subs) > 3:
            lines.append(f"  - ... and {len(subs) - 3} more subcategories")
    for entry in unmerged:
        lines.append(
            f"- **{entry['category']}** ({entry.get('size', 0):,} products) "
            f"— {entry.get('subcategory', '')}"
        )
    return "\n".join(lines)


def call_llm(
    client: genai.Client,
    model: str,
    prompt: str,
    retries: int = 3,
) -> dict:
    """Call Gemini and parse JSON response."""
    last_error = None
    for attempt in range(retries):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.1),
            )
            raw = response.text.strip()
            if raw.startswith("```"):
                lines = raw.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                raw = "\n".join(lines).strip()
            return json.loads(raw)
        except (json.JSONDecodeError, KeyError) as e:
            last_error = f"Parse error: {e}"
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
        except Exception as e:
            last_error = str(e)
            if attempt < retries - 1:
                wait = 5 * (attempt + 1)
                print(f"  Retry {attempt + 1}/{retries} in {wait}s: {e}")
                time.sleep(wait)

    raise RuntimeError(f"LLM call failed after {retries} attempts: {last_error}")


def main():
    parser = argparse.ArgumentParser(
        description="Consolidate product categories (Phase 2.5)."
    )
    parser.add_argument(
        "--input",
        type=str,
        default=None,
        help="Path to Phase 2 draft JSON (default: data/product_categories_draft.json).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output path (default: data/product_categories_final.json).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print prompt without calling LLM.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help=f"Gemini model (default: {Config.DEFAULT_MODEL}).",
    )
    args = parser.parse_args()

    data_dir = Path(Config.DATA_DIR)
    input_path = args.input or str(data_dir / "product_categories_draft.json")
    output_path = args.output or str(data_dir / "product_categories_final.json")

    print(f"Loading clusters from: {input_path}")
    with open(input_path, "r") as f:
        clusters = json.load(f)
    print(f"Loaded {len(clusters)} clusters.")

    # Sort by category for readability
    clusters = sorted(clusters, key=lambda c: (c.get("category", ""), c.get("cluster_id", 0)))

    # ---- Pass A: merge near-duplicate top-level names ----
    cluster_table = build_cluster_table(clusters)
    prompt_a = PASS_A_PROMPT.format(cluster_table=cluster_table)

    if args.dry_run:
        print("\n" + "=" * 60)
        print("PASS A PROMPT (first 3000 chars):")
        print("=" * 60)
        print(prompt_a[:3000])
        print(f"\n... ({len(prompt_a):,} total chars)")
        return

    model = args.model or Config.DEFAULT_MODEL
    print(f"Using model: {model}")

    print("\n--- Pass A: Merging near-duplicate top-level categories ---")
    client = genai.Client()
    result_a = call_llm(client, model, prompt_a)

    merged = result_a.get("merged_categories", [])
    unmerged = result_a.get("unmerged", [])

    # Build lookup: cluster_id → final category name
    cluster_to_category = {}
    for group in merged:
        final_name = group["final_name"]
        for cid in group["cluster_ids"]:
            cluster_to_category[cid] = final_name
    for entry in unmerged:
        cluster_to_category[entry["cluster_id"]] = entry["category"]

    # Enrich merged groups with subcategory and product count info
    cluster_map = {c["cluster_id"]: c for c in clusters}
    for group in merged:
        subs = []
        total = 0
        for cid in group["cluster_ids"]:
            c = cluster_map.get(cid)
            if c:
                subs.append(c.get("subcategory", ""))
                total += c.get("size", 0)
        group["subcategories"] = subs
        group["total_products"] = total

    deduped_count = len(set(cluster_to_category.values()))
    print(f"Pass A complete: {deduped_count} unique top-level categories "
          f"(down from {len(set(c['category'] for c in clusters))})")

    # Save intermediate result
    with open(output_path.replace(".json", "_passA.json"), "w") as f:
        json.dump({"merged_categories": merged, "unmerged": unmerged}, f, indent=2)

    # ---- Pass B: group into super-categories ----
    print("\n--- Pass B: Grouping into super-categories ---")
    category_list = build_category_list(merged, unmerged)
    prompt_b = PASS_B_PROMPT.format(category_list=category_list)

    result_b = call_llm(client, model, prompt_b)
    super_categories = result_b.get("super_categories", [])

    print(f"Pass B complete: {len(super_categories)} super-categories")

    # ---- Build final output ----
    # Combine into final structure
    final = {
        "super_categories": super_categories,
        "category_mapping": {},  # cluster_id → {super_category, category, subcategory}
    }

    # Build reverse lookup: category name → super-category name
    cat_to_super = {}
    for sc in super_categories:
        for cat_name in sc.get("categories", []):
            cat_to_super[cat_name] = sc["name"]

    for c in clusters:
        cid = c["cluster_id"]
        final_cat = cluster_to_category.get(cid, c.get("category", "Unknown"))
        super_cat = cat_to_super.get(final_cat, "Uncategorised")
        final["category_mapping"][str(cid)] = {
            "super_category": super_cat,
            "category": final_cat,
            "subcategory": c.get("subcategory", ""),
            "cluster_size": c.get("size", 0),
        }

    with open(output_path, "w") as f:
        json.dump(final, f, indent=2, ensure_ascii=False)

    print(f"\nDone! Output: {output_path}")
    print(f"  Super-categories: {len(super_categories)}")
    print(f"  Consolidated top-level categories: {deduped_count}")
    print(f"  Subcategories (clusters): {len(clusters)}")

    # Print super-category summary
    print("\nSuper-category summary:")
    for sc in super_categories:
        cat_count = len(sc.get("categories", []))
        print(f"  {sc['name']} ({cat_count} categories)")


if __name__ == "__main__":
    main()
