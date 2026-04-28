# Plan: Augment Suppliers — New Fields, Edit UI & Change Tracking

Add new data fields to the Supplier model, make suppliers editable from the dashboard UI, and track modifications for future NetSuite sync.

## Status: Complete

## Context

The `suppliers` table currently has: `id`, `netsuite_id`, `name`, `url`, `address_1`, `city`, `country`, `notes`, `contacts` (JSONB), `embedding`. We need to enrich it with categorization, trade terms, and a comments system. We also need dashboard edit capability with change tracking.

The supplier categorization script (`scripts/categorize_suppliers.py`) is already producing `category`, `tier`, `confidence`, and `reasoning` per supplier — these need a home in the DB.

---

## Phase 1: Model & Migration

### 1. ✅ Add new columns to the `Supplier` model

File: `includes/dashboard/models.py`

```python
# New fields on Supplier:
comments        = Column(JSONB, nullable=True)           # list of {author, comment, timestamp}
supply_chain_position = Column(JSONB, nullable=True)     # {category, tier, confidence, reasoning}
terms           = Column(String, nullable=True)           # e.g. "30 days", "COD", "Prepaid"
modified_at     = Column(DateTime(timezone=True), nullable=True)  # last edit timestamp
modified_by     = Column(String, nullable=True)           # who made the edit ("user:tom", "netsuite", "ai:categorizer")
```

**Design decisions:**

- **`comments`** — JSONB list of objects: `[{author: str, comment: str, ts: str}]`. Using a list inside JSONB keeps it simple (no new table) while allowing multiple entries. `ts` is ISO-8601 so comments are orderable.
- **`supply_chain_position`** — JSONB object rather than separate columns. Rationale: the categorization script already emits a structured object (`{category, tier, confidence, reasoning}`), and the schema may evolve as the taxonomy matures. JSONB lets us store the full result without frequent migrations. Tier values map to the taxonomy tiers (A/B/C/D) from `docs/supplier-categorization-taxonomy.md`; category is the role name (e.g. "Trade Wholesaler", "OEM").
- **`terms`** — Simple string. Trade terms are short free-text values ("30 days", "COD", "Net 60 EOM"). A string is sufficient; we can normalize later if needed.
- **`modified_at` / `modified_by`** — Lightweight change tracking. `modified_by` uses a prefixed convention (`user:`, `netsuite:`, `ai:`) to distinguish the edit source. This is enough for a future sync endpoint to detect local changes since the last NetSuite push.

### 2. ✅ Alembic migration

File: `alembic/versions/xxxx_add_supplier_augment_fields.py`

- Add columns: `comments`, `supply_chain_position`, `terms`, `modified_at`, `modified_by`.
- All nullable, no data backfill needed.
- Downgrade drops the five columns.

---

## Phase 2: Supplier Edit UI

### 3. ✅ Add HTMX edit form to supplier detail page

File: `templates/partials/supplier_detail.html`

Add an "Edit" button to the details card that swaps the display into an inline edit form. Keep it simple with HTMX `hx-get` to fetch an edit partial and `hx-put` to save.

**Editable fields (Phase 1):**
- `name`, `url`, `address_1`, `city`, `country`, `notes`, `terms`

**Read-only / display-only:**
- `netsuite_id`, `supply_chain_position` (set by AI categorizer), `comments` (added via separate input)

**UI pattern:**
- Click "Edit" → fields become `<input>` / `<textarea>` inline (no modal).
- "Save" sends `PUT /partial/suppliers/{id}` → returns updated detail partial.
- "Cancel" re-fetches the read-only partial.

### 4. ✅ Add edit partial template

~~File: `templates/partials/supplier_edit.html`~~

Implemented as inline Alpine.js toggle in `supplier_detail.html` (matching RFQ pattern) instead of a separate partial.

### 5. ✅ Add comments input to supplier detail

Below the details card, add a "Comments" section:
- Displays existing comments in chronological order (author, text, timestamp).
- A small textarea + "Add Comment" button that POSTs to `/partial/suppliers/{id}/comments`.
- The author is auto-set from the logged-in user session.

### 6. ✅ Display new fields in supplier detail

- **Supply Chain Position**: Shown as badge next to supplier name + in details grid with tier-coloured tag. Editable via single dropdown (tier + category combined). Taxonomy driven from `config/settings.py → SUPPLY_CHAIN_TAXONOMY`.
- **Terms**: Displayed in the Details `<dl>` grid alongside country/city.
- **Comments**: Rendered as a chronological list with inline add form. Separate `supplier_comments.html` partial for HTMX swap.

---

## Phase 3: Dashboard Routes & Database

### 7. ✅ Add edit routes

File: `includes/dashboard/routes.py`

```
POST /partial/suppliers/{id}/update     → save edits, return supplier_detail.html
POST /partial/suppliers/{id}/comments   → append comment, return comments partial
```

### 8. ✅ Add database update function

File: `includes/dashboard/database.py`

```python
def update_supplier(supplier_id: str, updates: dict, modified_by: str) -> Supplier:
    """
    Update supplier fields and set modified_at/modified_by.
    Returns the updated supplier.
    """
```

- Accepts a dict of field→value for the editable fields.
- Sets `modified_at = datetime.now(UTC)` and `modified_by` on every save.
- Validates that only allowed fields are updated (whitelist).

```python
def add_supplier_comment(supplier_id: str, author: str, comment: str) -> list:
    """
    Append a comment to the supplier's comments JSONB list.
    Returns the updated comments list.
    """
```

---

## Phase 4: Wire Categorization Results

### 9. ✅ Update categorization to write to DB

Restructured into three layers:

1. **`includes/supplier_categorization.py`** — Reusable service module with `categorize_supplier()`, `save_categorization_to_db()`, `build_prompt()`, `load_taxonomy()`. Prompt categories/tiers are built dynamically from `config.SUPPLY_CHAIN_TAXONOMY`.

2. **`scripts/categorize_suppliers_job.py`** — Background job that reads suppliers from DB, categorizes, and writes back. Registered in `config/scripts.py` as `categorize_suppliers`. Supports `--force` (re-categorize all), `--limit N`, `--model`, `--delay`, `--dry-run`.

3. **`scripts/categorize_suppliers.py`** — Original CLI script refactored to import from the shared service. Still reads/writes JSON files for R&D use.

File: `scripts/categorize_suppliers.py`

After categorization, write results into `supply_chain_position` JSONB:
```json
{
  "category": "Trade Wholesaler",
  "tier": "B",
  "confidence": 4,
  "reasoning": "Requires trade account, multi-brand, no public pricing."
}
```

Set `modified_by = "ai:categorizer"` and `modified_at` when writing.

### 10. ✅ Display categorization in supplier list table

Combined City/Country into "Location" column and added "Supply Chain" column showing "tier, category".

---

## Implementation Order

| Step | Task | Files |
|------|------|-------|
| 1 | Add model fields | `includes/dashboard/models.py` |
| 2 | Alembic migration | `alembic/versions/` |
| 3-4 | Edit form UI | `templates/partials/supplier_detail.html`, `supplier_edit.html` |
| 5-6 | Display new fields + comments | `templates/partials/supplier_detail.html` |
| 7 | Edit routes | `includes/dashboard/routes.py` |
| 8 | DB update functions | `includes/dashboard/database.py` |
| 9 | Wire categorization script | `scripts/categorize_suppliers.py` |
| 10 | List view category column | `templates/partials/` supplier list |

## Notes

- **No separate `supplier_edits` audit table** — `modified_at`/`modified_by` is sufficient for the planned NetSuite API sync. If we need full edit history later, we can add an audit log table.
- **Comments as JSONB array** — keeps it simple with no join table. If comment volume grows significantly, we can migrate to a separate `supplier_comments` table.
- **`supply_chain_position` as JSONB** — aligns with the categorization script output format and avoids migrations as the taxonomy evolves. We can always add typed columns later if query performance needs it.
- **Field whitelist for edits** — the update function rejects attempts to edit `id`, `netsuite_id`, `embedding` through the UI form. `supply_chain_position` is now editable via the combined tier/category dropdown.
- **Global taxonomy** — `config/settings.py → SUPPLY_CHAIN_TAXONOMY` is the single source of truth for tiers/categories. The dropdown, validation, and categorization script all derive from it. Cross-referenced with `docs/supplier-categorization-taxonomy.md`.
- **Future NetSuite sync**: the sync endpoint will query `WHERE modified_at > last_sync_timestamp` to find locally-changed suppliers.
