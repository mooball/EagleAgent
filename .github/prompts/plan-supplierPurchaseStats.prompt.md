# Supplier Lookup: Purchase History Integration & Workflow Enforcement

Improve the procurement workflow so that supplier lookups always prioritise purchase history, and enrich supplier search results with purchase stats.

## Phase 1 — Enforce "Purchase History First" Workflow

### 1. Update ProcurementAgent system prompt with supplier-finding sequence

- In `includes/agents/procurement_agent.py`, update `get_system_prompt()` to add an explicit **Supplier Finding Workflow** section that instructs the agent:
  1. When the user asks "who can supply product X?" or similar, first call `search_products` to identify the product and its brand.
  2. Then call `part_purchase_history(part_number)` to find suppliers with actual purchase records.
  3. **Only if** `part_purchase_history` returns no results (no purchase history), fall back to `search_suppliers(brand=...)` to find suppliers linked to that brand.
  4. If both return results, present the purchase history results first (proven suppliers), then mention the brand-linked suppliers as alternatives.
- Keep the existing general tool descriptions intact — this is an additional workflow directive, not a replacement.

### 2. Fix `part_purchase_history` sort order

- In `includes/tools/product_tools.py`, in `_do_part_purchase_history()`, change `.order_by(desc('total_quantity'))` to `.order_by(desc('order_count'))` so results are sorted by **number of purchases** (most frequently used supplier first), not total quantity.
- Update the output header text from "sorted by total quantity supplied" to "sorted by number of purchases".

## Phase 2 — Enrich Supplier Search with Purchase Stats

### 3. Add purchase stats subquery to `_do_supplier_search()`

- In `includes/tools/product_tools.py`, after collecting `supplier_ids` from results (~line 296), add a query against `ProductSupplier` grouped by `supplier_id` to fetch:
  - `func.count(ProductSupplier.id)` → number of purchases
  - `func.max(ProductSupplier.date)` → date of last purchase
- When the `brand` parameter is provided, join `ProductSupplier → Product` and filter `Product.brand.ilike(...)` so that stats are scoped to that brand only.
- When `brand` is not provided, aggregate across all products for each supplier.
- Build a lookup dict: `supplier_purchase_stats = {supplier_id: (count, last_date)}`.

### 4. Include purchase stats in supplier search output formatting

- In the output formatting loop (~line 310), append purchase stats to each supplier's output line:
  - If purchases exist: `| Purchases: 27 | Last Purchase: 2025-11-15`
  - If no purchases: `| Purchases: 0`
- Suppliers with no rows in the lookup dict should display `Purchases: 0` (LEFT JOIN semantics handled by dict `.get()` with a default).

## Phase 3 — Validation

### 5. Run existing tests

- Run `uv run pytest tests/ -v` to confirm no regressions.
- Pay attention to any tests that mock or assert on `_do_supplier_search` or `_do_part_purchase_history` output format.

### 6. Manual verification

- Start the app and ask "what supplier can supply product [known part]?" and confirm:
  - The agent calls `part_purchase_history` first.
  - Results are sorted by number of purchases.
  - If no purchase history, the agent falls back to `search_suppliers(brand=...)`.
- Search suppliers by brand and confirm purchase stats columns appear.
- Verify suppliers with no purchase history still appear with `Purchases: 0`.

## Scope Notes

**Files to modify:**
- `includes/agents/procurement_agent.py` — system prompt workflow directive (Phase 1)
- `includes/tools/product_tools.py` — sort order fix in `_do_part_purchase_history`, purchase stats in `_do_supplier_search` (Phases 1–2)

**No changes needed to:**
- Database models or migrations (no schema changes)
- Tool function signatures, parameters, or docstrings
- Other tools (`search_purchase_history` already covers general history queries)
- `includes/prompts.py` — the `find_product_supplier` intent context already says to prioritise purchase history; the enforcement moves into the agent's own system prompt where it's actually effective
