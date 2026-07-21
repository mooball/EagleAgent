# Plan: RFQ Bulk Operations — Multi-Line Tool Calls

## Implementation Priority

| Priority | Phase | Description | Complexity | Value |
|----------|-------|-------------|------------|-------|
| 1 | 1.2 | `update_items_bulk` | Low | High — fixes classification N+1, enables batch updates |
| 2 | 1.1 | `add_suppliers_bulk` | Medium — requires refactoring `_add_supplier_sync` into session-accepting helper | High |
| 3 | 1.4 | `select_quotes_bulk` | Low — loop of toggles | Medium |
| 4 | 1.3 | `update_quotes_bulk` | Low — simple wrapper | Medium |
| 5 | 3 | Prompt enhancements | Low | Medium — guides LLM to use bulk tools |
| 6 | 4 | Tests | Low | Required |

---

## TL;DR

The RFQ tools currently force the LLM agent to make one call **per line** for most operations (`add_supplier`, `update_item`, `update_quote`, `select_quote`, `decline_quote`). For a 50-line RFQ, adding suppliers = 50 round-trips. This plan adds bulk multi-line variants to eliminate those round-trips.

---

## Problem

| Current Behavior | Pain |
|---|---|
| `add_supplier` takes a single `line` (int) | 50 items → 50 tool calls, each a full LLM→DB→response round-trip |
| `update_item` takes a single `line` (int) | Same — updating qty on 30 items = 30 calls |
| `update_quote` / `select_quote` / `decline_quote` are per-line | Same per-line bottleneck |
| `_classify_rfq_items_sync` calls `_update_item_sync` per item | Backend N+1: each update opens a session, commits, and closes — dozens of DB round-trips during classification |
| Prompts don't emphasize batching | The LLM may default to adding items one at a time out of caution, even though `add_items` already accepts a list |

**Note:** `add_items` already accepts `data["items"]` as a list — the backend batches these in one DB transaction. The issue is that the LLM isn't strongly guided to pass all items in one call.

---

## Phase 1: Bulk Multi-Line Tool Actions

Add new actions to `manage_rfq` that accept arrays of per-line data. Each operates within one DB session + single commit.

### Critical implementation note: Session refactoring

The existing single-line functions (`_add_supplier_sync`, `_select_quote_sync`, etc.) each open and close their own session. The bulk functions **cannot** simply call these in a loop — that would still be N sessions + N commits.

**Required refactoring pattern:**
1. Extract the core logic from each `_*_sync` function into a `_*_core(session, rfq, line_item, data, ...)` helper that accepts an **existing session** and doesn't commit.
2. The existing single-line `_*_sync` function becomes a thin wrapper: opens session → calls `_*_core` → commits.
3. The new bulk `_*_bulk_sync` function: opens session → loops calling `_*_core` → single commit at end.

This ensures:
- Zero behavioral change for existing single-line operations
- Bulk operations are truly batched (one session, one commit)
- No code duplication between single and bulk paths

### 1.1 `add_suppliers_bulk` — Add suppliers to multiple lines

**Tool action:** `manage_rfq(action='add_suppliers_bulk', rfq_id=..., data={...})`

**Data shape (flat — easier for LLM to construct):**
```json
{
  "entries": [
    {"line": 1, "name": "Acme Corp", "supplier_id": "abc-123", "price": 45.50, "currency": "AUD"},
    {"line": 1, "name": "Global Parts Ltd"},
    {"line": 3, "name": "WidgetCo", "supplier_id": "def-456"}
  ]
}
```

The backend groups by line internally. This flat shape is easier for the LLM to construct than deeply nested `lines → [{line, suppliers: [...]}]`.

**Backend:** `_add_suppliers_bulk_sync(rfq_number, data, user_id)` → groups entries by line → for each line calls `_add_supplier_core(session, rfq, line_item, suppliers_data)`. Single commit at end.

**Behavior:**
- Each line's suppliers are processed independently (a failure on line 3 doesn't roll back line 1)
- Returns a summary: `"Added 5 suppliers across 3 lines. Lines: 1 (+2 suppliers), 3 (+2 suppliers), 7 (+1 supplier). Skipped: line 5 (not found)."`
- Auto-advances RFQ status `draft → in_progress` if any suppliers were added
- Invalidates `item_groups` if needed
- **Cap:** Max 200 entries per call (aligned with Smart Item Adder's bulk add cap)

### 1.2 `update_items_bulk` — Update multiple line items

**Tool action:** `manage_rfq(action='update_items_bulk', rfq_id=..., data={...})`

**Data shape:**
```json
{
  "items": [
    {"line": 1, "quantity": 10, "uom": "box"},
    {"line": 2, "part_number": "DHP486Z", "brand": "Makita"},
    {"line": 5, "input_description": "Heavy duty cable 10mm", "match": "generic"}
  ]
}
```

**Backend:** `_update_items_bulk_sync(rfq_number, data, user_id)` → one session, iterates `data["items"]`, applies each update to the matching `RFQItem` row. Single commit. Resets `match` to `unmatched` when description/part_number/brand changes (same as single `update_item`).

**Behavior:**
- Validates each line exists; reports missing lines without failing the whole batch
- Returns summary: `"Updated 3 items. Lines: 1 (qty→10, uom→box), 2 (part_number→DHP486Z, brand→Makita), 5 (description updated)."`
- **Also used internally:** `_classify_rfq_items_sync` should be updated to call this (or the core helper) instead of looping `_update_item_sync`, eliminating the N+1 pattern.

### 1.3 `update_quotes_bulk` — Update quotation fields across lines

**Tool action:** `manage_rfq(action='update_quotes_bulk', rfq_id=..., data={...})`

**Data shape:**
```json
{
  "quotes": [
    {"line": 1, "name": "Acme Corp", "quote_cost": 45.50, "quote_status": "quoted", "quote_currency": "AUD", "quote_leadtime": "2 weeks"},
    {"line": 3, "name": "WidgetCo", "quote_status": "declined"}
  ]
}
```

**Backend:** `_update_quotes_bulk_sync(rfq_number, data, user_id)` → one session, for each `{line, name, ...}`, finds the supplier on that line and updates quote fields. Single commit.

### 1.4 `select_quotes_bulk` — Select suppliers across multiple lines

**Tool action:** `manage_rfq(action='select_quotes_bulk', rfq_id=..., data={...})`

**Data shape:**
```json
{
  "selections": [
    {"line": 1, "name": "Acme Corp"},
    {"line": 2, "name": "Acme Corp"},
    {"line": 3, "name": "WidgetCo"}
  ]
}
```

**Backend:** `_select_quotes_bulk_sync(...)` → same logic as single `select_quote` but across lines. Copies `quote_cost` to `cost_price` per line. Single commit.

### 1.5 Register new actions in `manage_rfq`

In `includes/tools/quote_tools.py`, add to the `_ACTION_MAP` dict and the docstring:

```python
"add_suppliers_bulk": lambda: asyncio.to_thread(_add_suppliers_bulk_sync, rfq_id, data, user_id),
"update_items_bulk": lambda: asyncio.to_thread(_update_items_bulk_sync, rfq_id, data, user_id),
"update_quotes_bulk": lambda: asyncio.to_thread(_update_quotes_bulk_sync, rfq_id, data, user_id),
"select_quotes_bulk": lambda: asyncio.to_thread(_select_quotes_bulk_sync, rfq_id, data, user_id),
```

Update the docstring with the new actions and their data shapes.

---

## Phase 2: Server-Side CSV Import Tool (DEFERRED)

> **Status: Deferred.** The Smart Item Adder dashboard modal already handles CSV/file import via `parse_text_table` + LLM fallback + user preview + `/api/rfq/{id}/items/bulk`. A separate chat-agent tool is not needed until we identify a use case the dashboard modal doesn't cover.
>
> If implemented in future, it should reuse `rfq_item_import.py` (the shared module with strict `auto_detect_columns` + `_all_columns_mapped` gate + LLM fallback) rather than implementing its own column detection.

---

## Phase 3: Prompt Enhancements

### 3.1 Update `manage_rfq` docstring

Add the new bulk actions to the docstring and emphasize batching for existing `add_items`:

```
add_items          — Add multiple line items to an existing RFQ. data keys:
                     items (required, list of dicts). ALWAYS pass ALL items
                     in one call — never call this once per item.
add_suppliers_bulk — Add suppliers to multiple lines at once. data keys:
                     entries (list of {line, name, ...}). Use instead of
                     calling add_supplier per line.
update_items_bulk  — Update fields on multiple line items. data keys:
                     items (list of {line, field: value, ...}).
update_quotes_bulk — Update quote fields across lines. data keys:
                     quotes (list of {line, name, field: value, ...}).
select_quotes_bulk — Select suppliers across multiple lines. data keys:
                     selections (list of {line, name}).
```

### 3.2 Update `procurement_agent.md`

Add a "Bulk Operations" section to the ProcurementAgent prompt:

```markdown
## Bulk Operations — ALWAYS batch when possible

When working with RFQs that have multiple items, use bulk tools to minimize
tool calls:

| Instead of... | Use... |
|---|---|
| Calling `add_supplier` once per line | `add_suppliers_bulk` — one call for all lines |
| Calling `update_item` once per line | `update_items_bulk` — one call for all items |
| Calling `update_quote` once per line | `update_quotes_bulk` — one call for all quotes |
| Calling `add_items` once per item | Pass the FULL items list in one `add_items` call |

**Rule:** If you find yourself about to make the same tool call more than twice
with different line numbers, use the bulk variant instead.
```

### 3.3 Update internal pipeline stages

In `includes/tools/rfq_crud.py`, update `_classify_rfq_items_sync` to use the bulk update helper instead of looping `_update_item_sync` per item. This eliminates the backend N+1 problem.

---

## Phase 4: Tests

### 4.1 Unit tests for bulk CRUD functions

In `tests/tools/test_rfq_bulk.py` (new file):

- `test_add_suppliers_bulk_multiple_lines` — verify suppliers added to lines 1, 3, 5
- `test_add_suppliers_bulk_missing_line` — verify partial success, missing line reported
- `test_add_suppliers_bulk_empty_list` — verify error for empty `entries`
- `test_add_suppliers_bulk_cap_exceeded` — verify error when > 200 entries
- `test_update_items_bulk` — verify multiple items updated in one call
- `test_update_items_bulk_match_reset` — verify match resets to `unmatched` when description changes
- `test_update_quotes_bulk` — verify quote fields updated across lines
- `test_select_quotes_bulk` — verify selection + cost_price copy across lines
- `test_select_quotes_bulk_deselect_previous` — verify previous selection cleared

### 4.2 Integration test

- `test_bulk_workflow_end_to_end` — create RFQ → add 50 items → add suppliers in bulk → select quotes in bulk → verify all data in DB

---

## Relevant Files

| File | Change |
|---|---|
| `includes/tools/rfq_crud.py` | Refactor single-line functions into `_*_core` helpers + add `_add_suppliers_bulk_sync`, `_update_items_bulk_sync`, `_update_quotes_bulk_sync`, `_select_quotes_bulk_sync`. Update `_classify_rfq_items_sync` to use bulk helper. |
| `includes/tools/quote_tools.py` | Add 4 bulk actions to `_ACTION_MAP`, update docstring |
| `config/prompts/procurement_agent.md` | Add "Bulk Operations" section |
| `tests/tools/test_rfq_bulk.py` | **New file** — all bulk operation test cases |

---

## Verification

1. `uv run pytest tests/` — all existing + new tests pass
2. **Manual: Bulk supplier add** — after pipeline runs, agent adds suppliers to 30 lines with 1 `add_suppliers_bulk` call instead of 30 `add_supplier` calls
3. **Manual: Small RFQ still works** — single-line `add_supplier` and `update_item` still work unchanged for small RFQs
4. **Manual: Classification performance** — classify a 50-item RFQ; verify single DB commit instead of 50 individual commits

---

## Decisions

- **Flat data shape for `add_suppliers_bulk`** — Using `entries: [{line, name, ...}]` (flat) instead of `lines: [{line, suppliers: [...]}]` (nested). Flat is easier for the LLM to construct and less error-prone. Backend groups by line internally.
- **Session refactoring approach** — Extract `_*_core` helpers from existing single-line functions rather than duplicating logic. Keeps single-line and bulk paths in sync.
- **Partial failure model** — Bulk operations report which lines succeeded/failed but don't roll back the whole batch. This matches user expectation: "fix the 3 that failed, don't make me redo all 50."
- **Keep existing single-line actions** — They remain for small operations. The bulk variants are additive, not replacements.
- **CSV import deferred** — The Smart Item Adder dashboard modal already covers CSV/file import with preview. A separate chat-agent tool isn't needed now.
- **Max items per bulk call: 200** — Aligned with the Smart Item Adder's `/items/bulk` cap for consistency.
- **Fix N+1 in classification** — `_classify_rfq_items_sync` will use the new bulk update helper, eliminating per-item session/commit overhead.
