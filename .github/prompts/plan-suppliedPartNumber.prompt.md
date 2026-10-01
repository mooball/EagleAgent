# Plan: RFQ Supplied Part Number (requested vs quoted)

**Created:** 2026-10-01
**Status:** 🟡 Proposed — awaiting review
**Scope:** RFQ line items — store the supplier's quoted part number, use it for the NetSuite push, surface it in the UI
**Related task:** todo.vu #33073 — "Always override part number if different" (client 116, project 1028 EagleAgent: RFQ)
**Related docs:** `plan-opportunityItemSync.prompt.md`, `plan-quotationTab.prompt.md`

---

## Summary

When a supplier quotes an item they often give their own part number — frequently
an **alternative** to the one the customer asked for. Today we capture that number
(from the quote email/PDF) onto the supplier's entry as `quote_part_number`, but we
almost never *use* it:

- On supplier selection we copy it into `RFQItem.part_number` **only when the item
  has no part number yet** (`_is_empty_part_number`), otherwise we discard it.
- The NetSuite push therefore sends the **requested** number, never the quoted one.
- Nothing in the UI shows the supplier's quoted number, so an alternative part is
  invisible.

We will introduce a **persistent `supplied_part_number`** on `rfq_items` and a single
**effective part number** = supplied (if set) else requested. Selection populates
supplied from the chosen supplier's quote; the NetSuite sync pushes the effective
number; the UI shows `requested / supplied` with an "alt" badge.

**Decision (confirmed with the user):** use a **persistent field**, not derive-at-read,
for two reasons:
1. Users will occasionally need to **manually override** the supplied number
   independently of the supplier's quote.
2. It makes **description-only lines** and the **readiness gate** simpler: a line with
   no requested number still becomes syncable once a supplier supplies one.

---

## Current behaviour (ground truth)

| Concern | Where | Today |
|---|---|---|
| Requested part number | `RFQItem.part_number` (`includes/dashboard/models.py:419`) | Populated at RFQ creation. |
| Supplier's quoted number | `suppliers[].quote_part_number` in `RFQItem.suppliers` JSONB | Written by the quote pipeline (`includes/tools/supplier_quote_pipeline.py:616, 718–739`); preserved by `rfq_crud.py:1303/1348`. |
| On select — copy to item | `_select_supplier_on_item` (`rfqs.py:1452`), `quotation_select_supplier` (`rfqs.py:3354`), `_select_quote_core` (`rfq_crud.py:1878`) | Only when `part_number` is empty/placeholder. |
| Sync source number | `_sync_opportunity_items_sync` (`rfqs.py:1068`, `:1128`, `:1139`) | `item.part_number` (requested) → `ensure_item_with_vendor(part_number=…)` + `custcol_new_item_code`. |
| Readiness product match | `_rfq_sync_readiness` (`rfqs.py:675`) | linked product's `part_number` must equal `item.part_number`. |
| Dirty detection | `_diff_sync_snapshot` (`rfqs.py:901`) | compares `item.part_number` to `snapshot[line].part_number`. |
| Supplier-quote edit endpoint | `quotation_update_supplier_quote` (`rfqs.py:3234`) | editable fields are `quote_status/quote_cost/quote_currency/quote_leadtime` — **not** `quote_part_number`. |
| UI | `_rfq_items_table.html`, `_rfq_selection_row.html`, `_rfq_quotation_table.html`, `_rfq_quotation_final.html` | Show `part_number` only; `quote_part_number` is never displayed. |

NetSuite item resolution (`ensure_item_with_vendor`, `includes/netsuite/records/item.py:368–429`)
already does local smart-match → NetSuite `itemid` lookup → create, keyed purely on the
part number it is handed. Feeding it the effective number is exactly the desired
"push the quoted number, create/match as if it were a new line" behaviour.

---

## Behavioural rules

| Event | `supplied_part_number` |
|---|---|
| RFQ created / item imported | `NULL` |
| Supplier selected, quotes a number **different** (normalised) from requested | that number |
| Supplier selected, quotes a number **equal** to requested, or none | `NULL` |
| Selection switched to another supplier | recomputed from the new supplier |
| Selected supplier deselected | `NULL` |
| Selected supplier's quoted part number edited | recomputed |
| User manually edits supplied on the line | stored verbatim (blank clears it) |
| Requested `part_number` edited | **unchanged** |

`requested` = `part_number`, `supplied` = `supplied_part_number`, and the
**effective** number for NetSuite is `supplied` when non-empty else `requested`.

---

## Design decisions

- **One new column, one resolver.** `RFQItem.supplied_part_number` (String, nullable);
  `effective_part_number(requested, supplied)` centralised next to
  `_is_empty_part_number` in `includes/tools/rfq_crud.py`.
- **Selection writes it; nothing derives at read time.** Every selection path sets or
  clears supplied, so read paths (readiness, sync, UI) just read the field. This keeps
  readiness unchanged in cost — no new per-item query.
- **NetSuite link follows the effective number.** The readiness product-match guard and
  the sync both key off the effective number. When supplied differs, the requested
  product link is intentionally *not* reused; `ensure_item_with_vendor` finds/creates
  the supplied item (the pre-EagleAgent behaviour the user described).
- **Requested stays the anchor for guidance data.** Product search, last-sale
  (`_annotate_last_sale`), price history and supplier ranking keep using
  `part_number`. They guide the supplier choice; once chosen they are secondary.
- **"Alternative" is a derived display flag**, not a new `match` value. `match`
  remains the classification field (`specific`/`branded`/`generic`/`discrepancy`).
- **Preline for all new UI** (badges/tooltips via `data-hs-*`), per project standard.

## Open decisions (confirm before starting)

1. **Manual override vs selection change.** Proposed: a selection change *recomputes*
   supplied and overwrites any manual override (last action wins). Alternative: a
   manual override "sticks" until the user clears it. *Recommend last-action-wins.*
2. **Requested edited to equal supplied.** Proposed: leave supplied as-is; the UI just
   stops showing it as "alternative" when they are equal. Alternative: auto-clear.
   *Recommend leave as-is (less magic).*

---

## Phase 1 — Data model & migration ✅

### ~~1. Add `supplied_part_number` to `RFQItem` + Alembic migration~~ ✅
- ~~Add `supplied_part_number = Column(String, nullable=True)` to `RFQItem`
  (`includes/dashboard/models.py`), with a comment explaining requested vs supplied
  and the effective-number rule.~~
- ~~Add an Alembic revision (additive column, nullable — safe on the live table), modelled
  on `alembic/versions/c7d8e9f0a1b2_add_pipeline_activity_to_rfqs.py`.
  Current head is `d2e3f4a5b6c7` (`alembic heads`); set it as `down_revision` unless
  a newer head has landed.~~
- ~~Include a `downgrade()` that drops the column.~~
- Implementation: added `supplied_part_number = Column(String, nullable=True)` to
  `RFQItem` in `includes/dashboard/models.py`, directly after `part_number`, with
  comments on requested/supplied/effective. Migration
  `alembic/versions/e7a1b2c3d4f5_add_supplied_part_number_to_rfq_items.py`
  (`down_revision = 'd2e3f4a5b6c7'`; `downgrade()` drops the column). `alembic heads`
  reports the single head `e7a1b2c3d4f5`.

### ~~2. Centralise effective-part-number resolution~~ ✅
- ~~In `includes/tools/rfq_crud.py`, beside `_is_empty_part_number` (`:84`), add:
  - `effective_part_number(requested, supplied) -> str | None` — supplied when
    non-empty (via `_is_empty_part_number`), else requested; both stripped.
  - `is_alternative_part_number(requested, supplied) -> bool` — True only when supplied
    is non-empty **and** normalised (`normalize_part_number`) different from requested.~~
- ~~Use these everywhere instead of ad-hoc `or` fallbacks, so the rule lives in one place.~~
- ~~Unit tests for both helpers (empty/placeholder/normalised-equal cases).~~
- Implementation: added `effective_part_number(requested, supplied=None)` and
  `is_alternative_part_number(requested, supplied)` beside `_is_empty_part_number` in
  `includes/tools/rfq_crud.py`. The alternative check is separator- **and
  case-insensitive** — a case-only difference is the same part, so not an alternative
  (a deliberate refinement over the plan text). Tests in
  `tests/tools/test_rfq_part_number_helpers.py` (14 passing).

---

## Phase 2 — Selection & quote-edit recompute ✅

### ~~3. Recompute supplied on supplier select/deselect (all three paths)~~ ✅
- ~~The three selection paths currently copy into `part_number`; replace that with the
  supplied rule and remove the `part_number` overwrite entirely.~~
- ~~`_select_supplier_on_item` (`rfqs.py:1444–1457`) — bulk/select-all helper (operates on
  the ORM `item`). On select: set `item.supplied_part_number` from the target's
  `quote_part_number` when it is an alternative, else `None`. On the deselect branch,
  clear it.~~
- ~~`quotation_select_supplier` (`rfqs.py:3339–3357`) — single star endpoint. Same.~~
- ~~`_select_quote_core` (`rfq_crud.py:1866–1884`) — chat/tool path. Same, including the
  deselect-at-`:1863` branch clearing supplied.~~
- ~~Delete the `_is_empty_part_number(...) → item.part_number = supplier_pn` blocks; the
  requested number must never be overwritten by selection.~~
- Implementation: added `supplied_from_supplier(requested, supplier_entry)` to
  `rfq_crud.py` to centralise the rule, and switched all three paths to it. The old
  `part_number` overwrite is gone. `_select_supplier_on_item` and
  `quotation_select_supplier` set supplied on select; `quotation_select_supplier` and
  `_select_quote_core` clear it on deselect. `quotation_select_supplier_all` inherits the
  behaviour via `_select_supplier_on_item`.

### ~~4. Recompute supplied when the selected supplier's quote number is edited~~ ✅
- ~~Add `quote_part_number` to the editable `quote_fields` in
  `quotation_update_supplier_quote` (`rfqs.py:3270`).~~
- ~~After applying the field changes, if the edited supplier is the line's **selected**
  supplier, recompute `line_item.supplied_part_number` from the (new)
  `quote_part_number` using the same rule as Task 3.~~
- ~~Mirror the recompute in `_update_supplier_sync` (`rfq_crud.py`) so the chat/tool path
  behaves identically.~~
- Implementation: `quotation_update_supplier_quote` now accepts `quote_part_number`
  (trimmed; blank → `None`) and recomputes supplied when the edited supplier is the line's
  selected supplier. The chat/tool path recomputes in `_update_supplier_core`, so
  `_update_supplier_sync` and `_update_quotes_bulk_sync` inherit it. An edit to a
  non-selected supplier's quote number leaves supplied untouched.

### ~~5. Manual override via update_item / edit forms (backend)~~ ✅
- ~~Add `supplied_part_number` to the `updatable` list in `_update_item_core`
  (`rfq_crud.py:848`) — **not** to `_no_clear` (blank must clear it) and **not** to the
  `_identifying` set (editing it must not reset the pipeline or clear `product_id`).~~
- ~~Treat placeholder values as a clear (`_is_empty_part_number`), matching `part_number`
  semantics elsewhere.~~
- ~~Route the dashboard edit-item form / edit-all grid fields through `update_item` so the
  override reaches the same code path.~~
- Implementation: `supplied_part_number` is in the `updatable` list (not `_no_clear`, not
  `_identifying`); blank/placeholder values normalise to `None`. Editing it does not reset
  the pipeline or drop `product_id`. The dashboard edit-form / JS wiring is Phase 4
  (task 11), so the field is backend-ready but not yet exposed in the UI.
- Tests: `tests/tools/test_rfq_part_number_selection.py` (DB-free core paths) plus the
  rewritten `tests/test_rfq_selection_row.py`. Verified against the local dev DB after
  `alembic upgrade head`; the DB-backed `tests/test_rfq_supplier_id_integrity.py` and
  `tests/tools/test_rfq_bulk.py::TestSelectQuotesBulk` pass.

---

## Phase 3 — Readiness & NetSuite sync ✅

### ~~6. Effective number in the readiness gate and product match~~ ✅
- ~~In `_rfq_sync_readiness` (`rfqs.py:575–839`):
  - Compute `item["effective_part_number"]` and `item["is_alternative_part_number"]` once
    per item (reuse the selected-supplier lookup already at `:702` — no new query).
  - "Part number not set" issue (`:760`) fires only when the **effective** number is empty,
    so description-only lines with a supplied number become syncable.
  - Product-match guard (`:673–677`) compares `normalize_part_number(p_cand.part_number)`
    to the **effective** number. When supplied differs, `product_ns_id` resolves to `None`
    and the sync falls through to `ensure_item_with_vendor(effective)` — intended.
  - When effective ≠ requested and no linked product matches effective, raise the existing
    `item_unmatched` warning text mentioning the supplied number (e.g. "…a new NetSuite
    item will be created for <supplied>").~~
- Implementation: `_rfq_sync_readiness` computes `requested_pn`/`supplied_pn` from the
  persisted fields (no supplier lookup, no extra query) and annotates
  `item["effective_part_number"]` + `item["is_alternative_part_number"]`. The product guard
  and the "part number not set" gate both use the effective number. When the effective
  number is an alternative with no matching linked product, the warning reads
  "Supplier part '<n>' — a new NetSuite item will be created or matched".

### ~~7. Effective number in the Opportunity sync + snapshot~~ ✅
- ~~In `_sync_opportunity_items_sync` (`rfqs.py:916–1237`):
  - `effective = effective_part_number(item.get("part_number"), item.get("supplied_part_number"))`.
  - `ensure_item_with_vendor(part_number=effective, …)` (`:1067`).
  - `custcol_new_item_code = effective` (`:1128`).
  - Snapshot: store `part_number = effective`, and additionally
    `requested_part_number` + `supplied_part_number` for audit (`:1139`, `:1173–1186`).
  - The post-sync product link (`:1201–1218`) uses `s["part_number"]` (now effective) via
    `_find_product_by_code` — correct: the created/matched product is the effective one.~~
- Implementation: `_sync_opportunity_items_sync` computes `effective_pn` per ready line and
  uses it for `ensure_item_with_vendor(part_number=…)`, `custcol_new_item_code`, and the
  synced record's `part_number`. The snapshot additionally stores `requested_part_number`
  and `supplied_part_number`. Also added `supplied_part_number` to `_item_to_dict`
  (`rfq_crud.py`) — without it the dict path would have silently dropped the field.

### ~~8. Dirty detection uses the effective number~~ ✅
- ~~`_diff_sync_snapshot` (`rfqs.py:867–913`): compare the item's **effective** number to
  `snap.get("part_number")`, instead of `item.get("part_number")`. Keep the function pure
  (no DB) by computing effective from the item dict itself.~~
- ~~This makes a supplier switch, a manual override, or a selected-supplier quote edit all
  correctly mark the line dirty for re-sync.~~
- ~~Verify `_refq_sync_readiness` annotates the effective field **before** it calls
  `_diff_sync_snapshot` (`:834`), or have the function compute it locally.~~
- Implementation: `_diff_sync_snapshot` computes the effective number from the item dict
  itself (stays pure) and compares it to the snapshot's `part_number`. A supplier switch, a
  manual override, or a cleared override all mark the line dirty.
- Tests: `tests/test_opportunity_sync.py` (sync-uses-supplied + three diff cases) and
  `tests/test_rfq_supplier_id_integrity.py` (readiness gate + product match). Full RFQ
  suite green: 154 passed across `test_rfq_crud`/`test_rfq_bulk`/`test_quote_tools`, 120
  passed across the supplied/opportunity/readiness files.

---

## Phase 4 — UI ✅

### ~~9. Part-number columns show `requested / supplied` with an "alt" badge~~ ✅
- ~~In `_rfq_selection_row.html` (`:18`), `_rfq_quotation_table.html` (`:40`) and
  `_rfq_quotation_final.html` (`:91`), render the requested number normally and, when
  `is_alternative_part_number` is true, append the supplied number as a distinct
  secondary token (e.g. `BOLT-123` then a muted/amber `SP-1234`).~~
- ~~Add a small **"alt"** badge with a Preline tooltip: "Alternative part quoted by
  {selected supplier}". When supplied is absent or equal, render the requested number
  alone — no slash, no noise.~~
- ~~`_rfq_items_table.html` (`:38–40`) shows the requested number in an editable input
  (`:164`); add the supplied override input here too (Task 11).~~
- Implementation: added a reusable `part_number_cell(item, supplier_name)` macro to
  `templates/components.html` (requested / supplied + amber **alt** badge + Preline
  tooltip naming the selected supplier). Applied in `_rfq_selection_row.html`,
  `_rfq_items_table.html`, `_rfq_quotation_table.html` and `_rfq_quotation_final.html`.
  Added `_annotate_part_numbers(item)` in `rfqs.py`, called by `_rfq_detail_context`
  (all tabs), `_annotate_selection_state` and `_rfq_sync_readiness`, so every tab has
  `effective_part_number` / `is_alternative_part_number` without a DB hit.

### ~~10. Editable per-supplier "Quoted Part #" on the Quotation tab~~ ✅
- ~~Add a **Quoted Part #** column to the per-supplier table in `_rfq_quotation_table.html`
  (`:75–80`), bound to `sup.quote_part_number`, editable inline in the existing
  click-to-edit style.~~
- ~~Wire `updateSupplierQuote(line, name, 'quote_part_number', value)`; extend
  `updateSupplierQuote` in `base.html` (`:2063`) so the field persists and, for the
  selected supplier, triggers a selection-row refresh (the backend recompute from Task 4
  updates the item's supplied number).~~
- ~~This is where users fix OCR/extraction mistakes; because it writes to the supplier
  entry, the persisted supplied field updates automatically on save.~~
- Implementation: added the **Quoted Part #** column to the per-supplier table in
  `_rfq_quotation_table.html` (inline edit → `updateSupplierQuote(..., 'quote_part_number', ...)`);
  `updateSupplierQuote` in `base.html` re-renders the tab on save so the item's
  requested/supplied display updates.
- ⚠️ **Deviation / caveat:** `_rfq_quotation_table.html` is the **legacy hidden tab**
  (`quotation-old`, route live but no tab button). The visible path for correcting the
  pushed number is the Items-tab override (Task 11). Moving this editor onto the visible
  Selection or Quotation tab is a small follow-up — flagged for the user.

### ~~11. Manual override input on the Items tab / edit-all~~ ✅
- ~~Add a `supplied_part_number` input to the Items tab (`_rfq_items_table.html`) and the
  edit-all grid (`rfq_detail.html:508`), visually paired with the requested part number,
  with a placeholder/help text "Supplier's part number (auto-filled on selection)".~~
- ~~Include the field in the client-side payloads assembled in `base.html`
  (item edit form + edit-all save).~~
- ~~Blank clears the value (Task 5 semantics).~~
- Implementation: added a "Supplier Part # (override)" input to the Items-tab edit row and
  a "Supplier Part #" column to the edit-all grid (`rfq_detail.html`, incl. a `<col>` and
  header). `base.html` now sends `supplied_part_number` from both payloads; the
  `update-item` and `bulk-update-items` routes whitelist the field. Blank clears it
  (backend `_update_item_core`).

### ~~12. Sync modal shows the number to be pushed~~ ✅
- ~~In `_rfq_quotation_final.html` (`:284`) show the **effective** number and, when it is
  an alternative, a callout: "Will push quoted part `SP-1234` (alternative) — a new
  NetSuite item may be created."~~
- ~~Reuse the readiness `item_unmatched`/alternative warning rather than adding a second
  source of truth.~~
- Implementation: the sync-requirements modal now shows `effective_part_number` and, for
  an alternative, "— quoted alternative (requested REQ-1)". The readiness
  `item_unmatched` warning already names the supplied part (Phase 3).

---

## Phase 5 — Tests, verification & docs

### ~~13. Update and extend tests~~ ✅
- ~~`tests/test_rfq_selection_row.py`:
  - `test_selects_and_copies_cost_and_part_number` (`:124`) and
    `test_preserves_existing_part_number` (`:141`) encode the old "copy into
    `part_number`" rule — rewrite for supplied semantics (requested preserved, supplied
    set/cleared).
  - Add cases: supplier quotes equal number → supplied `None`; deselect → supplied
    `None`; switch supplier → recompute.~~
- ~~`tests/test_rfq_selection_endpoints.py` (`_sup(...)` at `:92`) — extend the helper and
  assert supplied on select/deselect.~~
- ~~`tests/test_opportunity_sync.py` — snapshot asserts (`:472`) still hold when supplied
  is absent; add a case where supplied differs and assert `ensure_item_with_vendor` is
  called with the supplied number and the snapshot records it. Extend the
  `_diff_sync_snapshot` cases (`:618`) for a supplied-number change.~~
- ~~Add readiness tests: description-only line + supplied number passes the part-number
  gate; effective-number product match.~~
- ~~Add `_update_item_core` tests for the manual override (set, normalise, clear) and that
  editing supplied does **not** reset the pipeline or clear `product_id`.~~
- ~~Add unit tests for `effective_part_number` / `is_alternative_part_number`.~~
- Implementation: rewrote `tests/test_rfq_selection_row.py` (incl. a display test for the
  `requested / supplied` macro); added `tests/tools/test_rfq_part_number_helpers.py`
  (helpers, 14) and `tests/tools/test_rfq_part_number_selection.py` (write paths);
  extended `tests/test_opportunity_sync.py` (sync-uses-supplied + 3 diff cases) and
  `tests/test_rfq_supplier_id_integrity.py` (readiness gate + product match). Full
  RFQ/template sweep green (183 passed in the focused run, plus 154 tool tests).

### 14. Verification and documentation
- ~~**No N+1:** confirm the new field adds no per-item query — effective/is-alternative are
  computed from data already loaded (`suppliers` JSONB + `part_number`). Re-check the
  Selection/Quotation render timings against `plan-rfqSelectionPerformance.prompt.md`.~~
  - Done: `_annotate_part_numbers` is pure (string ops on the item dict); the product
    guard now compares against the same, already-loaded product rows.
- **No duplicate NetSuite items:** dry-run a two-supplier line locally (supplied differs)
  and confirm the requested item is not created, the supplied item is created/matched,
  and a re-sync marks the line clean (diff against the snapshot). — *Remains for manual
  local testing (NetSuite writes).*
- ~~Confirm `last_sale`, price history and product search remain keyed on the requested
  number (no change).~~ — Done: unchanged.
- Update `AGENTS.md` RFQ notes if the requested/supplied distinction is worth recording;
  cross-reference this plan from `docs/` if a relevant page exists. — *Pending review.*

---

## Review fixes (PR #223)

Copilot reviewed the PR and raised two medium findings, both the same root cause:

- **`quotation_update_supplier_quote`** (`rfqs.py`) and **`_update_supplier_core`**
  (`rfq_crud.py`) recomputed `supplied_part_number` only when the *quoted part number*
  changed. But these paths also accept `quote_status` (used by `toggleDeclined` and
  `cycleQuoteStatus`, and by `_decline_quote_sync` on the chat/tool path), so declining
  the selected supplier left a stale supplied number.

**Agreed and fixed.** Added `supplied_after_supplier_change(requested, supplier, was_selected)`
→ `(apply, value)`: derives from the supplier when it is (now) selected; clears it when it
*was* selected and no longer is; otherwise leaves the stored value untouched (so a manual
override survives editing a non-selected supplier). Both write paths now call it whenever
`quote_status` or `quote_part_number` changes. Tests added in
`tests/tools/test_rfq_part_number_selection.py` (`TestSupplierStatusChange`,
`TestSuppliedAfterSupplierChange`).

Two further Copilot observations needed no change:

- **"Tooltip trigger is missing"** (`components.html`) — **not a bug.** Preline defaults
  `toggle` to the wrapper when there is no `.hs-tooltip-toggle` (verified in the vendored
  `preline@4.2.0`); the existing brand/match tooltips use the same pattern.
- **"Quoted part editor not in the visible flow"** — known, and accepted: the quoted-part
  editor lives on the legacy `quotation-old` tab, and the Items-tab override is the
  intended visible path.

---

## Out of scope

- Changing how the quote pipeline *extracts* supplier part numbers (already works).
- Feeding the supplied number into product search, last-sale or supplier ranking — by
  decision these stay on the requested number.
- Editing a NetSuite item's part number in place — the model is "create/match a new item
  under the quoted number", matching legacy behaviour.
