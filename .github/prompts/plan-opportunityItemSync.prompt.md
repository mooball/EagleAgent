# Plan: Sync RFQ Items to NetSuite Opportunity ("Update Opportunity")

## Overview
Each RFQ is linked to a NetSuite Opportunity. We need to push (or update) the RFQ's line items onto that Opportunity — including department, quantity, sale price, cost price, and the chosen supplier. A new **Update Opportunity** button on the RFQ header triggers the sync.

This plan is based on live read-only probes + write tests against the NetSuite REST API (2026-08-20, account 794882, test opportunity OP72714 / "Test Company 2"). Probe scripts: `_probe_ns_opp_items.py`, `_ns_opp_schema.json`, `_ns_opp_swagger.json`.

Staged approach agreed with the user:
1. **Phase 1** — UI gating: sync only allowed when every item matches a NetSuite item; rows highlighted; button disabled until all ready. Implement the line push for matched items.
2. **Phase 2** — Tool to manually push a product to NetSuite (separate sub-task).
3. **Phase 3** — One unified process: create missing items/products in NetSuite, then fully sync the Opportunity.

---

## Verified API Findings

### Write path (works)
`PATCH record/v1/opportunity/{id}` with body `{"item": {"items": [...]}}` — merge semantics:

| Rule | Detail |
|---|---|
| **New lines** | Send **without** the `line` key — NetSuite assigns the next free line number (observed: 1, 9, 12). |
| **Existing lines** | Include the `line` key — updates in place (verified). |
| **Fields accepted on lines** | `item` (ref), `description`, `quantity`, `rate`, `amount`, `department` (ref), `custcol_po_vendor` (vendor ref), `custcol_po_rate` (number) — all verified. |
| **`amount` is required** | Send `quantity`, `rate`, and `amount` explicitly (`0` accepted). |
| **taxCode — never send** | Sending `taxCode` caused 500s and bogus "record has been deleted" errors. NetSuite defaults it from the item (observed `GST:FREE`). |
| **`Prefer: transient` on PATCH** | Harmless but irrelevant; omit for clarity. |

### Write path (broken)
- **`?replace=item`** → always 400 `"The record has been deleted since you retrieved it."` even with clean payloads. Full sublist replace is **not available** on this account → merge-only.
- **`PUT /record/v1/opportunity/{id}`** → 405 (in swagger but not supported at runtime).
- **Description-only lines** (no `item` ref) → rejected: `"Please enter value(s) for: Item."` Every line requires a NetSuite item ref. (This is the driver for Phases 2–3.)
- **SuiteQL on `transactionLine`** returns 500 `UNEXPECTED_ERROR` for opportunity lines. Verification must use `GET record/v1/opportunity/{id}?expandSubResources=true`, which returns the full `item` sublist.

### Real line shape (from opportunity 999937 / OP60443)
```
line, item→inventoryitem, description, quantity, rate, amount,
department→{id,refName}, custcol_po_vendor→vendor, custcol_po_rate,
costEstimateType=PURCHORDERRATE, costEstimateRate, custcol_currency_symbol
```
Confirms the target shape exists in production today (manually maintained).

---

## Do we need to store the NetSuite line number locally? — YES

**Yes.** Without it, every push duplicates lines:

- `?replace=item` (the way to "just resend everything") is broken on this account.
- Merge PATCH only updates a line if the `line` key matches an existing NetSuite line.
- New lines sent without a key always append — so an unkeyed re-push would create duplicates every time.

Therefore:

- Add **`netsuite_line` (Integer, nullable)** to `RFQItem` (Alembic migration).
- Push algorithm per RFQ item:
  - `netsuite_line` set → include `"line": <netsuite_line>` (update in place).
  - `netsuite_line` null → send without `line` (NetSuite creates it).
- After the PATCH, `GET …?expandSubResources=true` and **reconcile**: match the response lines back to RFQ items (by item id + quantity + description, in push order to break ties) and store the assigned `netsuite_line` on each item.
- Note: NetSuite line numbers are not the RFQ line numbers (they're the next-free transaction line number), so they must never be assumed.

Deletion caveat: if an RFQ item is deleted after a push, the NetSuite line cannot be removed via REST (no replace, no documented per-line DELETE). Phase 1 ignores removals (stale lines flagged later). Optionally investigate `DELETE …/opportunity/{id}/item/{line}` on a test record — it exists as a `links` self-href but is not in the swagger.

---

## Readiness model (Phase 1 core)

**An item is "ready to sync" when:**
- it has a `product_id` linked to a local `Product` whose `products.netsuite_id` is set, **and**
- (loose for phase 1) it has a quantity.

Everything else — department, sale/cost price, chosen supplier — is optional on the line (0s/omitted are accepted), so it does **not** block readiness. (Phase 3 can tighten this once item auto-creation exists.)

### UI (Phase 1)

- **Row highlighting** in the Quotation tab items table (`templates/partials/_rfq_quotation_matrix.html`, and `_rfq_quotation_table.html` if it stays) + the main items table (`templates/partials/_rfq_items_table.html`):
  - 🟢 green dot / subtle green tint — ready (product has NetSuite item, maybe `netsuite_line` already synced).
  - 🟡 amber — has a product but the product has no `netsuite_id` (not in NetSuite).
  - 🔴/gray — no matched product at all.
  - Tooltip per row: "Ready to sync", "Product not in NetSuite — push product first", "No matching product".
- **Button**: `Update Opportunity` in the RFQ header card (`templates/partials/rfq_detail.html`, next to the existing NetSuite link).
  - Disabled until `ready_count == item_count`; caption shows `Update Opportunity (2/4 ready)`.
  - Disabled also when the RFQ has no linked opportunity (`rfq.opportunity_id` / `opportunities.netsuite_id` missing) — point the user at Create New.
- Readiness computed server-side (single source of truth) and passed into the RFQ detail context; Alpine in `templates/base.html` (`rfqDetail()`) uses it for the button state.

### Push endpoint (Phase 1)

- `POST /partial/rfqs/{rfq_id}/sync-opportunity` in `includes/dashboard/routes/rfqs.py`:
  1. Guard: all items ready, opportunity linked → else 400 with message (do not call NetSuite).
  2. Build lines from `RFQItem`s: `item` ref from `products.netsuite_id`, `description = "{part_number} - {input_description}"`, `quantity`, `rate = sale_price or 0`, `amount = quantity * rate`, `department` ref when `department_id` set, `custcol_po_rate = cost_price`, `custcol_po_vendor` from the selected supplier's `suppliers.netsuite_id` (if a selected supplier exists), and `line` when `netsuite_line` set.
  3. `PATCH` via a new `push_opportunity_items()` in `includes/netsuite/records/opportunity.py` (extend `NetSuiteClient.update_record` with a `params` arg).
  4. Reconcile line numbers back to `netsuite_line`, commit, re-render the RFQ detail partial + toast.

---

## Phase 2 — Manual "Push product to NetSuite" tool (sub-task)

- New helper `push_product_to_netsuite(product)` in `includes/netsuite/records/` (or `item.py`):
  - `POST record/v1/inventoryitem` with `itemid` (part_number), `description`, `department` ref, `custitem_brand` ref (brands table has NetSuite IDs) — exact required fields to be verified against the `inventoryitem` metadata catalog (same probe approach as the opportunity).
  - Idempotency: before create, check `SELECT id FROM item WHERE itemid = '<part_number>'` (SuiteQL) or `itemsByName` lookup; refuse if an active item with that itemid exists (or return its id).
  - Store the returned internal ID on `products.netsuite_id`.
- Trigger points: button on product detail (`templates/partials/product_detail.html`) and/or an action per RFQ item row ("Create item in NetSuite" on amber rows).
- This is deliberately **manual, one product at a time** — no bulk creation.

## Phase 3 — Unified "create missing items then sync" process

- One flow behind the same button:
  1. For each RFQ item without a matched NetSuite item, auto-create the `InvtPart` (part_number, description, brand, department) using the Phase 2 helper and link/create the local `Product` (part_number match on `products` table first).
  2. Then run the full line push with prices, supplier, and department.
  3. Button readiness rule becomes "every item has a part_number/description it could create from" rather than "already matched".
- Optional UX: a pre-flight dialog listing "3 items will be created in NetSuite: …" with Confirm/Cancel before writing.

---

## Suggested implementation files

| Area | File(s) |
|---|---|
| Model | `includes/dashboard/models.py` — add `netsuite_line` to `RFQItem` |
| Migration | `alembic/versions/` (auto-generated) |
| Client | `includes/netsuite/client.py` — `update_record(..., params=None)` |
| NetSuite logic | `includes/netsuite/records/opportunity.py` — `push_opportunity_items()`; new `includes/netsuite/records/item.py` (Phase 2) |
| Route | `includes/dashboard/routes/rfqs.py` — `POST /partial/rfqs/{id}/sync-opportunity`, readiness context |
| Templates | `templates/partials/rfq_detail.html` (button), `_rfq_items_table.html`, `_rfq_quotation_matrix.html` (row badges) |
| Alpine | `templates/base.html` — `rfqDetail()` sync state + button binding |
| Tests | `tests/test_dashboard_routes.py`, new `tests/test_netsuite_opportunity_items.py` (mock HTTP layer) |

## Open questions / decisions

1. **taxCode** — we rely on the item's default tax code. If some customers/items need `GST:EXPS` vs `GST:FREE`, decide later (sending taxCode currently breaks the API; needs investigation).
2. **Currency** — `amount` is in the opportunity's currency (AUD for the test customer). Confirm multi-currency behaviour is out of scope for now.
3. **UOM** — RFQ `uom` is not mapped to NetSuite units; item defaults apply.
4. **Selected supplier** — `custcol_po_vendor` needs the supplier's `netsuite_id`; define "selected" as the supplier with `quote_status == "selected"` in the item's `suppliers` JSONB. Omit vendor if none.
5. **Stale line removal** — not possible via REST today. Track `netsuite_line` per item and flag "removed items still on opportunity" in the UI; optionally test the undocumented per-line DELETE later.
6. **salesRep placeholder** ("local-tom") — accepted as-is for testing; defensive fix deferred (user decision).
7. **Product/item naming** — Phase 2/3 item creation needs rules for itemid collisions (case, suffix on duplicate) and for `custitem_brand` refs when no brand exists in NetSuite.
