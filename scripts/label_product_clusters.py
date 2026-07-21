"""
label_product_clusters.py — Phase 2 of Product Category Discovery

Loads the clustered product samples from Phase 1, sends each cluster's samples
to Gemini for category labelling, and writes the proposed categories to
data/product_categories_draft.json.

Usage:
  uv run python -m scripts.label_product_clusters
  uv run python -m scripts.label_product_clusters --input data/product_clusters.json
  uv run python -m scripts.label_product_clusters --dry-run  # label first 3 only
"""

import json
import time
import argparse
import sys
from pathlib import Path

from google import genai
from google.genai import types

from config.settings import Config

# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

SYSTEM_CONTEXT = """You are categorising products for a procurement company \
in the mining and building industries. Products range from spare parts, full \
machinery, tools, accessories, safety gear, motors, electrical components, \
hydraulics, fasteners, PPE, welding equipment, hoses, bearings, seals, and more.

We need a well-structured 2-level category hierarchy. Categories should be:
- Broad enough to contain 50–5,000+ products each
- Specific enough to be useful for procurement analysis
- Industry-standard naming where possible
- Relatively flat (aim for ~50–100 top-level categories)

The hierarchy has two levels:
  Category     — the broader group (e.g. "Electrical Components")
  Subcategory  — the specific type (e.g. "Circuit Breakers & Switchgear")"""


def build_prompt(samples: list[dict]) -> str:
    """Build the labelling prompt for one cluster's samples."""
    lines = []
    for i, s in enumerate(samples, 1):
        pn = s.get("part_number", "") or ""
        desc = s.get("description", "") or ""
        brand = s.get("brand", "") or ""
        # Truncate very long descriptions
        if len(desc) > 200:
            desc = desc[:197] + "..."
        lines.append(f"{i}. [{brand}] {pn}: {desc}")

    sample_text = "\n".join(lines)

    return f"""{SYSTEM_CONTEXT}

## Sample Products from One Cluster

These products were grouped together by a semantic clustering algorithm.
They should share a common theme — your job is to identify what that theme is.

{sample_text}

## Instructions

1. Review all {len(samples)} sample products above.
2. Identify the common theme (what kind of products are these?).
3. Propose a **Category** (top-level) and **Subcategory** name.
4. If the cluster seems incoherent or mixed, use your best judgement for the
   dominant theme and note this in the rationale.
5. Provide a brief 1-sentence rationale.

## Required Output Format

Respond with ONLY a JSON object (no markdown fences, no extra text):
{{"category": "Category Name", "subcategory": "Subcategory Name", "rationale": "1-sentence explanation"}}"""


def parse_response(text: str) -> dict:
    """Parse the LLM response, stripping markdown fences if present."""
    text = text.strip()
    # Strip ```json ... ``` fences
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    return json.loads(text)


def label_cluster(
    client: genai.Client,
    model: str,
    cluster: dict,
    retries: int = 3,
) -> dict:
    """Label a single cluster via Gemini.

    Returns {category, subcategory, rationale} plus cluster metadata.
    """
    samples = cluster.get("samples", [])
    if not samples:
        return {
            "category": "EMPTY_CLUSTER",
            "subcategory": "",
            "rationale": "No products in this cluster.",
        }

    prompt = build_prompt(samples)

    last_error = None
    for attempt in range(retries):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.2),
            )
            raw = response.text
            result = parse_response(raw)
            # Validate required keys
            for key in ("category", "subcategory", "rationale"):
                if key not in result:
                    result[key] = ""
            return result
        except (json.JSONDecodeError, KeyError) as e:
            last_error = f"Parse error: {e}"
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
        except Exception as e:
            last_error = str(e)
            if attempt < retries - 1:
                wait = 5 * (attempt + 1)
                print(f"  Retry {attempt + 1}/{retries} in {wait}s: {e}")
                time.sleep(wait)

    return {
        "category": "ERROR",
        "subcategory": "",
        "rationale": f"Failed after {retries} attempts: {last_error}",
    }


def main():
    parser = argparse.ArgumentParser(
        description="Label product clusters with categories via LLM (Phase 2)."
    )
    parser.add_argument(
        "--input",
        type=str,
        default=None,
        help="Path to Phase 1 cluster JSON (default: data/product_clusters.json).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output path (default: data/product_categories_draft.json).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Label only the first 3 non-empty clusters as a test.",
    )
    parser.add_argument(
        "--start-at",
        type=int,
        default=0,
        help="Resume labelling from this cluster index (0-based).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay in seconds between API calls (default: 1.0).",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help=f"Gemini model (default: {Config.DEFAULT_MODEL}).",
    )
    args = parser.parse_args()

    # ---- Load Phase 1 output ----
    data_dir = Path(Config.DATA_DIR)
    input_path = args.input or str(data_dir / "product_clusters.json")
    output_path = args.output or str(data_dir / "product_categories_draft.json")

    print(f"Loading clusters from: {input_path}")
    with open(input_path, "r") as f:
        clusters = json.load(f)

    non_empty = [c for c in clusters if c.get("size", 0) > 0]
    print(f"Clusters: {len(clusters)} total, {len(non_empty)} non-empty")

    if args.dry_run:
        non_empty = non_empty[:3]
        print(f"DRY RUN — labelling only {len(non_empty)} clusters")

    # ---- Resume support ----
    existing_results = []
    if args.start_at > 0 and Path(output_path).exists():
        with open(output_path, "r") as f:
            existing_results = json.load(f)
        print(f"Resuming from index {args.start_at} "
              f"(already have {len(existing_results)} results)")

    # ---- Init Gemini client ----
    print("Initialising Gemini client...")
    client = genai.Client()
    model = args.model or Config.DEFAULT_MODEL
    print(f"Using model: {model}")

    # ---- Label each cluster ----
    results = list(existing_results)
    start_idx = max(args.start_at, len(existing_results))

    for i, cluster in enumerate(non_empty):
        if i < start_idx:
            continue

        cluster_id = cluster["cluster_id"]
        size = cluster["size"]
        print(f"\n[{i + 1}/{len(non_empty)}] Cluster {cluster_id} "
              f"({size:,} products)...")

        label = label_cluster(client, model, cluster)
        print(f"  → {label.get('category')} / {label.get('subcategory')}")

        results.append({
            "cluster_id": cluster_id,
            "size": size,
            "avg_distance": cluster.get("avg_distance", 0),
            "category": label.get("category", ""),
            "subcategory": label.get("subcategory", ""),
            "rationale": label.get("rationale", ""),
            "samples": cluster.get("samples", []),
        })

        # Save incrementally
        with open(output_path, "w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

        if i < len(non_empty) - 1:
            time.sleep(args.delay)

    # ---- Summary ----
    print(f"\n{'='*60}")
    print(f"Done! {len(results)} clusters labelled.")
    print(f"Output: {output_path}")

    # Quick stats
    categories = set()
    for r in results:
        cat = r.get("category", "")
        sub = r.get("subcategory", "")
        categories.add(f"{cat} / {sub}")

    print(f"Unique category/subcategory pairs: {len(categories)}")
    dupes = len(results) - len(categories)
    if dupes > 0:
        print(f"Duplicate pairs (merge candidates): {dupes}")

    errors = [r for r in results if r.get("category") in ("ERROR", "EMPTY_CLUSTER")]
    if errors:
        print(f"Errors/empty: {len(errors)}")


if __name__ == "__main__":
    main()
