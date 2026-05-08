# Plan: RFQ Data & Workflow Enhancements

## Overview

Improve the RFQ system across five areas: migrate storage to SQL for data coherence and proper querying, surface pricing data more effectively, add intelligent supplier ranking, streamline the user workflow, and generate quote request emails. RFQs move from the LangGraph BaseStore into PostgreSQL tables alongside the existing dashboard data (`suppliers`, `products`, `transactions`).

---

## Phase 1: Migrate RFQ Storage from LangGraph Store to SQL

**Goal:** Move RFQs from the LangGraph `BaseStore` (key-value, coupled to chat infrastructure) into proper SQL tables alongside the rest of the dashboard data (`suppliers`, `products`, `transactions`). This gives us backup coherence, real SQL querying, PostgreSQL row-level locking, and referential integrity.

### 1. Create SQLAlchemy models ✅

Add `RFQ` and `RFQItem` models to `includes/dashboard/models.py`:

**`RFQ`** (`rfqs` table):
- `id` — UUID primary key
- `rfq_number` — String, unique, indexed (e.g. `RFQ-2026-0042`)
- `customer` — String, not null
- `customer_contact` — JSONB, nullable (`{name, email, phone}`)
- `reference` — String, nullable
- `netsuite_opportunity` — String, nullable
- `hubspot_deal` — String, nullable
- `created_by` — String
- `created_date` — Date
- `assigned_to` — String
- `thread_id` — String, nullable
- `status` — String (draft / in_progress / awaiting_quotes / completed / cancelled)
- `notes` — Text, nullable
- `history` — JSONB (`[{date, user, action}, ...]`)
- `updated_at` — DateTime with timezone

**`RFQItem`** (`rfq_items` table):
- `id` — UUID primary key
- `rfq_id` — UUID FK → `rfqs.id`, indexed, not null
- `line` — Integer, not null
- `input_description` — Text
- `input_code` — String, nullable
- `part_number` — String, nullable
- `brand` — String, nullable
- `product_id` — UUID FK → `products.id`, nullable
- `quantity` — Integer, nullable
- `uom` — String, default `"ea"`
- `status` — String (unidentified / identified / confirmed / review)
- `notes` — Text, nullable
- `suppliers` — JSONB (`[{name, supplier_id, price, price_type, contacts, ...}, ...]`)

Add a `UniqueConstraint('rfq_id', 'line')` on `rfq_items`.

### 2. Create Alembic migration ✅

- Add migration for both tables with foreign keys and indexes
- Run against local and production databases

### 3. Rewrite `quote_tools.py` — swap BaseStore → SQL ✅

Replace all `store.aget()` / `store.aput()` / `store.asearch()` calls with SQLAlchemy queries:
- `create` → `INSERT` into `rfqs` + `rfq_items`
- `update` → `UPDATE rfqs SET ...`
- `update_item` / `add_supplier` / `update_supplier` / `clear_suppliers` → `UPDATE rfq_items SET ... WHERE rfq_id = ? AND line = ?`
- `get` → `SELECT` with optional `JOIN` to `rfq_items`
- `_next_rfq_number()` → `SELECT MAX(rfq_number) FROM rfqs WHERE ...`
- Remove the `_rfq_locks` dict, `get_rfq_lock()`, `_items_ns()`, `_get_rfq_items()`, `_put_rfq_item()`, `_assemble_rfq()`, and `_migrate_rfq_if_needed()` — all replaced by standard SQL operations
- Remove the `NAMESPACE` constant and BaseStore dependency
- Tool functions no longer need `store` parameter — use `get_session()` from the database module
- Use `asyncio.to_thread()` for DB calls (same pattern as `product_tools.py`)

### 4. Rewrite dashboard routes ✅

- All RFQ routes use SQLAlchemy queries instead of `store.aget/aput/asearch`
- `_fetch_rfqs()` → SQL query with `WHERE` filters, `ORDER BY`, `LIMIT/OFFSET`
- `rfq_detail` → `SELECT rfq JOIN rfq_items`
- Mutation endpoints → direct `UPDATE` on the appropriate table
- Remove `_get_store()` helper from RFQ routes
- `_normalize_rfq_suppliers()` and `_enrich_rfq_supplier_contacts()` work on the JSONB suppliers field (same logic, just read from `RFQItem.suppliers` instead of dict)

### 5. Update `create_quote_tools()` signature ✅

- Remove `store` parameter — tools get their own DB sessions
- Update all call sites (agent creation in `procurement_agent.py`, etc.) to stop passing `store`

### 6. Data migration script ✅

- Create `scripts/migrate_rfqs_to_sql.py`:
  - Read all RFQs from the LangGraph BaseStore (both v1 and v2 formats)
  - Insert into the new SQL tables
  - Print summary: migrated count, skipped count
- Run once after deploying the new code

### 7. Update tests ✅

- Replace `test_store` (InMemoryStore) fixture with DB session fixture for RFQ tests
- All test assertions query the `rfqs` / `rfq_items` tables directly
- Verify FK constraints work (e.g. `product_id` references `products.id`)

---

## Phase 2: Enhanced Pricing Display

**Goal:** Surface cost price, sale price, and historical pricing data in the RFQ UI so users can make informed decisions without leaving the page.

### 1. Enrich supplier data with cost & sale prices ✅

When adding a supplier to an RFQ item (via `add_supplier` or during Phase 2 identification), query the `Transaction` table for that product+supplier combination and include:
- `cost_price` — most recent cost from a Purchase Order transaction
- `sale_price` — most recent sale price from a Sales Order transaction
- `quote_price` — most recent price from a Quote transaction
- `last_purchase_date` — date of most recent purchase
- `purchase_count` — number of historical orders for this product+supplier pair

Update the supplier dict within each RFQ item to carry these fields.

Also auto-resolves `product_id` on the line item when missing but `part_number` matches a product in the DB — ensures pricing enrichment and history lookups work even when the identification step was skipped.

### 2. Update `_render_rfq_summary` (agent markdown) ✅

Add cost and sale price columns to the supplier display within the markdown table. Show:
- `Cost: $X.XX` — last purchase cost
- `Sale: $X.XX` — last sale price  
- `Margin: XX%` — calculated if both cost and sale are available

### 3. Update RFQ detail template ✅

Modify the supplier table in `templates/partials/rfq_detail.html` to add columns:
- **Cost** — `$X.XX` from last purchase order (clickable to reveal price history)
- **Sale** — `$X.XX` from last sales order
- **Margin** — calculated percentage, colour-coded (green ≥30%, amber 15-30%, red <15%)

Also replaced `[DB]` text badges with blue database cylinder icons (Heroicons circle-stack) for both product part numbers and supplier names.

### 4. Add price history popover ✅

Cost price column is clickable when the supplier has a `supplier_id` and the item has a `product_id`. Clicking reveals a popover with:
- Last 5 transactions for this product+supplier pair
- Each row: date, doc type (PO/SO/Quote), doc number, quantity, cost, sale price
- Source: `/partial/rfqs/price-history` endpoint, also accepts `part_number` as fallback

### 5. Update `part_purchase_history` tool ✅

Extend the tool output to include both cost and sale prices per supplier:
- Add `cost` column alongside existing `price` column
- Add `margin` calculation where both values exist
- This gives the agent better data to work with when recommending suppliers

---

## Phase 3: Supplier Ranking

**Goal:** When an RFQ item has multiple candidate suppliers, sort them from "best" to "worst" using a scoring model that can evolve over time.

### 1. Define initial scoring criteria

Create a scoring function in `includes/tools/quote_tools.py` (or a new `includes/tools/supplier_scoring.py`) that evaluates each supplier for a given item:

| Factor | Weight | Logic |
|--------|--------|-------|
| **Purchase history** | 30% | Has the supplier supplied this exact product before? More orders = higher score |
| **Recency** | 15% | How recently did we last buy from them? More recent = higher score |
| **Price competitiveness** | 25% | Lower cost relative to other candidates = higher score |
| **Brand match** | 15% | Is the supplier linked to this product's brand in `supplier_brands`? |
| **Supply chain position** | 10% | Prefer manufacturers/authorized distributors over brokers (from `supply_chain_position`) |
| **Location** | 5% | Prefer domestic (Australia) suppliers for shorter lead times |

Each factor produces a 0–100 score. The weighted total gives an overall ranking score.

### 2. Add a `rank_suppliers` action to `manage_rfq`

- New action: `manage_rfq(action='rank_suppliers', rfq_id='...', data={line: N})`
- Reads the item's supplier list, scores each supplier, sorts by score descending
- Updates the supplier list order and adds a `score` field to each supplier dict
- Returns the ranked list with scores and reasoning
- If `line` is omitted, rank suppliers on ALL items

### 3. Agent integration

- After the supplier search phase, the agent automatically calls `rank_suppliers` on each item
- The agent can explain the ranking: "I've ranked the suppliers. ABC Industrial scores highest because they've supplied this exact part 12 times at competitive prices and are an authorized Caterpillar distributor."
- Users can override rankings by manually changing supplier status (shortlisted/selected)

### 4. UI display

- Show rank number and score badge on each supplier row in the RFQ detail template
- Add a "Rank Suppliers" button per item (or globally) that triggers re-ranking via HTMX
- Colour-code scores: green (≥70), amber (40–69), red (<40)

### 5. Evolve the model

- Store the scoring weights in `config/settings.py` so they can be tuned without code changes
- Track which suppliers are ultimately `selected` — this data can later feed back into improving the weights
- Future: add supplier reliability metrics (on-time delivery, quality issues) as new data becomes available

---

## Phase 4: Workflow UI Improvements

**Goal:** Make it easier for users to progress an RFQ through the standard workflow: create → identify items → find suppliers → rank → select → request quotes.

### 1. Add workflow progress indicator

Add a visual stepper/progress bar to the RFQ detail page header showing:

1. **Created** — RFQ exists with items
2. **Items Identified** — all items have status `confirmed` or `identified`
3. **Suppliers Found** — all items have at least one supplier candidate
4. **Suppliers Ranked** — ranking has been run
5. **Suppliers Selected** — at least one supplier per item has status `selected`
6. **Quotes Requested** — quote request emails generated (Phase 5)

Highlight the current step. Steps auto-advance based on RFQ data state.

### 2. Add contextual action buttons

Based on the current workflow step, show prominent action buttons:

- **Step 1 → 2:** "Identify Items" button → sends a message to the agent: "Please identify all unidentified items in RFQ-2026-0042"
- **Step 2 → 3:** "Find Suppliers" button → sends: "Please find suppliers for all items in RFQ-2026-0042 that don't have suppliers yet"
- **Step 3 → 4:** "Rank Suppliers" button → calls `manage_rfq(action='rank_suppliers')` via HTMX
- **Step 4 → 5:** Manual — user reviews rankings and sets supplier statuses to `selected`
- **Step 5 → 6:** "Generate Quote Requests" button → triggers Phase 5 email generation

### 3. Bulk status actions

Add checkboxes to the items table allowing users to:
- Select multiple items and set all their suppliers to a given status
- Select multiple items and trigger "Find Suppliers" for just those items
- Select all unidentified items for batch identification

### 4. Filter and sort controls

Add controls to the items table:
- **Filter by status:** Show only unidentified / identified / confirmed / review items
- **Filter by supplier count:** Show items with no suppliers / items with suppliers
- **Sort by:** Line number, status, supplier count, price (lowest first)

---

## Phase 5: Quote Request Email Generation

**Goal:** Generate draft quote request emails to selected suppliers, grouping all relevant items per supplier into a single email.

### 1. Build the email generation logic

Create `includes/tools/quote_email.py` with a function `generate_quote_requests(rfq_id, store)`:

- Read the RFQ header and all items
- Find all suppliers with status `selected` across all items
- Group items by supplier: for each selected supplier, collect all the items they're linked to
- For each supplier, generate a draft email containing:
  - **To:** supplier contact email (from supplier's `contacts` in the DB)
  - **Subject:** `Quote Request — {RFQ ID} — {Customer Name}`
  - **Body:**
    - Greeting with supplier contact name
    - Brief intro: "We're preparing a quote for our customer and would appreciate pricing on the following items:"
    - Table of items: line number, description, part number, brand, quantity, UOM
    - Request for: unit price, lead time, minimum order quantity, availability
    - Preferred currency (AUD)
    - Closing with Eagle Exports contact details
  - **Metadata:** supplier_id, item line numbers included, generated timestamp

### 2. Add `generate_quotes` action to `manage_rfq`

- New action: `manage_rfq(action='generate_quotes', rfq_id='...')`
- Calls the email generation logic
- Stores the generated emails in the RFQ header under a `quote_requests` key:
  ```python
  "quote_requests": [
      {
          "supplier_id": "uuid-...",
          "supplier_name": "ABC Industrial",
          "to_email": "sales@abcindustrial.com",
          "to_name": "Jane Smith",
          "subject": "Quote Request — RFQ-2026-0042 — Acme Construction",
          "body": "...",
          "items": [1, 3, 7],  # line numbers
          "generated_at": "2026-05-08T14:30:00+10:00",
          "status": "draft"  # draft | sent | responded
      }
  ]
  ```
- Returns a summary: "Generated 5 quote request emails covering 12 items across 5 suppliers"

### 3. Add email review UI

Add a "Quote Requests" tab/section to the RFQ detail page:
- List of generated email drafts, one card per supplier
- Each card shows: supplier name, contact email, item count, generated date
- Expandable to show full email body
- "Copy to Clipboard" button for each email — copies the formatted email body
- Status badge: Draft / Sent / Responded
- "Mark as Sent" button to update status after user sends the email manually

### 4. Agent integration

- The agent can generate quotes via the tool: "Generate quote request emails for RFQ-2026-0042"
- The agent can also generate a single email for a specific supplier if asked
- After generation, the agent provides a summary and directs the user to the Quote Requests section in the dashboard

### 5. Email template configuration

- Store the email template in `config/prompts.yaml` so it can be customized without code changes
- Support template variables: `{supplier_name}`, `{supplier_contact}`, `{rfq_id}`, `{customer}`, `{items_table}`, `{sender_name}`, `{sender_email}`
- Allow per-RFQ customization of the intro/closing text via the RFQ notes or a dedicated field

---

## Relevant Files

- `includes/dashboard/models.py` — RFQ + RFQItem models, plus existing Transaction/Supplier/Product models (Phase 1)
- `includes/tools/quote_tools.py` — RFQ management tools (Phase 1 rewrite, all phases)
- `includes/tools/product_tools.py` — Supplier search, purchase history tools
- `includes/tools/supplier_scoring.py` — New: supplier ranking logic (Phase 3)
- `includes/tools/quote_email.py` — New: email generation (Phase 5)
- `includes/dashboard/routes.py` — RFQ dashboard endpoints (all phases)
- `includes/agents/procurement_agent.py` — Agent wiring, passes store/tools (Phase 1)
- `includes/prompts.py` — RFQ workflow prompt updates (Phases 3, 4)
- `templates/partials/rfq_detail.html` — RFQ detail template (Phases 2, 3, 4, 5)
- `config/settings.py` — Scoring weights (Phase 3)
- `config/prompts.yaml` — Email template (Phase 5)
- `alembic/versions/` — Migration for rfqs + rfq_items tables (Phase 1)
- `scripts/migrate_rfqs_to_sql.py` — One-time data migration from BaseStore (Phase 1)
- `tests/tools/test_quote_tools.py` — RFQ test suite (Phase 1 rewrite)

## Dependencies

- **Phase 1** has no dependencies — can start immediately
- **Phase 2** depends on Phase 1 (SQL storage) and NetSuite transaction data being imported
- **Phase 3** depends on Phase 2 (needs pricing data to score)
- **Phase 4** depends on Phases 2 and 3 (workflow steps reference ranking and pricing)
- **Phase 5** depends on Phase 4 (the "Generate Quote Requests" button is the final workflow step)

Phases 1 and 2 can proceed in parallel if transaction data is already available.
