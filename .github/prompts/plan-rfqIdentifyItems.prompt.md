# Plan: RFQ Item Classification & Validation

**Created:** 2026-06-24
**Branch:** `rfq-optimisation`
**Status:** 🟢 Phase 1-5 complete | ✏️ Phase 6 planning

---

## Summary

Replace the vague `status` field (`unidentified`/`identified`/`confirmed`/`review`)
with a `match` column that classifies items by data completeness AND flags
discrepancies. The workflow shifts from "identify items" to **classify then
validate**: determine how much data we have for each item, then check for
problems in fully-specified items.

### The `match` Scale

| Value | Color | Meaning |
|---|---|---|
| `unmatched` | ⬜ grey | **Default** — not yet processed by this workflow |
| `specific` | 🟢 green | Has brand + part_number + description — fully specified, can be validated |
| `branded` | 🔵 blue | Has brand + description, no part_number — has a brand anchor |
| `generic` | 🟣 purple | Description only — needs the most interpretation |
| `discrepancy` | 🟠 orange | Validation found a conflict (wrong part_number, brand mismatch, etc.) |

### Three Real-World Scenarios

This maps directly to the three common item types:

| Scenario | Example | `match` value | Can be validated? |
|---|---|---|---|
| 1. Full spec | `8T4223, Caterpillar, Washer` | `specific` | ✅ Yes — check part_number matches brand+desc |
| 2. Branded | `Colman, 20l Esky` | `branded` | ❌ No part_number to check |
| 3. Description only | `5m x 200x50 Steel lengths` | `generic` | ❌ Nothing to validate |

---

## Context

The existing `status` field on `rfq_items` conflates two separate concerns:
1. **Data completeness** — how much identifying info do we have?
2. **Correctness** — is the identifying info accurate?

This causes confusion:
- A brand+description item and a part_number+brand+description item are both
  "unidentified" until processed, but they're fundamentally different
- The Phase 2 web search treats everything as "needs identification" when it
  should be focused on finding discrepancies in specific items
- The "Identify Items" button sends ALL non-confirmed items to Phase 2,
  including branded and generic items that have nothing to validate

---

## Steps

### ✅ Phase 1 — Unify Product DB Lookups (COMPLETE)

Merged `_find_product_exact` and `_find_product_by_supplier_code` into
`_find_product_by_code` (single `or_()` query against `part_number` and
`supplier_code`). Call site in `on_rfq_identify_items` simplified to one
DB roundtrip per item.

**Files changed:**
- `includes/tools/product_tools.py` — replaced two functions with one
- `includes/chat/rfq_actions.py` — simplified import + call site

---

### ✅ Phase 2 — Add `match` Column & Migration (COMPLETE)

**Replace** the `status` column on `rfq_items` with `match`.

1. **Alembic migration**: Rename `status` → `match`, set default `'unmatched'`,
   migrate existing values:
   - `'confirmed'` → `'specific'` (confirmed items had full specs)
   - `'identified'` → `'specific'` (identified items had part numbers matched)
   - `'unidentified'` → `'unmatched'` (unprocessed)
   - `'review'` → `'discrepancy'` (flagged for issues)

2. **Model** (`includes/dashboard/models.py` L304):
   ```python
   # Before
   status = Column(String, nullable=True, default='unidentified')
   # After
   match = Column(String, nullable=True, default='unmatched')
   ```

3. **CRUD** (`includes/tools/rfq_crud.py` L126, 300, 398):
   - `_rfq_to_dict`: `"match": item.match or "unmatched"`
   - `_create_rfq_sync`: default `match="unmatched"`
   - `_add_items_sync`: default `match="unmatched"`

4. **`_update_item_sync`** (`rfq_crud.py` L477):
   - Replace `"status"` in `updatable` list with `"match"`

**Files:**
- `alembic/versions/` — new migration
- `includes/dashboard/models.py`
- `includes/tools/rfq_crud.py`

---

### ✅ Phase 3 — Refactor `on_rfq_identify_items` (COMPLETE)

Rename and restructure the callback to match the new semantics.

**New name:** `on_rfq_classify_validate` (or keep `on_rfq_identify_items` for
action name compatibility — TBD).

**New flow:**

```python
for each item where match == 'unmatched':

    # Step A: CLASSIFY (deterministic, no I/O)
    has_part = bool(part_number)
    has_brand = bool(brand)
    has_desc = bool(description)

    if has_part and has_brand and has_desc:
        match = 'specific'
    elif has_brand and has_desc:
        match = 'branded'
    elif has_desc:
        match = 'generic'
    else:
        # Item has almost no data — leave as unmatched
        continue

    # Write classification immediately
    _update_item_sync(rfq_id, {line, match})

    # Step B: VALIDATE (specific items only)
    if match == 'specific':
        # Phase 1: Internal DB lookup (_find_product_by_code)
        product = await _find_product_by_code(part_number, brand)
        if product:
            _update_item_sync(rfq_id, {line, product_id, ...})
            continue  # Found in DB, done

        # Phase 2: Web search for discrepancy detection
        # Route to ResearchAgent with updated prompt
        # (see Phase 4)
```

**Key changes from current behavior:**
- Items without part numbers get classified but NOT sent to web search
- The classification step runs for ALL unmatched items, not just those with
  part numbers
- `match` is set immediately on classification (deterministic)

**Files:**
- `includes/chat/rfq_actions.py` — `on_rfq_identify_items`

---

### Phase 4 — Rewrite `rfq_identify_items.md` Prompt

Shift the ResearchAgent prompt from "identify items" to **"validate & find
discrepancies"**.

**Current instruction:**
> Identify unidentified product(s) from the RFQ. For each item, search the web
> to verify the part number and find a positive product match.

**New instruction:**
> Validate specific items (those with part_number + brand + description) by
> searching the web. Your primary job is to find DISCREPANCIES:
> - Part number exists but refers to a different product than the description
> - Part number cannot be found online
> - Close matches exist that better fit the description (possible typo)
>
> Set `match='discrepancy'` (not 'review') for problem items with notes
> explaining the issue. Leave correctly matching items as `match='specific'`.
> Do NOT process branded or generic items — they have already been classified.

**File:**
- `config/prompts/rfq_identify_items.md`

---

### Phase 5 — Update UI Templates & JS

Replace all references to `status` with `match`, and update visual indicators
for the new scale.

1. **`templates/partials/rfq_detail.html`**:
   - Line 22: `"unidentified"` list → `"unmatched"` list
     ```jinja2
     "unmatched": items | selectattr('match', 'equalto', 'unmatched') | list
     ```
   - Lines 188, 228-229: Summary counts — replace `unidentified`/`review`/
     `confirmed`/`identified` with `unmatched`/`discrepancy`
   - Button visibility: show when `unmatched or discrepancy` items exist

2. **`templates/partials/_rfq_items_table.html`**:
   - Lines 48-65: Status dot indicators — replace 4-way `if/elif` with 5-way
     `match` check:
     ```jinja2
     {% if item.match == 'specific' %}🟢
     {% elif item.match == 'branded' %}🔵
     {% elif item.match == 'generic' %}🟣
     {% elif item.match == 'discrepancy' %}🟠
     {% else %}⬜{% endif %}
     ```

3. **`templates/base.html`**:
   - Lines 823, 860: JS checks for `item.status === 'confirmed'` →
     `item.match === 'specific'` (for "Find Suppliers" button eligibility)

4. **`templates/partials/_rfq_items_table.html`** L65:
   - "Find Suppliers" button eligibility: `item.match == 'specific'` (was
     `item.status == 'confirmed'`)

---

### Phase 6 — Update Rendering, Quote Tools, Scripts & Tests

1. **`includes/tools/rfq_render.py`** (L22-25, 193):
   - Replace `status` counts with `match` counts
   - Status label map → match label map

2. **`includes/tools/quote_tools.py`** (L465):
   - Update docstring to document `match` values

3. **`includes/dashboard/routes/rfqs.py`** (L930):
   - Default item field: `match="unmatched"` instead of `status="unidentified"`

4. **Scripts**:
   - `scripts/migrate_rfqs_to_sql.py` L110: `match="unmatched"`
   - `scripts/generate_supplier_notes.py` L145-426: replace `status` with `match`
     (note: this script's item status is different from RFQ item status — may
     need careful review)

5. **Tests** (`tests/tools/test_quote_tools.py`):
   - L148: `assert items[0].match == "unmatched"`
   - L165, 173, 204, 675: `"match": "specific"`

---

## Decisions

| Decision | Rationale |
|---|---|
| Replace `status` entirely, don't keep both | `status` was confusing — it mixed workflow state with data completeness. Starting fresh with `match` is cleaner. |
| Default is `unmatched` (grey) | Provides an explicit "not yet processed" signal. Replaces the need for a separate `is_processed` flag. |
| Classification is deterministic (no LLM/DB needed) | Has part_number + brand + description? → `specific`. Has brand + description? → `branded`. Has description? → `generic`. Pure logic. |
| Phase 2 (web search) only runs on `specific` items | Branded and generic items have nothing to validate — there's no part number to look up. Supplier search is a separate step. |
| Primary purpose of Phase 2 shifts to discrepancy detection | The web search validates that the part_number actually matches the claimed brand+description. Mismatches → `discrepancy`. |
| Supplier search is a separate, later step | Classification tells you what you're dealing with. Finding suppliers is an independent action that works on items of any match type. |

---

## Out of Scope (Future Plans)

- Brand+description fuzzy matching against products table for `branded` items
- Description-only semantic search against products table for `generic` items
- Supplier discovery for branded/generic items

---

## Backward Compatibility

### Risk Assessment

The `status` column on `rfq_items` has **no indexes, no foreign keys, no
constraints** — just a plain nullable `String`. An `ALTER TABLE RENAME COLUMN`
is atomic and instant with no data copy.

### Semantic Mapping Problem

The old `status` values don't map cleanly to `match`:

| Old `status` | Problem |
|---|---|
| `confirmed` | Safe → `specific`. Confirmed items had verified part numbers. |
| `identified` | Ambiguous. Could be a part number match, could be description-only. Can't assume `specific`. |
| `unidentified` | Not "has no data" — could have full data but was never clicked. Shouldn't all be `unmatched`. |
| `review` | Human-flagged. Must preserve the flag regardless of data completeness. |

### Migration Strategy

Re-classify from **actual item data** (not old status), with one exception:

```sql
UPDATE rfq_items SET match = CASE
  -- Preserve human review flags
  WHEN status = 'review' THEN 'discrepancy'

  -- Classify fresh from actual data
  WHEN part_number IS NOT NULL AND part_number != ''
       AND brand IS NOT NULL AND brand != ''
       AND input_description IS NOT NULL AND input_description != ''
    THEN 'specific'
  WHEN brand IS NOT NULL AND brand != ''
       AND input_description IS NOT NULL AND input_description != ''
    THEN 'branded'
  WHEN input_description IS NOT NULL AND input_description != ''
    THEN 'generic'
  ELSE 'unmatched'
END
```

This preserves `review` → `discrepancy` (human judgment + notes), and re-derives
everything else from the actual `part_number`/`brand`/`input_description` fields.

### What Won't Break

- Existing RFQ numbers, items, suppliers — untouched
- Chat threads / history — RFQ data re-read from DB each request
- Email tracking — no dependency on item status
- Purchase history — no dependency

### Deployment

Run the migration script during a maintenance window (after-hours, no active
users):

```bash
# Dry-run first
python scripts/migrate_status_to_match.py

# Apply
python scripts/migrate_status_to_match.py --apply
```

Deploy the new application code immediately after. If any in-progress chat
sessions exist, a page refresh picks up the new `match` field.

### Migration Script

See `scripts/migrate_status_to_match.py` — idempotent, dry-run by default,
reports before/after counts.

---

## Verification

### Phase 1 (complete)

1. `grep_search` for `_find_product_exact` and `_find_product_by_supplier_code` — zero matches ✅
2. `uv run pytest tests/ -x --timeout=60 -q --no-header` — all tests pass ✅

### Phase 2 (migration)

3. Run `python scripts/migrate_status_to_match.py` — dry-run output shows correct reclassification counts
4. Run `python scripts/migrate_status_to_match.py --apply` — column renamed, data migrated
5. Query DB: `SELECT match, COUNT(*) FROM rfq_items GROUP BY match` — verify distribution
6. Spot-check: pick a previously `review` item → should show `discrepancy` with notes intact
7. Spot-check: pick a previously `confirmed` item → should show `specific`

### Phase 3–6 (after implementation)

8. New RFQ: add items of each type → verify correct `match` values
9. Existing RFQ: click "Classify & Validate" → `specific` items go to web validation, others just get classified
10. UI: verify colors (green/blue/purple/orange/grey) and button visibility
