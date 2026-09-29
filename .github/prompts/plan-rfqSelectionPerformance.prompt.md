# Plan: RFQ Selection Tab — Star-Click Latency & Bulk Supplier Select

**Created:** 2026-09-29
**Status:** ✅ Complete — Phases 1–4 implemented and verified on branch `optimise-supplier-selection`
**Branch baseline:** current working branch
**Evidence source:** production DB (read-only) + representative local snapshot, 2026-09-29

---

## Summary

On the RFQ **Selection** tab, clicking a supplier's star (to choose who supplies a
line) takes several seconds — and gets worse as the RFQ grows. Users report this is
painful on RFQs with many items.

The write itself is trivial: **two lookups and one `UPDATE`**. The delay is not the
action — it is the response. After the write, the client re-renders the **entire RFQ
detail page** (`htmx.ajax('GET', '/partial/rfqs/{id}/selection')` replacing
`#main-content`), and that render recomputes every decorated field for every item,
including enrichment the Selection tab never displays and a full table scan of
`products`/`transactions` for the "Last" column.

This plan fixes that in two independent layers, then adds a **Select all** action
for a single supplier across the RFQ.

---

## Evidence

### Click flow

`selectSupplier(line, name)` — `templates/base.html:2108`:

1. `POST /partial/rfqs/{id}/items/{line}/select-supplier` — the write
   (`includes/dashboard/routes/rfqs.py:3043`): SELECT rfq, SELECT rfq_item, mutate
   `suppliers` JSONB + `cost_price` (+ `part_number` when empty), COMMIT.
2. `.then(...)` → `htmx.ajax('GET', '/partial/rfqs/{id}/selection')` → generic tab
   route `partial_rfq_detail_tab` (`rfqs.py:3368`) → `_get_rfq_dict_sync` +
   `_enrich_rfq_supplier_contacts` + `_rfq_detail_context` (`rfqs.py:1230`) →
   full `partials/rfq_detail.html`.

`_rfq_detail_context` runs, unconditionally:

- `_rfq_sync_readiness(rfq)` (`rfqs.py:1239`) — NetSuite/product/supplier/contact work
- `_annotate_brand_db_status(rfq)` (`rfqs.py:1240`) — brand DB matching
- `_annotate_last_sale(rfq)` (`rfqs.py:1243`) — only on the Selection tab

### Measured cost

Local snapshot, same host: 306k `products`, 270k `product_suppliers`, 31k `brands`.

| Operation | Time | Notes |
|---|---|---|
| `last_sale_for_part_numbers` (31 part numbers) | **~1.4 s** | `EXPLAIN`: Parallel Seq Scan of `products` |
| `match_brands` (9 brands) | **~218 ms** | seq scan of `brands`; called **twice** per render |
| Index-friendly rewrite of the above | **~0.6 ms** | uses `idx_products_*_norm_ci` (≈230×) |

Production, read-only, `RFQ-2026-1233` (173 items / 153 part numbers), timings
inflated by the Railway proxy but structurally representative:

| Stage | Time |
|---|---|
| `_get_rfq_dict_sync` | 9.1 s |
| `_enrich_rfq_supplier_contacts` | 2.9 s |
| `_rfq_sync_readiness` | 15.7 s |
| `_annotate_brand_db_status` | 3.4 s |
| `_annotate_last_sale` | 23.5 s |
| `_rfq_detail_context` total | **40.2 s** |

### Root causes

1. **Non-index-usable part-number matching.** `last_sale_for_part_numbers`
   (`includes/tools/product_tools.py:1108`) uses
   `regexp_replace(col,'[^a-zA-Z0-9]','','g') ILIKE ?` → full Seq Scan of `products`.
   The module already documents this trap at `_norm_expr_ci` (`product_tools.py:52`).
2. **Same pattern in `match_brands`** (`product_tools.py:1206`, `1211`) → seq scan of
   `brands`, and it is called **twice per render** (`_rfq_sync_readiness` at
   `rfqs.py:635` and `_annotate_brand_db_status` at `rfqs.py:1313`).
3. **`_rfq_sync_readiness` is far heavier than the tab needs.** It runs on every tab,
   and calls `open_near_miss_pairs(session, ...)` **once per item** (`rfqs.py:692`) —
   an N+1. The Selection tab only consumes `selected_supplier` + `link_broken`.
4. **The full re-render.** Every click re-serialises a 150–230-row frozen-column
   table, header and all modals, then swaps `#main-content`.
5. **No memoisation or scoping.** Each render recomputes every enrichment for every
   item, even when the click changed nothing they display. Addressed here by index
   fixes and tab scoping (tasks 1–4), not by caching.

### Tab → enrichment dependency map

| Enrichment | Items | Selection | Quotation | Suppliers / Comms |
|---|---|---|---|---|
| `_annotate_brand_db_status` | ✅ | ❌ | ❌ | ❌ |
| `_annotate_last_sale` | ❌ | ✅ | ❌ | ❌ |
| selection state (`selected_supplier`, `link_broken`) | ❌ | ✅ | ✅ | ❌ |
| full `_rfq_sync_readiness` | ❌ | ❌ | ✅ | ❌ |
| `_enrich_rfq_supplier_contacts` | partial | ❌ | ✅ | ✅ |

(`_rfq_sync_readiness` is *also* called deliberately by the sync/export path at
`rfqs.py:942`; that call stays.)

---

## Design Decisions

- **Correctness is not at stake.** The selected supplier, quote status, `cost_price`
  and `part_number` all live inside the one `rfq_items` row the POST already read.
  There is no cross-row invariant beyond "deselect the other suppliers on this line".
  Trimming the render is removing unnecessary work, not removing a guarantee.
- **Keep the server as the source of truth.** Prefer a targeted server-rendered row
  swap over a purely optimistic client toggle: less duplicated logic, no divergence
  risk. Optimistic UI is treated as an optional fallback only if row-swap is not
  snappy enough.
- **Fix the queries first.** Task group 1 is low-risk and benefits every tab (Items,
  Quotation, email flows), not just Selection.
- **Scope enrichment to the tab that needs it.** See the dependency map above.
- **Caching considered and rejected.** An earlier draft proposed TTL-caching the
  last-sale/brand lookups. It was dropped: keyed on the whole part-number set, the
  cache busts whenever a star fills an empty part number — exactly when a large RFQ
  is being worked — and once the index fixes land the queries are cheap enough that
  a cache buys little while adding staleness and invalidation risk. Fix the queries
  properly instead.
- **Preline UI for new UI.** The Select-all confirmation should use a Preline
  overlay/modal rather than another hand-rolled modal (per `AGENTS.md`, Preline is
  the default for new UI). This is also a chance to note the existing hand-rolled
  modals (`rfq_detail.html:1085–1126`) as future migration candidates.

---

## Phase 1 — Query-Level Fixes ✅

Low-risk, benefits every RFQ render. Do first and independently.

### ~~1. Make `last_sale_for_part_numbers` index-usable~~ ✅

- ~~**File:** `includes/tools/product_tools.py` (`last_sale_for_part_numbers`,
  line 1085).~~
- ~~Replace `_norm_expr(Product.part_number).ilike(norm)` /
  `_norm_expr(Product.supplier_code).ilike(norm)` with
  `_norm_expr_ci(col) == _norm_key(pn)` so the planner uses
  `idx_products_part_number_norm_ci` / `idx_products_supplier_code_norm_ci`
  (confirmed: Bitmap Index Scan, ~0.6 ms vs ~134 ms).~~
- ~~Update the Python-side post-filter (lines 1142–1146) to compare lower-cased
  normalised values (`_norm_key`) so it stays consistent with the CI match.~~
- ~~Preserve semantics: part number **or** supplier code match; latest by
  `Transaction.date desc nulls last`; `doc_type in ('Quote','SalesOrder')`.~~
- ~~Extend tests in `tests/tools/test_product_tools.py`.~~
- Implementation: switched to `_norm_expr_ci(col) == _norm_key(pn)` and lowered
  the Python post-filter to `_norm_key`. This also makes the match fully
  case-insensitive end to end (the old DB match was case-insensitive but the
  Python filter could drop case variants).
- Implementation: measured on the local snapshot — 31 part numbers went from
  **~1.4 s to ~7–27 ms**; `EXPLAIN` now shows a `BitmapOr` of `Bitmap Index
  Scan` on `idx_products_*_norm_ci` instead of a `Parallel Seq Scan`.
- Implementation: added `TestLastSaleForPartNumbers` (latest-sale-wins,
  case/punctuation-insensitive, supplier-code match, empty input).

### ~~2. Make `match_brands` index-usable and cheaper~~ ✅

- ~~**File:** `includes/tools/product_tools.py` (`match_brands`, line 1181).~~
- ~~Exact pass (line 1206): switch to `_norm_expr_ci(Brand.name) == _norm_key(n)`.~~
- ~~Add an Alembic migration creating a matching CI functional index on
  `brands(regexp_replace(lower(name),'[^a-z0-9]','','g'))`.~~
- ~~Near/substring pass (line 1211): a btree cannot serve `LIKE '%…%'`. Options, in
  order of preference:~~
  1. ~~Add a `pg_trgm` GIN index on the normalised expression and switch the near pass
     to a `contains()`/`%` form the GIN index can serve (verify with `EXPLAIN`).~~
  2. ~~Only run the near pass for names with no exact match — cheaper, but drops the
     "alternatives" suggestions that are currently shown alongside an exact match.~~
- ~~Extend tests in `tests/tools/test_product_tools.py` (`tests/tools/test_product_tools.py:421`).~~
- Implementation: chose **option 1** (pg_trgm). The exact pass is CI equality;
  the substring pass is `_norm_expr_ci(name).like('%key%')`, verified via
  `EXPLAIN` to use a `gin_trgm_ops` GIN index even with a parameterised pattern.
  Option 2 was rejected — existing tests (`test_exact_with_alternatives`,
  `test_match_brands_batch_mixed`) require the alternatives even when an exact
  match exists.
- Implementation: new migration `c1d2e3f4a5b6_add_brand_name_norm_indexes.py`
  adds `idx_brands_name_norm_ci` (btree) and `idx_brands_name_norm_trgm`
  (pg_trgm GIN); `pg_trgm` was already installed in local and production.
- Implementation: inputs are grouped by normalised key so the DB work is deduped
  while the result still carries an entry for every input name (preserving the
  "dict keyed by exact input" contract).
- Implementation: measured `match_brands` (9 brands) ~218 ms → ~2 ms.

### ~~3. Compute brand matching once per render~~ DISCARDED

- ~~**File:** `includes/dashboard/routes/rfqs.py`.~~
- ~~Compute the brand lookup once in `_rfq_detail_context` (or a small helper) and
  pass it into both consumers, instead of `_rfq_sync_readiness` (line 635) and
  `_annotate_brand_db_status` (line 1313) each calling `match_brands` again.~~
- ~~Keep the two callers' signature backwards-compatible (optional param) so existing
  tests and the sync path continue to work.~~
- Reason: superseded by task 4. Once `_annotate_brand_db_status` runs only on the
  Items tab and `_rfq_sync_readiness` only on the Quotation tab, the two brand
  lookups can no longer occur in the same render — there is nothing left to
  share. Revisit with a shared helper only if a future change runs both together.

### ~~4. Scope enrichment to the tab that needs it~~ ✅

- ~~**File:** `includes/dashboard/routes/rfqs.py` (`_rfq_detail_context`, line 1230).~~
- ~~Call `_annotate_brand_db_status` only for the **items** tab.~~
- ~~Call `_annotate_last_sale` only for the **selection** tab (already the case).~~
- ~~Call the full `_rfq_sync_readiness` only for the **quotation** tab. Extract the
  cheap selection state (`selected_supplier`, `link_broken`) into a light helper
  (e.g. `_annotate_selection_state(rfq)`) used by selection + quotation.~~
- ~~Verify no other template consumes the removed fields (dependency map above;
  `sync_*` fields are used only by `_rfq_quotation_final.html`).~~
- Implementation: `_rfq_detail_context` now dispatches per tab — quotation →
  `_rfq_sync_readiness`; items → `_annotate_brand_db_status`; selection →
  `_annotate_selection_state` + `_annotate_last_sale`.
- Implementation: new `_annotate_selection_state` sets only `link_broken` and
  `selected_supplier` (one Supplier existence query) — the two fields the
  Selection matrix renders — with no product/brand/contact/NetSuite work.
- Implementation: verified before scoping that `sync_*`/`openNsSupplier` are
  quotation-only and `brand_db_*` is items-only.

### ~~5. Batch `open_near_miss_pairs`~~ ✅

- ~~**File:** `includes/dashboard/routes/rfqs.py:692` (inside the per-item loop).~~
- ~~Add a batched variant taking the set of selected supplier ids and returning a
  `{supplier_id: [near_miss, ...]}` map in (at most) two queries, and use it from
  `_rfq_sync_readiness` and `_enrich_rfq_supplier_contacts` as applicable.~~
- ~~Keep the single-id `open_near_miss_pairs` for the existing call sites in
  `supplier_dedup.py` / sync paths.~~
- Implementation: added `open_near_miss_pairs_batch` in `supplier_dedup.py` (one
  candidate query + one supplier query for all ids); `_rfq_sync_readiness`
  precomputes the map before its item loop. `_enrich_rfq_supplier_contacts` was
  already batched and was left alone.
- Implementation: the single-id helper is kept for the remaining call sites
  (`quote_tools`, `rfq_crud`, the supplier-to-netsuite route).
- Implementation: added `TestOpenNearMissPairsBatch` parity and edge tests
  (rejected pairs excluded, non-UUID/empty inputs ignored).

---

## Phase 2 — Targeted Update Instead of Full Re-render ✅

### ~~6. Return only the affected row from the select-supplier endpoint~~ ✅

- ~~Add a row partial, e.g. `templates/partials/_rfq_selection_row.html`, extracted
  from `_rfq_selection_matrix.html` (the `<tr class="item-row">`), used by **both**
  the full matrix render and the single-row path so they cannot diverge.~~
- ~~Change `POST /partial/rfqs/{id}/items/{line}/select-supplier` (`rfqs.py:3043`) to
  respond with the rendered row HTML (it already holds `line_item`). Alternatively
  add `GET /partial/rfqs/{id}/selection-row/{line}`; prefer returning HTML from the
  POST to keep it to one request.~~
- ~~Update `selectSupplier()` (`base.html:2108`) to replace only
  `tr[data-line="…"]` (add a `data-line` attribute), preserving scroll.~~
- ~~Update the totals row too (either a matching totals partial or a small client
  recompute) so Cost/Sale totals stay correct.~~
- ~~The row render needs `selected_supplier`, `link_broken` and `last_sale` — all
  cheap after Phase 1.~~
- Implementation: extracted `templates/partials/_rfq_selection_row.html` and
  `_rfq_selection_totals.html`; both are included by `_rfq_selection_matrix.html`
  and rendered standalone by `_selection_row_json()`.
- Implementation: `supplier_names` (column order) moved to Python
  (`_selection_supplier_names`) and passed into the matrix, so the full render and
  the row endpoint cannot disagree.
- Implementation: the POST now returns JSON `{"row", "totals"}`; a new
  `GET /partial/rfqs/{id}/selection-row/{line}` serves the same payload for
  interventions that have no HTML response of their own. The client swaps
  `#selection-table tr[data-line]` and `tr[data-selection-totals]`, with a full
  refresh fallback if the row is absent (keeps the legacy quotation table working).
- Implementation: only the rendered line is annotated (`{"items": [item]}`), so
  the click no longer scans every part number on the RFQ. Measured: **3–6 ms**
  server-side and a **~12 KB** payload for a 60-item RFQ, versus the ~825 KB full
  matrix HTML it replaces.
- Implementation: added `tests/test_rfq_selection_row.py` (row/totals render,
  missing line, selected + broken-link state, totals sums).

### ~~7. Apply the same treatment to sibling interactions~~ ✅

- ~~`toggleDeclined()` (`base.html:2125`) and `updateSupplierQuote()` (`base.html:2058`)
  use the same full re-render. Introduce a shared `refreshSelectionRow(line)` helper
  and migrate them where straightforward.~~
- ~~`updateItemPricing()` on Cost/Sale (`base.html`) similarly — assess scope; can be
  a follow-up.~~
- Implementation: both migrated to a shared `_refreshSelectionRow(line)` helper
  (PATCH, then fetch the row JSON). `updateSupplierQuote` keeps its
  selected-supplier `cost_price` follow-up PATCH, then refreshes the row.
- Implementation: `updateItemPricing()` on Cost/Sale was left as a follow-up — the
  Selection matrix never refreshed totals after a manual Cost/Sale edit (pre-existing
  behaviour, not a regression). Recommended as a small future task.

### ~~8. Optional — optimistic toggle~~ DISCARDED

- ~~Only if the row swap (task 6) is still not snappy enough: toggle the star and Cost
  cell immediately, then reconcile with the row response. Treat as a fallback, not
  the primary design.~~
- Reason: the row swap renders in 3–6 ms and replaces a ~825 KB page refresh with a
  ~12 KB fragment, so the perceived latency is already dominated by the network
  round-trip. Optimistic UI would add client/server divergence risk for no practical
  gain. Revisit only if real-world timing shows otherwise.

---

## Phase 3 — Select All for a Supplier ✅

### ~~9. Bulk select endpoint~~ ✅

- ~~**File:** `includes/dashboard/routes/rfqs.py`.~~
- ~~Add `POST /partial/rfqs/{rfq_id}/items/select-supplier-all`, JSON body
  `{"supplier_name": "…"}` (mirror the single endpoint's contract).~~
- ~~For every item where the supplier is `shortlisted`/`selected`, has a `quote_cost`,
  and is not `declined`: deselect any other selected supplier on that line, set this
  one `selected`, copy `quote_cost → cost_price`, and `quote_part_number →
  part_number` when the part number is empty — identical semantics to the single
  star.~~
- ~~Use `_commit_bulk_with_retry` with deterministic `order_by(RFQItem.id)` locking,
  matching `shortlist-supplier-all-items` (`rfqs.py:2715`).~~
- ~~Respect the pipeline lock (409) as `update-item`/`delete-item` do.~~
- Implementation: per-line semantics extracted to `_select_supplier_on_item()`
  (returns `changed` / `already` / `skipped` / `absent`); the endpoint loops over
  items and commits via `_commit_bulk_with_retry`. Pipeline activity is checked
  first and returns JSON 409.
- Implementation: for ≤ `_SELECT_ALL_INLINE_ROW_LIMIT` (25) changed lines the
  response carries the rendered rows + totals for a targeted swap; above that it
  returns `refresh: "full"` so the client does one tab refresh instead of a huge
  payload.

### ~~10. Report what was skipped~~ ✅

- ~~Return counts (selected / skipped-no-quote) — either in the response body or a
  follow-up toast — so the UI can say "selected on N lines; M lines had no quote
  from this supplier".~~
- Implementation: the response includes `changed` and `skipped`, and the client
  toasts "Selected on N line(s) — M with no usable quote". `skipped` counts only
  lines where the supplier is **present but not quotable** (declined / no cost);
  lines the supplier is not on are ignored, so a supplier quoted on 15 of 60 lines
  does not report 45 noisy "skips".

### ~~11. UI control + confirmation (Preline)~~ ✅

- ~~Add a "★ Select all" control in that supplier's column header
  (`templates/partials/_rfq_selection_matrix.html:79–90`), shown only when the
  supplier has ≥1 quotable line, with the count.~~
- ~~Confirmation via a **Preline overlay/modal** (flag: this is a new-UI moment and a
  good Preline adoption point; consider noting the existing hand-rolled modals as
  future migration candidates).~~
- ~~Refresh via the Phase 2 mechanism (affected rows / matrix), not a full page reload.~~
- Implementation: `_selection_supplier_quotable_counts()` gates a star-only button
  shown to the **left** of each supplier name (no text) when N ≥ 1; the count is in
  the tooltip.
- Implementation (follow-up): all selection-matrix stars (header + row select /
  deselect) now use **Preline `hs-tooltip`** rather than the browser `title`, which
  is slow to appear. `HSStaticMethods.autoInit()` is re-run after the client-side
  row swaps so tooltips on refreshed rows initialise.
- Implementation: confirmation is a **Preline overlay** (`#select-all-supplier-modal`)
  with Alpine for the dynamic name/count and the confirm action — the same
  Preline-overlay + Alpine-content pattern as the existing `#ns-supplier-modal`.
- Implementation: refresh uses the Phase 2 row/swap machinery (or a single tab
  refresh when > 25 lines change), with a toast summarising the result.

### ~~12. Optional — clear all / whole-RFQ supplier flow~~ DISCARDED

- ~~Optional "Clear all" for the same supplier.~~
- ~~Optional broader workflow: choose one supplier for the whole RFQ, apply to all
  quotable lines, and surface the lines it cannot cover.~~
- Reason: the "apply one supplier across the RFQ" goal is already covered by Select
  all, and the skipped-line toast surfaces what it could not cover. "Clear all" is a
  natural follow-up if users ask for it; not built now to keep the change focused.

### ~~13. Tests~~ ✅

- ~~Unit/integration tests asserting the bulk action yields exactly the same per-line
  state as clicking each star individually (including the deselect-others and
  `quote_cost`/`part_number` copy rules).~~
- Implementation: `tests/test_rfq_selection_row.py` — `TestSelectSupplierOnItem`
  (select + copy cost/part number, deselect previous, preserve existing part number,
  absent/declined/unquoted/already outcomes) and
  `TestSelectionSupplierQuotableCounts`.
- Implementation: `TestMutationGuards::test_select_supplier_all_blocked` asserts the
  pipeline-lock 409.

---

## Phase 4 — Testing & Verification ✅

### ~~14. Query regression tests~~ ✅

- ~~Assert the new `last_sale_for_part_numbers` / `match_brands` return identical
  results to the old implementation across case/punctuation variants.~~
- Implementation: `TestLastSaleForPartNumbers` (case/punctuation-insensitive match,
  latest-sale-wins, supplier-code match, empty input) and the existing/extended
  `TestMatchBrand`/`test_match_brands_batch_mixed` (case variants both returned as
  keys with the same result, punctuation-normalised exact, near-only, duplicates
  excluded).

### ~~15. Endpoint tests~~ ✅

- ~~Row endpoint returns the correct `<tr>` for a given line.~~
- ~~Single-select semantics unchanged (deselect previous, copy cost/part number).~~
- Implementation: `tests/test_rfq_selection_row.py` — row + totals render, missing
  line, selected/broken-link state, totals sums, and `TestSelectSupplierOnItem`.
- Implementation: `tests/test_rfq_selection_endpoints.py` — calls the route
  coroutines with a rolled-back real DB session: single-select copies cost,
  deselects the previous supplier, toggles off, 404 on unknown supplier; bulk
  selects quoted lines, skips declined/absent, returns the row payload for small
  changes and `refresh: "full"` above the threshold, and 409s under the pipeline
  lock.

### ~~16. Manual performance verification~~ ✅

- ~~Use the largest production RFQ (read-only) to compare before/after:~~
  - ~~`EXPLAIN ANALYZE` for `last_sale` and `match_brands` shows index scans.~~
  - ~~Time a star click end-to-end and confirm it no longer scales with item count.~~
- ~~Record before/after numbers in this plan (append notes; do not delete the original
  evidence).~~

#### Verification results (same-host local snapshot, 306k products / 270k transactions / 31k brands)

| Operation | Before | After |
|---|---|---|
| `last_sale_for_part_numbers` (31 PNs) | ~1.4 s (seq scan) | ~7–27 ms |
| `match_brands` (9 brands) | ~218 ms (seq scan ×2/render) | ~2 ms |
| `_rfq_sync_readiness` (60 items) | match scan ×2 + N+1 near-miss | ~20 ms (batched) |
| Selection-tab render enrichment | full readiness + brand scan | `_annotate_selection_state` 2 ms + `_annotate_last_sale` 29 ms |
| Star click server render | whole `rfq_detail.html` (~825 KB / 60 items) | one row + totals, **3–6 ms / ~12 KB** |

- `EXPLAIN` confirms the new plans: `BitmapOr` of `Bitmap Index Scan` on
  `idx_products_*_norm_ci` for last-sale; `Bitmap Index Scan` on
  `idx_brands_name_norm_ci` (exact) and `idx_brands_name_norm_trgm` (substring)
  for brand matching.
- **Deployment note:** the brand-index gain requires the
  `c1d2e3f4a5b6` migration to be applied on Railway. Until then, `match_brands`
  still seq-scans `brands` there (confirmed via a read-only timed run); the
  last-sale and row-render gains do not depend on it.

### ~~17. Full suite~~ ✅

- ~~`uv run pytest tests/ -v` (see the migrated-`.venv` note in `AGENTS.md` if
  `pytest` fails to spawn).~~
- Implementation: `uv run python -m pytest tests/ -q` → **1876 passed, 23 skipped**.
  The only intermittent failure is the environmental
  `tests/agents/test_browser_agent.py::test_real_browser_task` (missing
  `agent-browser` binary), unrelated to this work.

---

## Risks & Rollback

- **Case-sensitivity change** in `last_sale_for_part_numbers`: switching from
  `ILIKE` to CI-equality changes the DB match to lowercase-normalised; the Python
  post-filter must be updated in lockstep or results will silently differ. Cover with
  test 14.
- **Round-trip count**: task 6 must keep it to one request; a row endpoint + GET
  would reintroduce two.
- **Row/full-render divergence**: mitigated by sharing one row partial.
- **Bulk write concurrency**: reuse the existing `_commit_bulk_with_retry` +
  deterministic ordering so the bulk select does not deadlock other writers.
- **Migration**: the only schema change is the optional CI (and possibly `pg_trgm`)
  index on `brands`; additive and reversible.
