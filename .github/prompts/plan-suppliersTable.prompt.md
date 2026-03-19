## Plan: Suppliers Table Expansion, Import & Search Tool

### Overview

Expand the existing `suppliers` table from a minimal stub (id, netsuite_id, name) into a full supplier record with contact details, address, notes, and a JSON contacts field. Create a `supplier_brands` join table to link suppliers to canonical brands. Build an import script that reads supplier CSV data, maps the comma-separated brands list to the normalised brands table, and populates the contacts JSON from the CSV's contact columns. Add a search tool to the ProcurementAgent.

---

### Phase 1: Database Schema Changes ✅

1. **Expand the `Supplier` model** in `includes/db_models.py` — add new columns:
   - `url` — String, nullable
   - `address_1` — String, nullable
   - `city` — String, nullable
   - `country` — String, nullable
   - `notes` — Text, nullable
   - `contacts` — JSONB, nullable (array of contact objects)

2. **Create `SupplierBrand` join model** in `includes/db_models.py`:
   - `id` — UUID primary key
   - `supplier_id` — UUID, FK → `suppliers.id`, not null
   - `brand_id` — UUID, FK → `brands.id`, not null
   - Unique constraint on (`supplier_id`, `brand_id`) to prevent duplicates
   - Indexes on both foreign keys

3. **Create Alembic migration** to:
   - Add new columns to `suppliers` table
   - Create `supplier_brands` join table
   - Note: existing supplier data (id, netsuite_id, name) must be preserved

---

### Phase 2: Import Script — `scripts/import_suppliers.py` ✅

> **🐛 Known issue — production import hangs**
> The script freezes on the first batch when running against the remote Railway production database (`--production`). The per-row SELECT was replaced with a batch `IN()` pre-fetch, but it still hangs. Likely causes to investigate:
> - `link_supplier_brands()` still does one `INSERT ... ON CONFLICT` per brand per supplier — high round-trip cost over the network. Consider batching all brand-link inserts into a single `executemany` or bulk `INSERT ... ON CONFLICT DO NOTHING` per batch.
> - `session.flush()` after every new supplier insert — consider deferring flushes until after the full batch (use `session.bulk_save_objects` or collect new suppliers, flush once, then link brands).
> - Connection timeout / pool settings — Railway may have aggressive idle timeouts. Try adding `pool_pre_ping=True` and `connect_args={"connect_timeout": 10}` to `create_engine`.
> - Add progress logging *inside* the row loop (e.g. every 50 rows) to pinpoint where the hang occurs.

Replace the basic supplier import in `import_products.py` with a dedicated script following established patterns:

1. Accept `--production` flag via argparse.
2. Read CSV file(s) matching `suppliers_import*.csv` from `Config.IMPORT_DIR`.
3. **Data cleaning**:
   - Strip whitespace on all string fields.
   - Clean name field using same approach as brand name cleaning.
   - Normalise URLs (strip whitespace; optionally prepend `https://` if missing scheme).
4. **Contacts JSON construction** from CSV columns:
   - Build a contacts array from the CSV fields:
     - `email` + `phone` → contact with `label: "Main"`
     - `best_contact` + `best_contact_email` → contact with `label: "Best"` (no phone available for this contact)
   - Only include contact entries where at least name or email is present.
   - Example output:
     ```json
     [
       {"label": "Main", "name": null, "email": "info@supplier.com", "phone": "+61 2 9999 1234"},
       {"label": "Best", "name": "Lucy Smith", "email": "lucy@supplier.com", "phone": null}
     ]
     ```
5. **Upsert logic** keyed on `netsuite_id`:
   - If supplier exists → update all non-empty fields (don't overwrite with blanks).
   - Otherwise → insert new record.
6. **Brand linking** — for each supplier's `brands` CSV column:
   - Split on comma, strip whitespace on each brand name.
   - For each brand name, look up in the `brands` table (case-insensitive match on `name`).
   - If the matched brand is a duplicate (`duplicate_of IS NOT NULL`), resolve to the canonical brand.
   - Insert into `supplier_brands` join table (skip if already linked).
   - Log unmatched brand names so they can be reviewed.
7. Batch processing (200 per batch).
8. Summary output: suppliers inserted/updated/skipped, brand links created, unmatched brands listed.

---

### Phase 3: Search Tool — `search_suppliers` ✅

1. Add a `search_suppliers` tool to `includes/tools/product_tools.py`:
   - Accept parameters: `name` (str, optional), `brand` (str, optional), `country` (str, optional), `query` (str, optional — general text search across name, notes, city).
   - Search by `name` using `ilike`.
   - If `brand` is provided, join through `supplier_brands` → `brands` to filter by brand name (include duplicate resolution).
   - If `country` is provided, filter by `country` using `ilike`.
   - If `query` is provided, search across `name`, `notes`, `city` using `ilike` with OR.
   - Return formatted results with name, contacts, city, country, and linked brand names.
   - Limit results (default 20) with total count.
2. Register `search_suppliers` on `ProcurementAgent` in `includes/agents/procurement_agent.py`.
3. Update ProcurementAgent system prompt to describe the new tool and when to use it.

---

### Phase 4: Retire old supplier import from `import_products.py` ✅

1. Remove the `import_suppliers` function and `suppliers_import*.csv` routing from `scripts/import_products.py`.
2. Update the docstring in `import_products.py` to reflect it now only handles products.
3. The old `suppliers_import*.csv` files will now be handled by the new `scripts/import_suppliers.py`.

---

### Phase 5: Vector Search on Supplier Notes

Add semantic search to `search_suppliers` so users can describe what kind of supplier they need and get the best matches, following the same pattern used for product embeddings.

#### 5a. Schema — add embedding column

1. **Add `embedding` column** to the `Supplier` model in `includes/db_models.py`:
   - `embedding` — `Vector(256)`, nullable (same dimensions as Product)
2. **Create Alembic migration** to add the column.
   - Remember to clean out auto-detected Chainlit/LangGraph table drops before applying.

#### 5b. Embedding generation script — `scripts/update_supplier_embeddings.py`

Create a new script following the same pattern as `scripts/update_product_embeddings.py`:

1. Accept `--production` flag via argparse.
2. Query all suppliers where `embedding IS NULL` and `notes IS NOT NULL` (no text = nothing to embed).
3. **Build embedding text** for each supplier by combining relevant fields:
   - `Name: {name}` (always present)
   - `Notes: {notes}` (the primary content for semantic search)
   - `City: {city}` / `Country: {country}` (if present — helps with location-aware searches)
   - `Brands: {comma-separated brand names}` (fetch from `supplier_brands` join — helps match by product type)
   - Join parts with ` | ` separator.
4. Generate embeddings in batches of 100 using `GoogleGenerativeAIEmbeddings(model=Config.EMBEDDINGS_MODEL, output_dimensionality=256)`.
5. Save vectors back to the `suppliers.embedding` column.
6. Summary output: total suppliers processed, embeddings generated, skipped (no notes).

#### 5c. Update `search_suppliers` tool

Update `_do_supplier_search()` in `includes/tools/product_tools.py` to add a vector search stage:

1. **Search strategy**: brand filter first, then two-stage text search (same approach as `search_products`):
   - If `brand` is provided, always apply the brand join filter first (this narrows the candidate set).
   - **Stage 1**: Apply any `name`, `country`, `query` ilike filters as today (string matching).
   - **Stage 2 (vector fallback)**: If `query` is provided and string results are below the limit:
     - Embed the `query` text using `get_embeddings_model().embed_query(query)`.
     - Order remaining candidates by `Supplier.embedding.cosine_distance(query_vector)`.
     - Merge vector results with string results, deduplicating by supplier ID.
   - This means a search like "supplier that sells industrial adhesives in Sydney" would:
     1. Skip brand filter (none provided).
     2. String-match "industrial adhesives" across name/notes/city → may find some.
     3. Vector-search the full query against notes embeddings → surface semantically similar suppliers.
2. Update the `search_suppliers` tool docstring to mention that `query` supports semantic/descriptive searches.
3. Update the ProcurementAgent system prompt to note that `query` can accept natural language descriptions of what the user is looking for.

---

### Proposed Database Schema

```
Table: suppliers (expanded)
  id            UUID        PK, default uuid_generate_v4()
  netsuite_id   VARCHAR     UNIQUE
  name          VARCHAR     NOT NULL
  url           VARCHAR     NULLABLE
  address_1     VARCHAR     NULLABLE
  city          VARCHAR     NULLABLE
  country       VARCHAR     NULLABLE
  notes         TEXT        NULLABLE
  contacts      JSONB       NULLABLE

Table: supplier_brands (new join table)
  id            UUID        PK, default uuid_generate_v4()
  supplier_id   UUID        FK → suppliers.id, NOT NULL, INDEXED
  brand_id      UUID        FK → brands.id, NOT NULL, INDEXED
  UNIQUE(supplier_id, brand_id)
```

Contacts JSONB schema:
```json
[
  {
    "label": "Main" | "Best" | string,
    "name": "Contact Name" | null,
    "email": "email@example.com" | null,
    "phone": "+61 400 000 000" | null
  }
]
```

---

### CSV Column Mapping

| CSV Column | → | DB Field / Action |
|------------|---|-------------------|
| `netsuite_id` | → | `suppliers.netsuite_id` (upsert key) |
| `name` | → | `suppliers.name` |
| `email` | → | `contacts[].email` (label: "Main") |
| `phone` | → | `contacts[].phone` (label: "Main") |
| `url` | → | `suppliers.url` |
| `address_1` | → | `suppliers.address_1` |
| `city` | → | `suppliers.city` |
| `country` | → | `suppliers.country` |
| `brands` | → | Split on comma → lookup brands → `supplier_brands` join table |
| `notes` | → | `suppliers.notes` |
| `best_contact` | → | `contacts[].name` (label: "Best") |
| `best_contact_email` | → | `contacts[].email` (label: "Best") |

---

### Relevant Files

| File | Action |
|------|--------|
| `includes/db_models.py` | Expand `Supplier` model, add `SupplierBrand` join model, add `embedding` column |
| `alembic/versions/xxx_expand_suppliers.py` | New migration |
| `alembic/versions/xxx_add_supplier_embedding.py` | New migration (Phase 5) |
| `scripts/import_suppliers.py` | New dedicated supplier import script |
| `scripts/import_products.py` | Remove supplier import code |
| `scripts/update_supplier_embeddings.py` | New embedding generation script (Phase 5) |
| `includes/tools/product_tools.py` | Add `search_suppliers` tool, add vector search stage (Phase 5) |
| `includes/agents/procurement_agent.py` | Register supplier search tool, update prompt |
| `data/import/suppliers_import*.csv` | Input CSV file(s) |

---

### Decisions

| Decision | Rationale |
|----------|-----------|
| JSONB for contacts | Flexible schema — can add more contact types without migrations. Queryable in PostgreSQL. All contact info (email, phone) stored here rather than as top-level columns. |
| Separate join table for supplier-brands | Proper many-to-many relationship, enables querying "which suppliers carry this brand?" and vice versa. |
| Resolve to canonical brands during import | Ensures join table always points to canonical brands, consistent with deduplication work. |
| Log unmatched brands | Brands in CSV that don't exist in the brands table need manual review — may indicate missing brand imports or typos. Don't auto-create. |
| Retire old supplier import | Avoid confusion with two import paths; the new script is a superset. |

---

### Answers

1. No top-level `email`/`phone` columns — all contact info stored in the `contacts` JSONB field only.
2. No phone column for best contact in the CSV — only `best_contact` (name) and `best_contact_email`.
3. Unmatched brands are logged for review only — not auto-created in the brands table.
