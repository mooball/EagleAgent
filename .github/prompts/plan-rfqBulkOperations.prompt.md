# Plan: RFQ Bulk Operations — Multi-Line Tool Calls & CSV Import

## TL;DR

The RFQ tools currently force the LLM agent to make one call **per line** for most operations (`add_supplier`, `update_item`, `update_quote`, `select_quote`, `decline_quote`). For a 50-line RFQ, adding suppliers = 50 round-trips. This plan adds bulk multi-line variants to eliminate those round-trips, plus a server-side CSV import tool that removes the LLM from the parsing loop entirely for spreadsheet/image uploads.

---

## Problem

| Current Behavior | Pain |
|---|---|
| `add_supplier` takes a single `line` (int) | 50 items → 50 tool calls, each a full LLM→DB→response round-trip |
| `update_item` takes a single `line` (int) | Same — updating qty on 30 items = 30 calls |
| `update_quote` / `select_quote` / `decline_quote` are per-line | Same per-line bottleneck |
| CSVs/spreadsheets are converted to plain text and fed to the LLM | The LLM must parse, hold all items in context, and format a JSON array. Error-prone at scale (200+ lines) |
| Prompts don't emphasize batching | The LLM may default to adding items one at a time out of caution, even though `add_items` already accepts a list |

**Note:** `add_items` already accepts `data["items"]` as a list — the backend batches these in one DB transaction. The issue is that the LLM isn't strongly guided to pass all items in one call.

---

## Phase 1: Bulk Multi-Line Tool Actions

Add new actions to `manage_rfq` that accept arrays of per-line data. Each reuses the existing single-line logic internally (looped), all within one DB transaction.

### 1.1 `add_suppliers_bulk` — Add suppliers to multiple lines

**Tool action:** `manage_rfq(action='add_suppliers_bulk', rfq_id=..., data={...})`

**Data shape:**
```json
{
  "lines": [
    {
      "line": 1,
      "suppliers": [
        {"name": "Acme Corp", "supplier_id": "abc-123", "contacts": [{"url": "https://..."}], "price": 45.50, "price_type": "estimated", "currency": "AUD"},
        {"name": "Global Parts Ltd", "contacts": [{"url": "https://..."}]}
      ]
    },
    {
      "line": 3,
      "suppliers": [
        {"name": "WidgetCo", "supplier_id": "def-456", "contacts": [{"url": "https://..."}]}
      ]
    }
  ]
}
```

**Backend:** `_add_suppliers_bulk_sync(rfq_number, data, user_id)` → iterates `data["lines"]`, for each calls the core logic from `_add_supplier_sync` (match-to-DB, enrich pricing, deduplicate, update JSONB). Single commit at end.

**Behavior:**
- Each line's suppliers are processed independently (a failure on line 3 doesn't roll back line 1)
- Returns a summary: `"Added 5 suppliers across 3 lines. Lines: 1 (+2 suppliers), 3 (+2 suppliers), 7 (+1 supplier). Skipped: line 5 (not found)."`
- Auto-advances RFQ status `draft → in_progress` if any suppliers were added
- Invalidates `item_groups` if needed

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

**Backend:** `_update_items_bulk_sync(rfq_number, data, user_id)` → iterates `data["items"]`, applies each update to the matching `RFQItem` row. Single commit. Resets `match` to `unmatched` when description/part_number/brand changes (same as single `update_item`).

**Behavior:**
- Validates each line exists; reports missing lines without failing the whole batch
- Returns summary: `"Updated 3 items. Lines: 1 (qty→10, uom→box), 2 (part_number→DHP486Z, brand→Makita), 5 (description updated)."`

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

**Backend:** `_update_quotes_bulk_sync(rfq_number, data, user_id)` → for each `{line, name, ...}`, finds the supplier on that line and updates quote fields. Single commit.

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

### 1.5 Backend implementation approach

All bulk functions go in `includes/tools/rfq_crud.py`, following the existing `_*_sync` pattern:

```python
def _add_suppliers_bulk_sync(rfq_number: str, data: dict, user_id: str) -> dict | str:
    """Add suppliers to multiple line items. Returns RFQ dict or error."""
    lines_data = data.get("lines", [])
    if not lines_data:
        return "Error: 'lines' list is required."

    session = _get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()
        if not rfq:
            return f"Error: RFQ '{rfq_number}' not found."

        results = []
        added_any = False
        for entry in lines_data:
            line_num = entry.get("line")
            # ... reuse core logic from _add_supplier_sync per line ...
            # ... track results ...
            if added_names:
                added_any = True

        # Auto-progress status
        if added_any and rfq.status == "draft":
            rfq.status = "in_progress"

        # History entry
        history = list(rfq.history or [])
        history.append({
            "date": _now_iso(), "user": user_id,
            "action": f"Bulk add suppliers: {len(lines_data)} lines processed. " + "; ".join(results),
        })
        rfq.history = history
        rfq.updated_at = _now_dt()
        session.commit()
        session.refresh(rfq)
        return _rfq_to_dict(rfq)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

Each bulk function should:
- Extract and validate the list parameter
- Loop over entries, applying existing single-line logic (refactored into shared helpers where DRY)
- Collect per-line results for the summary
- Single `session.commit()` at the end
- Append a single history entry describing the bulk operation

### 1.6 Register new actions in `manage_rfq`

In `includes/tools/quote_tools.py`, add to the `_ACTION_MAP` dict and the docstring:

```python
"add_suppliers_bulk": lambda: asyncio.to_thread(_add_suppliers_bulk_sync, rfq_id, data, user_id),
"update_items_bulk": lambda: asyncio.to_thread(_update_items_bulk_sync, rfq_id, data, user_id),
"update_quotes_bulk": lambda: asyncio.to_thread(_update_quotes_bulk_sync, rfq_id, data, user_id),
"select_quotes_bulk": lambda: asyncio.to_thread(_select_quotes_bulk_sync, rfq_id, data, user_id),
```

Update the docstring with the new actions and their data shapes.

---

## Phase 2: Server-Side CSV Import Tool

### 2.1 New tool: `import_rfq_items`

A standalone tool (not an action on `manage_rfq`) that takes raw CSV text and parses it server-side, removing the LLM from the parsing loop.

**Location:** `includes/tools/quote_tools.py` (registered alongside the existing tools)

**Signature:**
```python
@tool
async def import_rfq_items(
    rfq_id: str,
    csv_text: str,
    has_header: bool = True,
    column_mapping: Optional[dict] = None,
) -> str:
```

**Parameters:**
- `rfq_id` — Target RFQ identifier
- `csv_text` — Raw CSV content as a string
- `has_header` — Whether the first row is a header (default true)
- `column_mapping` — Optional dict mapping CSV column names to RFQ item fields, e.g. `{"Description": "input_description", "Part #": "part_number", "Qty": "quantity"}`. If omitted, auto-detects from common header names.

**Auto-detected column names (case-insensitive):**
| CSV Header | Maps To |
|---|---|
| `description`, `item`, `name`, `product` | `input_description` |
| `part_number`, `part #`, `part no`, `partno`, `mpn`, `sku`, `code` | `input_code` |
| `brand`, `make`, `manufacturer` | `brand` |
| `quantity`, `qty`, `qty req'd`, `count` | `quantity` |
| `uom`, `unit`, `unit of measure` | `uom` |
| `notes`, `comment` | `notes` |

**Behavior:**
1. Parse CSV with Python's `csv` module
2. If `has_header`, use first row as header; otherwise generate `col_0, col_1, ...`
3. Map columns using `column_mapping` or auto-detection
4. Validate each row — at minimum, `input_description` must be non-empty
5. Build the `items` list and delegate to `_add_items_sync`
6. Return a summary:

```
✅ Imported 147 items into RFQ-2026-0042 (lines 12–158).

Column mapping used:
  "Description" → input_description
  "Part Number" → part_number
  "Brand"       → brand
  "Qty"         → quantity
  "Unit"        → uom

2 rows skipped:
  Row 45: missing description
  Row 89: quantity "N/A" is not a number

Shall I classify these items?
```

**Implementation:**
```python
# includes/tools/rfq_crud.py

def _import_csv_items_sync(rfq_number: str, csv_text: str, has_header: bool,
                           column_mapping: dict | None, user_id: str) -> str:
    """Parse CSV and add items to RFQ. Returns summary string."""
    import csv, io
    
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return "Error: CSV is empty."
    
    # Header detection & column mapping
    if has_header:
        headers = [h.strip().lower() for h in rows[0]]
        data_rows = rows[1:]
    else:
        headers = [f"col_{i}" for i in range(len(rows[0]))]
        data_rows = rows
    
    # Auto-detect or use provided mapping
    mapping = _auto_detect_columns(headers) if not column_mapping else column_mapping
    
    # Parse items
    items = []
    skipped = []
    for i, row in enumerate(data_rows, start=1):
        item = {}
        for csv_col, field in mapping.items():
            col_idx = headers.index(csv_col.lower()) if has_header else int(csv_col.replace("col_", ""))
            if col_idx < len(row):
                item[field] = row[col_idx].strip()
        
        if not item.get("input_description"):
            skipped.append(f"Row {i + (1 if has_header else 0)}: missing description")
            continue
        
        # Convert quantity to int
        if "quantity" in item:
            try:
                item["quantity"] = int(item["quantity"])
            except (ValueError, TypeError):
                skipped.append(f"Row {i + (1 if has_header else 0)}: quantity '{item.get('quantity')}' is not a number")
                continue
        
        items.append(item)
    
    if not items:
        return f"Error: no valid items found. {len(skipped)} rows skipped.\n" + "\n".join(skipped)
    
    # Delegate to existing batch add
    result = _add_items_sync(rfq_number, {"items": items}, user_id)
    if isinstance(result, str):
        return result
    
    summary = f"✅ Imported {len(items)} items into {rfq_number} (lines ...).\n"
    if skipped:
        summary += f"\n{len(skipped)} rows skipped:\n" + "\n".join(skipped)
    return summary


# Column auto-detection table
_COLUMN_PATTERNS = {
    "input_description": ["description", "item", "name", "product", "desc", "part name"],
    "input_code": ["part_number", "part #", "part no", "partno", "mpn", "sku", "code", "item code", "product code"],
    "brand": ["brand", "make", "manufacturer", "mfr"],
    "quantity": ["quantity", "qty", "qty req'd", "count", "qty required"],
    "uom": ["uom", "unit", "unit of measure", "measure"],
    "notes": ["notes", "comment", "remarks"],
}

def _auto_detect_columns(headers: list[str]) -> dict[str, str]:
    """Map CSV column names to RFQ item fields."""
    mapping = {}
    for h in headers:
        h_lower = h.strip().lower()
        for field, patterns in _COLUMN_PATTERNS.items():
            if h_lower in patterns:
                mapping[h.strip()] = field
                break
    return mapping
```

### 2.2 Register `import_rfq_items` as a tool

In `includes/tools/quote_tools.py`, add `import_rfq_items` to the list returned by `create_quote_tools()`:

```python
@tool
async def import_rfq_items(
    rfq_id: str,
    csv_text: str,
    has_header: bool = True,
    column_mapping: Optional[dict] = None,
) -> str:
    """Import line items from CSV text into an RFQ.

    Parses CSV server-side — faster and more reliable than LLM parsing
    for large spreadsheets. Use this when the user uploads a CSV, Excel
    file, or pastes tabular data.

    Args:
        rfq_id: Target RFQ identifier (e.g. 'RFQ-2026-0042')
        csv_text: Raw CSV content as a string
        has_header: Whether first row contains column names (default True)
        column_mapping: Optional manual column mapping, e.g.
            {"Description": "input_description", "Qty": "quantity"}
            If omitted, auto-detects from common header names.

    Returns:
        Summary with count of imported items and any skipped rows.
    """
    return await asyncio.to_thread(
        _import_csv_items_sync, rfq_id, csv_text, has_header, column_mapping, user_id
    )
```

---

## Phase 3: Prompt Enhancements

### 3.1 Update `rfq_workflow.md` Step 1

Replace the current terse instruction with explicit batching guidance:

```markdown
### Step 1: Create / Add Items

**CRITICAL — Always batch:** Pass ALL items in a SINGLE tool call. Never add items
one at a time.

- For pasted text or image uploads: extract every item, then call
  `manage_rfq(action='add_items', rfq_id='...', data={'items': [...]})`
  with the COMPLETE list.
- For CSV or spreadsheet uploads: pass the raw CSV text to
  `import_rfq_items(rfq_id='...', csv_text='...')` — this parses server-side
  and is much faster for large files.
- After adding, STOP and confirm: "Added N items (lines X–Y). Shall I classify them?"
```

### 3.2 Update `manage_rfq` docstring

Add the new bulk actions to the docstring and emphasize batching for existing `add_items`:

```
add_items     — Add multiple line items to an existing RFQ. data keys:
                items (required, list of dicts). ALWAYS pass ALL items
                in one call — never call this once per item.
```

### 3.3 Update `procurement_agent.md`

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
| Extracting items from a CSV manually | `import_rfq_items` — parse server-side |
| Calling `add_items` once per item | Pass the FULL items list in one `add_items` call |

**Rule:** If you find yourself about to make the same tool call more than twice
with different line numbers, use the bulk variant instead.
```

### 3.4 Update pipeline stages to use bulk tools

In `includes/agents/procurement_agent.py`, update the pipeline stages (`_stage_suppliers_internal`, etc.) to emit bulk tool calls where they currently loop per-item.

---

## Phase 4: Tests

### 4.1 Unit tests for bulk CRUD functions

In `tests/tools/test_rfq_crud.py` (or new file):

- `test_add_suppliers_bulk_multiple_lines` — verify suppliers added to lines 1, 3, 5
- `test_add_suppliers_bulk_missing_line` — verify partial success, missing line reported
- `test_add_suppliers_bulk_empty_list` — verify error for empty `lines`
- `test_update_items_bulk` — verify multiple items updated in one call
- `test_update_items_bulk_match_reset` — verify match resets to `unmatched` when description changes
- `test_update_quotes_bulk` — verify quote fields updated across lines
- `test_select_quotes_bulk` — verify selection + cost_price copy across lines

### 4.2 Unit tests for CSV import

- `test_import_csv_with_header` — standard CSV with header row
- `test_import_csv_no_header` — CSV without header, auto-generates col_0, col_1
- `test_import_csv_auto_detect_columns` — verify auto-mapping works
- `test_import_csv_custom_mapping` — verify manual `column_mapping` overrides auto-detect
- `test_import_csv_missing_description` — verify rows skipped
- `test_import_csv_bad_quantity` — verify non-numeric quantity skipped
- `test_import_csv_empty` — verify error on empty CSV

### 4.3 Integration test

- `test_bulk_workflow_end_to_end` — create RFQ → import 50 items via CSV → add suppliers in bulk → verify all data in DB

---

## Relevant Files

| File | Change |
|---|---|
| `includes/tools/rfq_crud.py` | Add `_add_suppliers_bulk_sync`, `_update_items_bulk_sync`, `_update_quotes_bulk_sync`, `_select_quotes_bulk_sync`, `_import_csv_items_sync`, `_auto_detect_columns` |
| `includes/tools/quote_tools.py` | Add 4 bulk actions to `_ACTION_MAP`, update docstring, register `import_rfq_items` tool |
| `config/prompts/rfq_workflow.md` | Update Step 1 with batching + CSV import instructions |
| `config/prompts/procurement_agent.md` | Add "Bulk Operations" section |
| `includes/agents/procurement_agent.py` | Update pipeline stages to use bulk tool calls where applicable |
| `tests/tools/test_rfq_crud.py` | Add bulk + CSV import test cases |

---

## Verification

1. `uv run pytest tests/` — all existing + new tests pass
2. **Manual: Large CSV upload** — upload a 100-line CSV → agent calls `import_rfq_items` (1 call) → all 100 items appear in RFQ
3. **Manual: Bulk supplier add** — after pipeline runs, agent adds suppliers to 30 lines with 1 `add_suppliers_bulk` call instead of 30 `add_supplier` calls
4. **Manual: Small RFQ still works** — single-line `add_supplier` and `update_item` still work unchanged for small RFQs
5. **Manual: Image upload** — photo of a parts catalog → agent extracts items → 1 `add_items` call with full list

---

## Decisions

- **New actions on `manage_rfq` vs. separate tools** — New actions keep everything in one tool the agent already knows. Separate `import_rfq_items` gets its own tool because it has a fundamentally different interface (raw CSV text, not structured data).
- **Partial failure model** — Bulk operations report which lines succeeded/failed but don't roll back the whole batch. This matches user expectation: "fix the 3 that failed, don't make me redo all 50."
- **CSV auto-detection scope** — Auto-detect covers common English headers only. Non-standard headers require `column_mapping`. This keeps the implementation simple and predictable.
- **Keep existing single-line actions** — They remain for small operations. The bulk variants are additive, not replacements.
- **CSV only, not XLSX** — Spreadsheets (.xlsx, .xls) are already converted to CSV text by `document_processing.py` before reaching the agent. The `import_rfq_items` tool only needs to handle CSV text.
- **Max items per bulk call** — Cap at 500 items/lines per call to prevent timeout on enormous uploads. If exceeded, the tool returns an error asking the user to split the file.
