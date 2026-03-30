# Supplier Lookup: Purchase History Integration & Workflow Enforcement

Improve the procurement workflow so that supplier lookups always prioritise purchase history, enrich supplier search results with purchase stats, and fix Supervisor routing so procurement intents actually reach the ProcurementAgent.

## Phase 1 — Fix Supervisor Routing for Procurement Intents ✅

**Problem:** When a user clicks a procurement action button (e.g. "Find a Brand Supplier") and then sends a message like "who can supply hilti products", the Supervisor ignores the `intent_context` stored in graph state and routes purely on LLM judgement of the message text. The LLM often picks GeneralAgent (which returns accurate but external web search results via Google Search grounding) instead of ProcurementAgent. The user gets good public information about suppliers, but not the internal database results they wanted.

### ~~1. Make Supervisor honour `intent_context` for routing~~ ✅

- ~~In `includes/agents/supervisor.py`, at the top of `__call__()` (after the AI-message early return), read `state.get("intent_context")`.~~
- ~~If `intent_context` is set and contains procurement-related keywords (e.g. references to `search_products`, `search_suppliers`, `search_brands`, `part_purchase_history`, `search_purchase_history`), route directly to `ProcurementAgent` — skip LLM routing.~~
- ~~Clear or consume the intent context after use (set `intent_context` to `None` in the returned state) so it doesn't keep re-routing on subsequent turns in the same thread.~~
- ~~Log the intent-based routing decision for debugging.~~
- Implementation note: Added intent-based routing block after AI-message early return. Checks for 5 procurement tool name signals in intent_context. Returns `{"next_agent": "ProcurementAgent", "intent_context": None}` to route and clear in one step.

### ~~2. Inject `intent_context` into the routed agent's messages~~ ✅

- ~~When `intent_context` is set, prepend it to the messages passed to the agent as a `SystemMessage` so the agent knows the specific workflow context (e.g. "The user wants to find suppliers who carry a specific brand. First use `search_brands`...").~~
- ~~This should happen in the Supervisor before handing off, or in `BaseSubAgent.__call__()` by reading it from state.~~
- ~~This ensures the agent follows the intent's prescribed tool sequence rather than making its own judgement.~~
- Implementation note: Already implemented in `BaseSubAgent.__call__()` — it reads `state.get("intent_context")` and appends it to the system prompt as `**Current user intent:** {context}`. No changes needed.

### ~~3. Add procurement keywords to Supervisor rule-based routing~~ DISCARDED

- ~~The `procurement_keywords` list in `supervisor.py` is currently empty. Populate it with terms that clearly indicate internal database queries: `"supplier"`, `"suppliers"`, `"purchase history"`, `"part number"`, `"who can supply"`, `"purchase order"`, `"brand supplier"`.~~
- ~~This provides a fast-path for procurement queries even without an intent button click.~~
- ~~Keep the list conservative — only unambiguous procurement terms. The LLM fallback handles ambiguous cases.~~
- Reason: Keyword routing is too brittle — e.g. "search the web for a supplier of X" would falsely route to ProcurementAgent. Replaced with improved LLM routing prompt that defaults to ProcurementAgent for supplier/product questions unless the user explicitly asks for web/external info. Also removed the pre-existing empty keyword lists and dead code. Tests updated from keyword-based assertions to LLM-mocked routing + new intent-based routing tests (12 passing).

### ~~3a. Notify user on transient API retries~~ ✅

- ~~When a transient API error (429/503) triggers a retry in `BaseSubAgent.__call__()`, send a Chainlit message to the user so they know the system is pausing, not frozen.~~
- ~~Added `_notify_retry()` helper in `includes/agents/base.py` that sends "⏳ LLM temporarily unavailable — retrying (1/3)..." via `cl.Message`.~~
- ~~Wired into both retry loops (tool-based and tool-less model invocation).~~
- ~~Wrapped in try/except so notification failures never break the retry loop.~~
- Implementation note: Note that the Google SDK also has its own internal retries (visible in logs as `google_genai._api_client` retries at 1.3s, 2.49s etc.) which happen before our retry logic. This notification covers our application-level retries only.

## Phase 2 — Enforce "Purchase History First" Workflow ✅

### ~~4. Update ProcurementAgent system prompt with supplier-finding sequence~~ ✅

- ~~In `includes/agents/procurement_agent.py`, update `get_system_prompt()` to add an explicit **Supplier Finding Workflow** section that instructs the agent:~~
  1. ~~When the user asks "who can supply product X?" or similar, first call `search_products` to identify the product and its brand.~~
  2. ~~Then call `part_purchase_history(part_number)` to find suppliers with actual purchase records.~~
  3. ~~**Only if** `part_purchase_history` returns no results (no purchase history), fall back to `search_suppliers(brand=...)` to find suppliers linked to that brand.~~
  4. ~~If both return results, present the purchase history results first (proven suppliers), then mention the brand-linked suppliers as alternatives.~~
- ~~Keep the existing general tool descriptions intact — this is an additional workflow directive, not a replacement.~~
- Implementation note: Added **Supplier Finding Workflow** section to system prompt with product-based and brand-based sub-workflows. Product workflow: search_products → part_purchase_history → fallback to search_suppliers. Brand workflow: search_brands → search_suppliers.

### ~~5. Fix `part_purchase_history` sort order~~ ✅

- ~~In `includes/tools/product_tools.py`, in `_do_part_purchase_history()`, change `.order_by(desc('total_quantity'))` to `.order_by(desc('order_count'))` so results are sorted by **number of purchases** (most frequently used supplier first), not total quantity.~~
- ~~Update the output header text from "sorted by total quantity supplied" to "sorted by number of purchases".~~
- Implementation note: Changed sort column and header text. All 22 procurement/product tests pass.

## Phase 3 — Enrich Supplier Search with Purchase Stats ✅

### ~~6. Add purchase stats subquery to `_do_supplier_search()`~~ ✅

- ~~In `includes/tools/product_tools.py`, after collecting `supplier_ids` from results (~line 296), add a query against `ProductSupplier` grouped by `supplier_id` to fetch:~~
  - ~~`func.count(ProductSupplier.id)` → number of purchases~~
  - ~~`func.max(ProductSupplier.date)` → date of last purchase~~
- ~~When the `brand` parameter is provided, join `ProductSupplier → Product` and filter `Product.brand.ilike(...)` so that stats are scoped to that brand only.~~
- ~~When `brand` is not provided, aggregate across all products for each supplier.~~
- ~~Build a lookup dict: `supplier_purchase_stats = {supplier_id: (count, last_date)}`.~~
- Implementation note: Added purchase stats query after brand links query. Uses conditional join to Product table only when brand filter is active. Stats stored as `{supplier_id: (count, last_date)}` dict.

### ~~7. Include purchase stats in supplier search output formatting~~ ✅

- ~~In the output formatting loop (~line 310), append purchase stats to each supplier's output line:~~
  - ~~If purchases exist: `| Purchases: 27 | Last Purchase: 2025-11-15`~~
  - ~~If no purchases: `| Purchases: 0`~~
- ~~Suppliers with no rows in the lookup dict should display `Purchases: 0` (LEFT JOIN semantics handled by dict `.get()` with a default).~~
- Implementation note: Date formatted as "d Mon YYYY" (e.g. "15 Nov 2025") consistent with part_purchase_history output. All 22 tests pass.

## Phase 4 — Validation ✅

### 8. Run existing tests ✅

- Full suite: `uv run pytest tests/ -v` → **225 passed, 5 failed**.
- All 5 failures are **pre-existing on main** (confirmed by running the same tests on a clean `git stash` of main):
  - `test_langgraph_wiring_with_stub` — mock wiring mismatch
  - `test_graph_with_user_profile` — mock wiring mismatch
  - `test_start_registers_signal_handlers` / `test_handle_signal_calls_shutdown` — signal handler tests
  - `test_graph_with_user_profile` (integration) — same root cause
- **No regressions introduced by this branch.**

### 9. Manual verification (pending)

- Click "Find a Brand Supplier", type "who can supply hilti products" and confirm:
  - Supervisor routes to ProcurementAgent (not GeneralAgent).
  - ProcurementAgent uses `search_brands` then `search_suppliers(brand=...)`.
  - No hallucinated external supplier info.
- Ask "what supplier can supply product [known part]?" (without clicking a button) and confirm:
  - Supervisor routes to ProcurementAgent via LLM routing.
  - The agent calls `part_purchase_history` first.
  - Results are sorted by number of purchases.
  - If no purchase history, the agent falls back to `search_suppliers(brand=...)`.
- Search suppliers by brand and confirm purchase stats columns appear.
- Verify suppliers with no purchase history still appear with `Purchases: 0`.

## Scope Notes

**Files modified:**
- `includes/agents/supervisor.py` — intent-based routing, improved LLM routing prompt (Phase 1)
- `includes/agents/procurement_agent.py` — system prompt workflow directive, purchase stats display mandate (Phase 2)
- `includes/agents/base.py` — `_notify_retry()` helper for UI feedback on transient LLM errors
- `includes/tools/product_tools.py` — sort order fix in `_do_part_purchase_history`, purchase stats in `_do_supplier_search` (Phases 2–3)
- `tests/agents/test_supervisor.py` — replaced keyword-based tests with intent-based and LLM-mocked tests
- `.chainlit/config.toml` — enabled custom CSS
- `public/stylesheet.css` — wider table display for Chainlit UI

**No changes needed to:**
- Database models or migrations (no schema changes)
- Tool function signatures, parameters, or docstrings
- `app.py` — intent context is already injected into graph state correctly
- `includes/actions.py` — action handlers already set intent context correctly
- `includes/prompts.py` — the `INTENTS` dict context strings are correct; the fix is making the Supervisor actually use them
