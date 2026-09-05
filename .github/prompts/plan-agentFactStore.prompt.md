# Plan: Agent Fact Store (Knowledge Base) — Minimal RAG in Postgres

## Overview

Build a persistent "Fact" / "Rule" store for the agents using the existing
PostgreSQL database plus `pgvector`. Each fact combines **structured context**
(who/what/where it applies to), **markdown content**, and an **embedding** of
the content for semantic search. Agents and users accumulate facts over time;
supplier hunting, product↔brand matching, and quotation prep query the store.

Status: **PLANNING ONLY** (2026-09-04). Not scheduled for implementation yet.

---

## Agreed Decisions

1. **Item taxonomy** — reuse the existing product category hierarchy
   (`data/product_categories_final.json`, `data/product_category_hierarchy.md`,
   12 super-categories → 23 categories → 100 subcategories). Facts store
   `item_type` (category name) and `item_subtype` (subcategory name).
   Super-category is derivable at query time via the mapping data.
   The taxonomy itself is acknowledged as needing more work later — store
   **names** in facts for now; migrate to stable IDs when the taxonomy matures.
2. **Supplier references by ID** — facts carry a structured `supplier_id`
   (FK `suppliers.id`, authoritative) and may reference suppliers inside the
   markdown content using the convention `use ABC Pty Ltd (ID:<short-id>)`.
   `suppliers.id` is a UUID; content should use a short form (first 8 chars)
   plus a name snapshot for human readability (name may drift after renames).
3. **Similarity check on insert** — before a new fact is stored, compare its
   embedding against existing facts in the same context bucket; surface
   "possible conflict — supersede?" rather than silently duplicating.

---

## Data Model

### `facts` table (alembic migration)

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `context` | text | `supply chain` \| `customer preference` \| `quotation` \| `general` (starting set — expected to grow) |
| `supplier_id` | uuid NULL FK `suppliers.id` | structured ref (authoritative) |
| `customer_id` | uuid NULL FK `customers.id` | add later — column now or when needed |
| `item_id` | uuid NULL FK `products.id` | most specific context |
| `item_type` | text NULL | category name from hierarchy |
| `item_subtype` | text NULL | subcategory name from hierarchy |
| `brand` | text NULL | brand name (snapshot; brand table link later) |
| `country` | char(2) NULL | ISO code |
| `state` | text NULL | e.g. NSW (AU states) |
| `fact_kind` | text | `preference` \| `policy` \| `observation` \| `contact` (default `observation`) |
| `content` | text | markdown, kept short (agent-instructed ≤ ~200 words) |
| `source` | text | `user` \| `agent` \| `pipeline` |
| `created_by` | text | email/agent id |
| `is_verified` | bool | default false |
| `is_active` | bool | default true (soft-delete / archive) |
| `usage_count` | int | incremented on retrieval |
| `last_used_at` | timestamptz NULL | |
| `expires_at` | timestamptz NULL | optional |
| `superseded_by` | uuid NULL FK `facts.id` | links to the replacement fact |
| `embedding` | vector(768) NULL | content embedding |
| `embedding_model` | text NULL | e.g. `text-embedding-004` |
| `created_at` / `updated_at` | timestamptz | |

Indexes:

- Composite btree `(context, item_type, item_subtype, brand, country)` for
  context-filtered retrieval.
- HNSW on `embedding` with `vector_cosine_ops` (add when scale warrants;
  exact scan is fine for the first few thousand facts).
- Optional GIN trigram on `content` if we ever want text-prefix conflict checks.

### `pgvector` setup

- Local dev: ensure `CREATE EXTENSION IF NOT EXISTS vector;` runs on the
  postgres container (`start_postgres.sh`) — verify the image ships the
  extension (e.g. `pgvector/pgvector` image) or install via apt.
- Railway (prod): enable the extension in the migration itself.
- Python: add `pgvector` package; SQLAlchemy `Vector` column type
  (`pgvector.sqlalchemy`).

---

## Embedding Service

- `includes/memory/embeddings.py` — `embed_text(text) -> list[float]` using
  Vertex AI `text-embedding-004` (768 dims), reusing the existing Vertex AI
  setup (`GOOGLE_GENAI_USE_VERTEXAI` etc.).
- Embed a canonical rendering of content **plus context**:
  `"supplier fact; brand Toyota; item type Engine & Powertrain Components; … <content>"`
  so content-direction search works even when structured context is absent.
- Store `embedding_model` per row; provide `scripts/reembed_facts.py` to
  re-embed the table after a model change.

---

## Matching & Precedence

### Context matching

- Exact matches on structured fields, plus **rollup**:
  - A fact on an `item_subtype` also matches queries for its parent
    `item_type` / super-category.
  - A fact with no `item_type`/`brand`/`country` matches everything of its
    `context` (global fact).
- Location: exact `country` match preferred; no-country facts are global.
  State only refines country matches.

### Specificity scoring (structural)

`item_id` (4.0) > `brand + item_type + item_subtype` (3.0) > `brand + item_type`
(2.5) > `brand` (2.0) > `item_type` (1.5) > `context` (1.0) > global (0.5).
Add +0.25 for country match, +0.1 for state match.

### Query shape

`search_facts(context, brand, item_type, item_subtype, item_id, country, query, limit=5)`:

1. SQL filter on context (exact + rollup), all `is_active`.
2. If `query` provided: rank candidates by cosine similarity;
   `score = 0.7 * cosine + 0.3 * normalized_specificity`.
3. No structured match: fall back to pure vector top-k over all facts.
4. Return per fact: content, matched context, specificity, source,
   `is_verified`, usage stats. Increment `usage_count` / `last_used_at`.

### Conflict detection on insert

- Bucket: same `context` + (brand or item_type) + country.
- Flag as possible conflict when cosine ≥ threshold (start 0.88, tune later)
  or when a bucketed fact has identical normalized content.
- Caller flow: agent *proposes* → lists conflicts → user chooses
  **supersede** (set `superseded_by` on the old fact) / **keep both** / **cancel**.

---

## APIs & Agent Integration

### Core module

- `includes/memory/facts.py`:
  - `add_fact(...)` (with conflict report), `search_facts(...)`,
    `supersede_fact(old_id, new_id)`, `archive_fact(id)`, `touch_fact(id)`.
- `includes/memory/embeddings.py` (see above).

### Agent tools (procurement agent)

- `search_facts` — read-only retrieval tool used by:
  - supplier hunting (`find_new_suppliers`, `search_previous_suppliers`,
    `search_brand_suppliers`) → "Known facts" section injected into the prompts
  - product↔brand matching
  - quotation prep (supplier selection / shortlisting)
- `propose_fact` — writes only after user confirmation (agent gathers
  context + content, presents it, user approves). No silent writes.

### Dashboard API (minimal, Preline UI per direction)

- `GET /api/facts` (list/filter/search), `POST /api/facts`, `PATCH /api/facts/{id}`
  (edit/verify/archive) — enough for a small Facts page (list, search, add,
  archive). UI page itself is a later phase.

### Relationship to existing memory

- Separate layer from the LangGraph cross-thread user memory
  (`docs/CROSS_THREAD_MEMORY.md`) — that's per-user profile data; facts are
  shared domain knowledge. No collision; facts could later be seeded from
  profile data.

---

## Rollout Phases

1. **Foundations** — alembic migration, pgvector enable (local + prod),
   `Vector` model, embedding service, re-embed script.
2. **Core module + tests** — `facts.py` with matching/precedence/conflict
   logic; `tests/test_fact_store.py`.
3. **Agent read path** — `search_facts` tool wired into supplier hunting and
   product matching prompts.
4. **Agent write path** — `propose_fact` confirmation flow; dashboard API.
5. **UI + later** — Facts page, email-pipeline fact extraction, taxonomy IDs.

---

## Open Questions

- UUID display convention in content: short 8-char id OK?
- Add `customer_id` column now (for schema stability) or when customer facts arrive?
- Who/what sets `is_verified` — a user action in the UI, or agent self-marking with `source`? 
- Conflict cosine threshold — start 0.88?
- Hard cap on `content` length or agent-instructed only?
- Should facts have an explicit `weight`/strength override?

## Testing

- `tests/test_fact_store.py`:
  - precedence ordering (specific beats general)
  - item_type/subtype rollup matching
  - country/global fallback
  - vector fallback with empty context
  - conflict detection & supersede chain
  - `usage_count`/`last_used_at` touches
