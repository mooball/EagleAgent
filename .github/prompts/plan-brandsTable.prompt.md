## Plan: Brands Table with Import & Deduplication

### Overview

Introduce a `brands` table to normalise brand data currently stored as a free-text string on products. The table will be populated from a CSV file via an import script that cleans messy names (whitespace, odd characters, inconsistent casing). A separate interactive deduplication tool will help identify and link duplicate brands (e.g. different casing, trailing "s", hyphens vs spaces, misspellings) by populating a `duplicate_of` foreign key.

---

### Phase 1: Database Schema

1. Add `Brand` model to `includes/db_models.py`:
   - `id` — UUID primary key (matches existing pattern)
   - `netsuite_id` — String, unique, not null
   - `name` — String, not null
   - `duplicate_of` — UUID, nullable, foreign key → `brands.id` (self-referential; points to the canonical brand this record is a duplicate of)
2. Create Alembic migration for the `brands` table with unique constraint on `netsuite_id` and index on `duplicate_of`.

---

### Phase 2: Import Script — `scripts/import_brands.py`

Follow the same patterns as `scripts/import_products.py`:

1. Accept `--production` flag via argparse.
2. Read CSV file(s) matching `brands_import*.csv` from `Config.DATA_DIR`.
3. **Data cleaning** on the `name` field during import:
   - Strip leading/trailing whitespace.
   - Collapse multiple consecutive spaces/tabs to a single space.
   - Remove non-standard-Latin characters (keep ASCII letters, digits, common punctuation like `-`, `&`, `.`, `'`, `/`). Use a regex whitelist approach.
4. Upsert logic keyed on `netsuite_id`:
   - If a brand with the same `netsuite_id` exists → update `name` (with cleaned value).
   - Otherwise → insert new record.
5. Batch processing (200 per batch) to stay within PG parameter limits.
6. Skip rows where `netsuite_id` is empty/null.
7. Summary output: count of inserted, updated, skipped records.

---

### Phase 3: Deduplication Tool — `scripts/deduplicate_brands.py`

**Why a separate interactive script (not during import):**
- Duplicate detection is fuzzy and requires human judgement — automated merging risks data loss.
- Keeping it separate means the import can run unattended (CI/cron), while dedup is a one-off or periodic human-assisted task.
- The canonical brand choice (which record to keep) is a business decision, not a data one.

**Approach — interactive CLI with fuzzy matching:**

1. Accept `--production` flag.
2. Load all brands where `duplicate_of IS NULL` (i.e. not already marked as duplicates).
3. Build candidate duplicate groups using multiple signals:
   - **Normalised comparison key**: lowercase, strip whitespace, remove hyphens/punctuation, remove trailing "s" → if two brands produce the same key, they are candidates.
   - **Fuzzy string matching** (e.g. `rapidfuzz` or `thefuzz`): compare all pairs within a similarity threshold (e.g. ≥ 85% ratio). This catches misspellings and minor variations.
4. Present each candidate group to the user interactively:
   ```
   Possible duplicates found:
     1. [id=abc] "Hilti"       (netsuite_id: 1001)
     2. [id=def] "HILTI"       (netsuite_id: 1002)
     3. [id=ghi] "Hiltis"      (netsuite_id: 1003)

   Which is the canonical brand? Enter number (or 's' to skip): 1
   ```
5. When the user picks a canonical brand, set `duplicate_of = <canonical_id>` on all other records in the group.
6. Provide a `--dry-run` flag that shows candidate groups without writing changes.
7. Provide a `--auto` flag that automatically picks the shortest/most-common name as canonical (for bulk runs with human review afterwards via `--dry-run`).
8. Summary output: count of groups found, groups resolved, brands marked as duplicates.

**Dependencies:** Add `rapidfuzz` to `pyproject.toml` dependencies.

---

### Phase 4: Brand Search Tool for ProcurementAgent

1. Add a `search_brands` tool to `includes/tools/product_tools.py` (alongside the existing `search_products` tool):
   - Accept a `query` string parameter.
   - Search by `name` using case-insensitive `ilike` matching across **all** brands (including duplicates).
   - If a match is a duplicate, resolve it to the canonical brand (follow `duplicate_of`).
   - Deduplicate results so the same canonical brand isn't listed twice.
   - Return results as a formatted list with `name` and `netsuite_id`.
   - Limit results (e.g. top 20) with a count of total matches.
2. Register the `search_brands` tool on the `ProcurementAgent` in `includes/agents/procurement_agent.py`.
3. Update the ProcurementAgent system prompt to mention brand search capability.

---

### Phase 5: Link Products to Brands (future, out of scope)

_Not part of this plan, but the logical next step:_
- Add `brand_id` FK on `products` table pointing to `brands.id`.
- Migration to populate `brand_id` by matching the cleaned `products.brand` string to `brands.name`.
- Update `ProcurementAgent` / `product_tools.py` to join through the brands table.

---

### Proposed Database Schema

```
Table: brands
  id            UUID        PK, default uuid_generate_v4()
  netsuite_id   VARCHAR     UNIQUE NOT NULL
  name          VARCHAR     NOT NULL
  duplicate_of  UUID        FK → brands.id, NULLABLE
```

Self-referential relationship: `duplicate_of` points to the canonical brand record. A brand with `duplicate_of = NULL` is either unique or is itself the canonical entry.

---

### Relevant Files

| File | Action |
|------|--------|
| `includes/db_models.py` | Add `Brand` model |
| `alembic/versions/xxx_add_brands_table.py` | New migration |
| `scripts/import_brands.py` | New import script |
| `scripts/deduplicate_brands.py` | New interactive dedup tool |
| `includes/tools/product_tools.py` | Add `search_brands` tool |
| `includes/agents/procurement_agent.py` | Register brand search tool |
| `pyproject.toml` | Add `rapidfuzz` dependency |
| `data/brands_import*.csv` | Input CSV file(s) |

---

### Decisions

| Decision | Rationale |
|----------|-----------|
| Separate dedup from import | Import should be automated and safe to re-run; dedup requires human judgement |
| Self-referential FK for duplicates | Simple, queryable (`WHERE duplicate_of IS NULL` = canonical brands), no extra junction table |
| Interactive CLI for dedup | Safer than auto-merge; human confirms each group |
| `rapidfuzz` for fuzzy matching | Fast C-based fuzzy string matching, well-maintained, handles misspellings well |
| Regex whitelist for character cleaning | Safer than blacklist — explicitly keep known-good characters rather than trying to enumerate all bad ones |
| `--dry-run` and `--auto` flags | Flexibility: preview before committing, or bulk-process with review after |

---

### Answers

1. CSV columns are `netsuite_id`, `name` — confirmed.
2. No need to seed from `products.brand` — both tables come from the same source.
3. No delete functionality planned — no cascade behaviour needed for now.
4. Start with 85% fuzzy match threshold — will tune after reviewing results with `--dry-run`.
