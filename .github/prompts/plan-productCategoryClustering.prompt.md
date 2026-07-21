---
mode: agent
description: "Discover ~100 product categories via K-Means clustering on existing embeddings, then label with LLM"
status: IN PROGRESS — Phases 1, 2, 2.5 complete. Phases 3 & 4 pending.
last_updated: 2026-07-21
---

# Product Category Discovery via Clustering

## Goal

Define approximately 100 product categories (2-level hierarchy) by clustering existing product embeddings, sampling representative products from each cluster, and using an LLM to propose category names. Final result: **12 super-categories → 23 consolidated top-level categories → 100 subcategories** (the K-Means clusters).

## Context

- **Products table**: `products` with 256-dim embeddings (Gemini `embedding-2-preview` via pgvector)
- **Embedding text**: `part_number | description | brand`
- **~200,000 products** have embeddings (out of ~300,000 total)
- **Industry**: Mining and building procurement — spare parts, full machinery, tools, accessories, safety gear, motors, electrical components, hydraulics, fasteners, PPE, etc.
- **Hierarchy**: 3-level (Super-category → Category → Subcategory), flattening to 2-level by merging super-category into category for practical use

---

## ✅ DONE — Phase 1: Clustering

**Script**: `scripts/cluster_products.py`
**Output**: `data/product_clusters.json`

- Loaded all ~200k product embeddings from local database
- Ran MiniBatchKMeans with `n_clusters=100`, `n_init=3`, `max_iter=100`
- Normalised vectors (L2) so Euclidean ≈ cosine distance
- Per cluster: centroid, size, avg_distance, 10 samples (7 close + 3 edge)
- **Results**: 100 clusters, 0 empty, size range 399–5,168, mean ~2,000
- Runs against local DB only (local data mirrors production, no `--production` flag needed)

```bash
uv run python -m scripts.cluster_products
uv run python -m scripts.cluster_products --n-clusters 150  # custom count
```

---

## ✅ DONE — Phase 2: LLM Labelling

**Script**: `scripts/label_product_clusters.py`
**Output**: `data/product_categories_draft.json`

- Loaded `data/product_clusters.json`
- Sent each cluster's 10 sample products to Gemini (`gemini-3.1-flash-lite`, temperature 0.2)
- LLM proposed Category + Subcategory + rationale per cluster
- Features: incremental save (resume with `--start-at`), `--dry-run`, `--model` override, retry on transient errors
- **Results**: 100/100 labelled, 0 errors, 63 unique top-level names (many near-duplicates from isolated naming), 100 unique category/subcategory pairs

```bash
uv run python -m scripts.label_product_clusters
uv run python -m scripts.label_product_clusters --dry-run           # test 3 clusters
uv run python -m scripts.label_product_clusters --start-at 59       # resume
```

---

## ✅ DONE — Phase 2.5: Category Consolidation

**Script**: `scripts/consolidate_categories.py`
**Outputs**: `data/product_categories_final.json`, `data/product_categories_final_passA.json`

Two-pass LLM consolidation:

**Pass A** — Merged near-duplicate top-level category names (named in isolation in Phase 2).
- 63 unique names → 23 consolidated categories
- e.g. "Fasteners & Hardware" + "Fasteners & Mechanical Hardware" → "Fasteners & Hardware"

**Pass B** — Grouped the 23 consolidated categories into super-categories.
- 23 categories → 12 super-categories
- 10–20 super-categories was the target; 12 was the LLM's natural grouping

### Final 12 Super-Categories

| # | Super-Category | Categories |
|---|---------------|-----------|
| 1 | Engine & Powertrain Systems | Engine & Powertrain Components, Mechanical Components & Drivetrain |
| 2 | Heavy Equipment & Undercarriage | Heavy Equipment & Machinery Parts, Ground Engaging Tools & Undercarriage |
| 3 | Hydraulics & Fluid Management | Hydraulics & Fluid Power, Filtration & Fluid Management |
| 4 | Sealing & Fastening Solutions | Seals, Gaskets & O-Rings, Fasteners & Hardware |
| 5 | Electrical & Automation | Electrical Components & Systems |
| 6 | Automotive & Fleet Maintenance | Automotive & Fleet Maintenance |
| 7 | Tools & Workshop Equipment | Tools & Workshop Equipment, Welding & Industrial Equipment |
| 8 | Safety, PPE & Lifting | Safety & PPE, Lifting/Rigging, Site Safety & Material Handling |
| 9 | MRO & Site Consumables | MRO Supplies, Consumables & Site Supplies, Office & Maintenance |
| 10 | Building & Industrial Infrastructure | Building Materials, Industrial Infrastructure & Maintenance |
| 11 | Industrial Equipment & Appliances | Industrial Equipment & Appliances, Industrial Maintenance |
| 12 | IT & Office Technology | IT & Office Equipment |

```bash
uv run python -m scripts.consolidate_categories
uv run python -m scripts.consolidate_categories --dry-run
```

---

## ⬜ TODO — Phase 3: Review Export

**Script to create**: `scripts/export_category_review.py` (NOT YET WRITTEN)

Generate a CSV/spreadsheet for manual review:
- Cluster ID, super-category, category, subcategory, cluster size, avg_distance, rationale, top 5 product descriptions
- Sorted by super-category → category → subcategory
- Include summary stats: size distribution, high-distance clusters (quality concerns), outliers

---

## ⬜ TODO — Phase 4: Assign Categories to Products

**Script to create**: `scripts/assign_product_categories.py` (NOT YET WRITTEN)

After manual review & approval of the taxonomy:
1. Load the K-Means model (need to save it in Phase 1 first, or re-run and pickle it)
2. For each product with an embedding, find nearest centroid → map to cluster → assigned category
3. Store in a new `product_categories` table or add columns to `products`
4. Store distance-to-centroid as a confidence score
5. Products without embeddings: leave uncategorised (or generate embeddings first)

**Note**: Phase 1 currently doesn't save the K-Means model. Will need to either:
- Re-run Phase 1 with pickle support, or
- Re-run K-Means with the same random seed on the same data

---

## Dependencies Added

- `scikit-learn~=1.6` (added to `pyproject.toml`)

---

## All Files (Current State)

```
scripts/
  cluster_products.py              # ✅ Phase 1 — K-Means clustering
  label_product_clusters.py        # ✅ Phase 2 — LLM labelling per cluster
  consolidate_categories.py        # ✅ Phase 2.5 — merge duplicates + super-categories
  export_category_review.py        # ⬜ Phase 3 — NOT YET WRITTEN
  assign_product_categories.py     # ⬜ Phase 4 — NOT YET WRITTEN

data/
  product_clusters.json            # ✅ Phase 1 output — 100 clusters with samples
  product_categories_draft.json    # ✅ Phase 2 output — 100 labelled clusters (raw)
  product_categories_final.json    # ✅ Phase 2.5 output — final 3-level taxonomy
  product_categories_final_passA.json  # ✅ Phase 2.5 intermediate — merge decisions
  product_categories_review.csv    # ⬜ Phase 3 output — NOT YET GENERATED

.github/prompts/
  plan-productCategoryClustering.prompt.md  # This plan document
```

---

## Quick Resumption

```bash
# Review the current taxonomy:
cat data/product_categories_final.json | python3 -m json.tool | less

# Re-run consolidation if category names need tweaking:
uv run python -m scripts.consolidate_categories

# Next step — generate review CSV:
# (script not yet written)

# Final step — assign categories to all products:
# (script not yet written)
```
