# Plan: Request for Quote (RFQ) Workflow

## Overview
Build a structured Request for Quote (RFQ) workflow on top of the existing chat system. An RFQ tracks a customer's parts list through identification, supplier sourcing, and shortlisting — all managed via conversational interaction with the agent. This is distinct from the "Quotation" that Eagle Exports sends to their customer.

## Phased approach

### Phase 1: RFQ state in LangGraph Store + chat UI ✅ IMPLEMENTED
Validate the workflow and data model using existing infrastructure. No new tables, no new UI framework. The agent manages RFQ state via tools, renders markdown summaries, and offers action buttons for common operations.

### Phase 2: Dedicated database tables
When the data model stabilizes and cross-RFQ querying/reporting is needed, migrate to proper relational tables. This unlocks listing all RFQs, filtering by status/customer/date, and linking to existing Product/Supplier records.

### Phase 3: Companion dashboard app
Build a lightweight web app (FastAPI + HTMX) alongside Chainlit for traditional CRUD operations: RFQ list view, detail view with editable parts table, supplier shortlist management. Chat remains the AI-powered interface; dashboard provides the structured view.

---

## Phase 1 — Detailed plan

**Status: ✅ Complete** — Implemented 13 Apr 2026.

### What was built

#### Data model (LangGraph Store)

**Namespace:** `("rfqs",)` — shared across all users for cross-user visibility.

**Key:** Auto-generated sequential RFQ ID (e.g. `RFQ-2026-0042`)

**Value structure:**
```python
{
    "id": "RFQ-2026-0042",
    "customer": "Acme Construction",
    "customer_contact": {
        "name": "John Smith",
        "email": "john.smith@acme.com.au",
        "phone": "0412 345 678"
    },
    "reference": "CustRef-2026-123",     # customer's own reference if provided
    "netsuite_opportunity": null,         # NetSuite Opportunity Number (linked later)
    "hubspot_deal": null,                 # HubSpot Deal Number (linked later)
    "created_by": "tom@eagle.com.au",
    "created_date": "2026-04-13",
    "assigned_to": "tom@eagle.com.au",
    "thread_id": "5fd6701e-...",         # originating chat thread
    "status": "in_progress",             # draft | in_progress | awaiting_quotes | completed | cancelled
    "notes": "Urgent — needed by end of month",
    "items": [
        {
            "line": 1,
            "input_description": "Cordless drill - skin only",
            "input_code": "Makita DHP486Z 18V",
            "part_number": "DHP486Z",
            "brand": "Makita",
            "product_id": "uuid-...",    # FK to products table if matched
            "quantity": 4,
            "uom": "ea",
            "status": "confirmed",       # unidentified | identified | confirmed
            "suppliers": [
                {
                    "supplier_id": "uuid-...",   # FK to suppliers table, null if not in DB
                    "name": "Sydney Tools",
                    "contacts": [
                        {"type": "email", "value": "sales@sydneytools.com.au"},
                        {"type": "phone", "value": "02 9123 4567"},
                        {"type": "url", "value": "https://sydneytools.com.au"}
                    ],
                    "status": "previous_purchase",  # see supplier statuses below
                    "price": 189.00,
                    "lead_time": "3-5 business days",
                    "notes": "Best price so far"
                }
            ]
        }
    ],
    "history": [
        {"date": "2026-04-13T10:30:00+10:00", "user": "tom@eagle.com.au", "action": "Created RFQ with 30 items"},
        {"date": "2026-04-13T10:35:00+10:00", "user": "tom@eagle.com.au", "action": "Identified 28 of 30 items"}
    ]
}
```

**Supplier status values** (source-aware pricing):
- `candidate` — default, no price yet
- `estimated` — price from web search/estimate
- `previous_purchase` — price from purchase history
- `previous_quote` — price from a past quote
- `quoted` — new quote received (not yet implemented — Phase 1 does not include quote receipt workflow)
- `shortlisted` — user has shortlisted this supplier
- `selected` — final supplier selected
- `dropped` — removed from consideration

#### Agent tools (`includes/tools/quote_tools.py`)

**`manage_rfq(action, rfq_id, data)`** — Single tool for all RFQ mutations:
- `create` — create a new RFQ from a parts list
- `update_item` — identify a part, confirm it, change quantity, etc. Accepts `line`, `line_number`, or `item` as aliases for the line parameter
- `add_supplier` — add supplier candidate(s) to a line item. Accepts a single supplier (`name=...`) or a batch (`suppliers=[{...}, ...]`) to avoid race conditions from parallel tool calls
- `update_supplier` — change supplier status, update price/notes
- `assign` — reassign RFQ to another user
- `update_status` — change overall RFQ status
- `add_note` — append a note to the RFQ
- `link_external` — set NetSuite Opportunity Number or HubSpot Deal Number

Each mutation appends to the `history` array for audit trail and returns a rendered markdown summary.

**`get_rfq(rfq_id, list_all, assigned_to, status)`** — Read access:
- Single RFQ detail, list all, filter by assignee/status
- Default (no args) shows current user's RFQs
- Returns rendered markdown tables

Both tools are created via `create_quote_tools(store, user_id)` factory pattern (same as user_profile tools).

#### Chat UI

**RFQ summary block** — rendered by the tool after each state change:
```markdown
## 📋 RFQ-2026-0042 — Acme Construction
**Status:** In Progress | **Assigned to:** Tom | **Created:** 2026-04-13
**Contact:** John Smith · john.smith@acme.com.au

| # | Description | Part Number | Brand | Qty | Status | Suppliers |
|---|------------|-------------|-------|-----|--------|-----------|
| 1 | Cordless drill skin only | DHP486Z | Makita | 4 ea | ✅ Confirmed | Sydney Tools ($189.00 prev purchase) |
| 2 | Dumpy level | — | Topcon | 1 ea | ⚠️ Unidentified | — |

**2 items** | 1 confirmed, 1 unidentified | 1 with suppliers
```

Suppliers within a cell are separated by `<br>` for multi-supplier visibility. Dropped suppliers render with ~~strikethrough~~.

**New RFQ command** (📋 icon) — triggers intent context that guides the agent through RFQ creation.

**Natural language interactions** (all working):
- "Create an RFQ for Acme Construction from this screenshot"
- "Line 2 is the Topcon RL-H5A"
- "Find suppliers for all confirmed items"
- "Drop Total Tools from line 1 — no stock"
- "Assign this RFQ to Sarah"
- "Show me all open RFQs"
- "What RFQs does Sarah have?"
- "Link this to NetSuite opportunity 12345"
- "Set the HubSpot deal to D-9876"

#### Agent prompt behaviour

Shared `RFQ_WORKFLOW_PROMPT` constant in `includes/prompts.py` used by both ProcurementAgent and ResearchAgent — single source of truth.

Key behaviours:
- **After RFQ creation:** STOP and present summary for user to confirm customer details and line items. Do NOT auto-search for products.
- **After user confirms:** Search for products/suppliers and immediately update the RFQ — don't just present results and wait to be asked.
- **Batch supplier adds:** Use `suppliers` list to add all suppliers for a line in one call (avoids race condition from parallel tool calls overwriting each other).
- **Price source tagging:** Set supplier status to `previous_purchase`, `estimated`, `previous_quote` etc. based on where the price came from.
- **Always present summary:** After every RFQ mutation, show the updated summary to the user.

#### Files created
- `includes/tools/quote_tools.py` — tool implementations, rendering, ID generation

#### Files modified
- `includes/prompts.py` — added `RFQ_WORKFLOW_PROMPT` shared constant, `new_rfq` intent in `INTENTS`, RFQ section in `build_research_prompt()`
- `includes/actions.py` — added `handle_new_rfq` action handler
- `includes/agents/procurement_agent.py` — registered RFQ tools in `get_tools()`, appended `RFQ_WORKFLOW_PROMPT` to system prompt
- `includes/agents/research_agent.py` — registered RFQ tools in `get_tools()`

#### Tests (36 tests in `tests/tools/test_quote_tools.py`)
- Create: basic, sequential IDs, requires customer, default draft status, history, customer contact, items default unidentified
- Update item: set part number/brand/status, missing line error, invalid line error, `line_number` alias
- Suppliers: add single, add batch (multiple in one call), update status/price, not found error, missing name error
- Misc: assign, update status (valid + invalid), add note (single + append), link NetSuite, link HubSpot, empty link error, unknown action, RFQ not found, history accumulation
- Get: single, not found, list all, filter by status, filter by assigned_to, default shows my RFQs
- Rendering: supplier price with source label, estimated label, customer contact display

### Known issues / refinements discovered during implementation

1. **Embedding model change:** Default changed from `gemini-embedding-2-preview` (not available on Vertex AI) to `text-embedding-005`. Existing product/supplier embeddings need re-generation — deferred to a separate task.
2. **Action buttons not yet implemented:** The plan mentioned action buttons (Show Summary, Find Suppliers, Export CSV, Confirm All) sent with the summary. These are not yet built — the agent responds to natural language requests instead.
3. **CSV export not yet implemented:** The plan mentioned export functionality. Not built in Phase 1.
4. **Tool call budget vs large RFQs:** When searching for products across many RFQ line items, the agent may hit the 5-tool-call budget before processing all items. May need a batch product lookup tool (see `plan-batchProductLookupTools.prompt.md`).

### Remaining Phase 1 enhancements (optional)

- [ ] Action buttons on RFQ summary (Show Summary, Find Suppliers, Export CSV)
- [ ] CSV export tool for RFQ data
- [ ] Batch product lookup to handle large RFQs efficiently
- [ ] Auto-link RFQ to current thread_id on creation

---

## Phase 2 — Database tables (future)

Trigger: When you need cross-RFQ reporting, complex queries, or the Store JSON approach becomes unwieldy.

### New tables
```sql
rfqs (
    id UUID PK,
    rfq_number VARCHAR UNIQUE,    -- RFQ-2026-0042
    customer VARCHAR,
    customer_contact JSONB,
    reference VARCHAR,
    netsuite_opportunity VARCHAR,  -- NetSuite Opportunity Number
    hubspot_deal VARCHAR,          -- HubSpot Deal Number
    created_by VARCHAR,
    assigned_to VARCHAR,
    thread_id VARCHAR,
    status VARCHAR,
    notes TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
)

rfq_items (
    id UUID PK,
    rfq_id UUID FK → rfqs,
    line_number INT,
    input_description TEXT,
    input_code VARCHAR,
    product_id UUID FK → products (nullable),
    quantity FLOAT,
    uom VARCHAR,
    status VARCHAR,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
)

rfq_item_suppliers (
    id UUID PK,
    rfq_item_id UUID FK → rfq_items,
    supplier_id UUID FK → suppliers (nullable),
    name VARCHAR,
    contacts JSONB,
    status VARCHAR,
    price FLOAT,
    lead_time VARCHAR,
    notes TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
)

rfq_history (
    id UUID PK,
    rfq_id UUID FK → rfqs,
    user_id VARCHAR,
    action TEXT,
    created_at TIMESTAMP
)
```

Migration from Phase 1: Read all RFQs from Store, insert into new tables, update tools to use SQLAlchemy models instead of Store.

---

## Phase 3 — Companion dashboard (future)

Trigger: When users need to view/edit RFQs outside the chat, or when you need a proper RFQ list with sorting/filtering/pagination.

### Tech stack
- FastAPI + HTMX + Jinja2 templates (Python-native, no separate JS build)
- Shares SQLAlchemy models and PostgreSQL with Chainlit app
- Same Google OAuth for authentication
- Deployed alongside Chainlit in the same Docker Compose / Railway service

### Key views
- `/app/rfqs` — list all RFQs, filterable by status/customer/assignee/date
- `/app/rfqs/{id}` — detail view with editable parts table and supplier shortlist
- `/app/rfqs/{id}/export` — CSV/PDF export
- Each RFQ detail page links to the Chainlit thread: `/chat/thread/{thread_id}`

### Architecture
```
docker-compose.yml:
  eagleagent:        # Chainlit app (port 8000)
  eagleagent-dash:   # Dashboard app (port 3000)
  postgres:          # Shared database
  nginx:             # Reverse proxy
    /chat  → eagleagent:8000
    /app   → eagleagent-dash:3000
```
