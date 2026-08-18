# Plan: RFQ Item Departments

> Status: **PROPOSAL — review before implementation.**
> Created: 2026-08-18
> Captures work done 2026-08-18 and scopes the remaining phases for approval.

## Goal

Classify every RFQ line item with a **Department** (NetSuite `department`) so
that when items are pushed onto NetSuite Opportunities the line items carry the
correct `department` field. Departments are a small, fixed set — 9 values with
NetSuite internal IDs — and most can be assigned **deterministically** from the
product database; the LLM is only needed as a fallback for items with no product
match.

## Background & findings (2026-08-18)

- **Field location:** `department` exists on the Opportunity header AND on every
  `transactionLine` (verified live: `OP72655` line 1 carries `department: '9'`).
  Our `create_opportunity` payload already supports the header-level field.
- **Items in NetSuite are already classified:** 287,257 of 304,286 items
  (~94.4%) have `item.department` set, spread across all 9 departments.
- **Exact IDs** (fetched live via SuiteQL `SELECT id, name FROM department`):

| NetSuite ID | Department | Items in NetSuite |
|---|---|---|
| 1 | Machine Parts | 110,090 |
| 4 | Engine Parts | 16,392 |
| 5 | Truck Parts | 46,478 |
| 7 | Tyres | 19,265 |
| 8 | Other Parts | 705 |
| 9 | 4WD Parts | 7,451 |
| 10 | Industrial | 81,554 |
| 11 | Forklift Parts | 5,321 |
| 13 | Cosmetic | 1 |

- **Existing scaffolding:** `scripts/sync_netsuite_departments.py` fetches the
  department list (SuiteQL) and can persist it to `system_settings`
  (`"departments"` key). The RFQ CSV export already has an empty `Department`
  column.

## ✅ Phase 0 — Canonical definitions (DONE today)

`includes/netsuite/departments.py`:

- `Department` enum — the 9 departments above, `value` = NetSuite internal ID.
- Properties: `.netsuite_id`, `.label`, `.description`.
- `DEPARTMENT_BY_ID` / `DEPARTMENT_BY_LABEL` lookups.
- `department_prompt_table()` — Markdown table for LLM classification prompts.
- Tests in `tests/test_departments.py` pin the IDs and labels against drift.

**Open question:** the `4WD Parts` description was authored by Copilot (you
forgot it in the original list) — review wording:

> Parts specific to four-wheel-drive vehicles — drivetrain components, transfer
> cases, differentials, suspension, and 4WD-specific accessories.

No DB table — a code-level enum was deliberately chosen (list changes rarely;
descriptions must version-control alongside matching logic).

## ✏️ Phase 1 — Add Department to the NetSuite product sync

1. **Query** — add `i.department` to `products_updated_since()` in
   `includes/netsuite/queries.py` (alongside the existing brand select).
2. **Model + migration** — add `department_id` (String, nullable) to `Product`
   in `includes/dashboard/models.py` + Alembic revision. Store the NetSuite
   string ID directly (matches the `Department` enum values; avoids a join to a
   departments table that doesn't exist).
3. **Sync script** — `scripts/sync_netsuite_products.py` maps
   `row["department"]` → `Product.department_id` in `map_item_to_product` and
   upsert.
4. **Validation on write** — store only if the value is a known `Department`
   (in `DEPARTMENT_BY_ID`); log-and-skip unknown IDs so a future NetSuite
   department addition fails loudly instead of polluting the column.

**Notes / decisions:**
- Current query restricts to active `InvtPart` — non-inventory items stay out of
  scope for now (note it in the docstring).
- ~5.6% of items have no department in NetSuite → NULL locally, fallbacks apply.

## ✏️ Phase 2 — One-off backfill of existing products

1. Extend `scripts/sync_netsuite_products.py` with a backfill mode (e.g.
   `--since 1970-01-01` documented as the full backfill, or an explicit
   `--full` flag). The `ORDER BY lastmodifieddate ASC` + resume pattern already
   supports long runs; confirm pagination (`offset`) handles ~300k rows.
2. Or a dedicated `scripts/backfill_product_departments.py` that walks products
   missing `department_id`, fetches by NetSuite ID in batches, and writes the
   column back. **Decision needed** — inline flag vs separate script.
3. Run once locally (or via Railway job) and verify counts match the live
   distribution table above.

## ✏️ Phase 3 — Department on RFQ item rows (store / edit / view)

1. **Model + migration** — add `department_id` (String, nullable) to `RFQItem`
   in `includes/dashboard/models.py` + Alembic revision.
2. **Dict plumbing** — `_item_to_dict` surfaces `department_id` and a resolved
   `department` label (via the `Department` enum) for display.
3. **View** — item table shows the department (small badge or column; hide when
   empty).
4. **Edit** — per-item edit form gets a Department `<select>` populated from the
   `Department` enum (label + ID); the spreadsheet **Edit All** grid gets a
   Department column with the same select. Follow the Quote Brand pattern:
   server resolves and validates the value against the enum.
5. **CSV export** — fill the existing empty `Department` column with the label.

**Decisions needed:**
- Show department as a new column in the item table, or a badge next to the
  description?
- Any department editable at any time, or only before suppliers are found?

## ✏️ Phase 4 — Agent tools to set / update item departments

1. Extend `manage_rfq` in `includes/tools/quote_tools.py`:
   - `update_item` accepts `department_id` (NetSuite string ID) or `department`
     (exact case-insensitive label, resolved via `Department`).
   - `update_items_bulk` accepts the same per entry.
   - `add_items` optionally accepts it at creation.
2. `_update_item_sync` / `_update_item_core` / `_update_items_bulk_sync` in
   `includes/tools/rfq_crud.py` validate against the enum (same resolve-or-error
   pattern as `quote_brand_id`).
3. `get_rfq` renderers show the department per line so the agent can see and
   verify what is set.
4. Tests mirror the Quote Brand suite: set by ID, set by label, invalid value
   rejected, clear works, bulk path works.

## ✏️ Phase 5 — LLM department classification (no product match)

Precedence, strongest first:

1. **Product match** — when classify/DB-match resolves an item to a `products`
   row, copy `products.department_id` onto the line. No LLM involved.
2. **LLM fallback** — for items with no product match, ask the LLM to pick a
   department from `department_prompt_table()` (descriptions are the decision
   guide). Require the output to be one of the exact labels/IDs; anything else
   is rejected.
3. **Human** — leave `NULL` when the LLM is unsure; never default to `Other
   Parts` ("used sparingly rather than as a default when unsure").

Implementation sketch:

- New prompt file (e.g. `config/prompts/rfq_item_departments.md`) built on
  `department_prompt_table()`.
- A deterministic helper `_set_item_departments_sync(rfq_number, user_id)`:
  - product matches → copy department;
  - remainder without product match → single LLM call, JSON `{line: department}`
    output, validated against the enum.
- Hook into the classify & validate step (both the agent `classify_items` tool
  path and the dashboard `on_rfq_identify_items` action), the same way the
  Quote Brand auto-set is wired, and report results in the tool summary.

**Decisions needed:**
- Should LLM classification run automatically inside classify & validate, or as
  a separate opt-in tool/button?
- Batch all unmatched items in one LLM call (cheaper) vs per item (more
  reliable)? Recommend one call with strict JSON validation.

## Out of scope (until a later phase)

- Pushing `department` to NetSuite Opportunity line items (the field already
  exists there; we're only capturing locally for now).
- Departments on RFQ-level vs line-level rollups.
- Non-InvtPart items in the product sync.

## Definition of done

- [ ] Phase 1–5 implemented with tests; full suite green.
- [ ] Product backfill run; local counts ~match the live distribution.
- [ ] RFQ item UI stores/edits/displays departments; CSV export populated.
- [ ] Agent can set/update departments by ID or label; invalid values rejected.
- [ ] LLM fallback produces only enum-valid assignments; ties/uncertainty leave
      the field empty.
