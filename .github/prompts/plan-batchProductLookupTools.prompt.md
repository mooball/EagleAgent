# Plan: Multi-Item Product Lookup

## TL;DR
Update the existing `search_products` and `part_purchase_history` tools to accept comma-separated lists of part numbers. Internally, split on commas and build OR'd `ILIKE` conditions in a single SQL query. Single-item calls work identically (no comma = single filter). Update ProcurementAgent prompt with bulk lookup workflow guidance.

## Steps

### Phase 1: Update existing tools
1. **Update `search_products`** in `includes/tools/product_tools.py`
   - `part_number` parameter now accepts comma-separated values (e.g. `"DHP486Z, DTD154Z, BL1850B-LX5"`)
   - Update `_do_product_search`: split `part_number` on commas, build OR'd `ILIKE` conditions
   - Group output by input part number so the user can see which items matched and which didn't
   - Cap at 50 items per call
   - Single-item calls (no comma) behave exactly as before

2. **Update `part_purchase_history`** in same file
   - `part_number` parameter now accepts comma-separated values
   - Update `_do_part_purchase_history`: split on commas, expand `Product.part_number.ilike(...)` filter to OR'd conditions
   - Group results by part number in the output table
   - Cap at 50 items per call
   - Single-item calls behave exactly as before

### Phase 2: Update agent prompt (*depends on Phase 1*)
3. Update ProcurementAgent system prompt in `includes/agents/procurement_agent.py`
   - Update tool descriptions to mention comma-separated support
   - Add "Bulk Lookup Workflow" section: when user provides a list/table of products (from screenshot, pasted text, etc.), pass all part numbers as a comma-separated list in a single tool call
   - Update tool call budget to allow slightly more calls for bulk workflows
4. Bump `recursion_limit` from 12 → 18 to accommodate bulk workflows that may need follow-up calls

### Phase 3: Update tests
5. Add test cases for multi-item `search_products` and `part_purchase_history`

## Relevant files
- `includes/tools/product_tools.py` — update `_do_product_search` and `_do_part_purchase_history`
- `includes/agents/procurement_agent.py` — update `get_system_prompt()`
- `includes/agents/base.py` — `recursion_limit` (line 289)
- `tests/tools/` — new test cases

## Verification
1. `uv run pytest tests/` — all pass (existing + new tests)
2. Manual: screenshot of 30-item list → "find all these products" → 1 tool call with comma-separated part numbers
3. Manual: "find suppliers for all of these" → single `part_purchase_history` call with comma-separated part numbers
4. Manual: single-item lookup still works unchanged

## Decisions
- Comma-separated string (not JSON or list) — simplest for the model to generate and for backwards compatibility
- `ILIKE` partial matching via OR conditions for fuzzy part number matching
- Cap at 50 items per call to prevent runaway queries
- No new tools — existing tools are extended in place
- No semantic/vector search in multi-item mode (part number matching only) — can add later
- Output groups results by input item, clearly indicating FOUND / NOT FOUND for each
