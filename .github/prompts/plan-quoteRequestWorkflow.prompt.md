# Plan: Request for Quote (RFQ) Workflow

## Overview
Build a structured Request for Quote (RFQ) workflow on top of the existing chat system. An RFQ tracks a customer's parts list through identification, supplier sourcing, and shortlisting — all managed via conversational interaction with the agent. This is distinct from the "Quotation" that Eagle Exports sends to their customer.

## Phased approach

### Phase 1: RFQ state in LangGraph Store + chat UI
Validate the workflow and data model using existing infrastructure. No new tables, no new UI framework. The agent manages RFQ state via tools, renders markdown summaries, and offers action buttons for common operations.

### Phase 2: Dedicated database tables
When the data model stabilizes and cross-RFQ querying/reporting is needed, migrate to proper relational tables. This unlocks listing all RFQs, filtering by status/customer/date, and linking to existing Product/Supplier records.

### Phase 3: Companion dashboard app
Build a lightweight web app (FastAPI + HTMX) alongside Chainlit for traditional CRUD operations: RFQ list view, detail view with editable parts table, supplier shortlist management. Chat remains the AI-powered interface; dashboard provides the structured view.

---

## Phase 1 — Detailed plan

### Data model (LangGraph Store)

**Namespace:** `("rfqs",)` — shared across all users for cross-user visibility.

**Key:** Auto-generated RFQ ID (e.g. `RFQ-2026-0042`)

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
                    "status": "shortlisted",  # candidate | shortlisted | dropped | selected
                    "price": 189.00,
                    "lead_time": "3-5 business days",
                    "notes": "Best price so far"
                },
                {
                    "supplier_id": null,         # not in our database yet
                    "name": "ToolMart Online",
                    "contacts": [
                        {"type": "url", "value": "https://toolmart.com.au"},
                        {"type": "email", "value": "quotes@toolmart.com.au"}
                    ],
                    "status": "candidate",
                    "price": null,
                    "lead_time": null,
                    "notes": "Found via web search"
                }
            ]
        },
        {
            "line": 2,
            "input_description": "Dumpy level",
            "input_code": "Topcon brand",
            "part_number": null,
            "brand": "Topcon",
            "product_id": null,
            "quantity": 1,
            "uom": "ea",
            "status": "unidentified",
            "suppliers": []
        }
    ],
    "history": [
        {"date": "2026-04-13T10:30:00", "user": "tom@eagle.com.au", "action": "Created RFQ with 30 items"},
        {"date": "2026-04-13T10:35:00", "user": "tom@eagle.com.au", "action": "Identified 28 of 30 items"},
        {"date": "2026-04-14T09:00:00", "user": "tom@eagle.com.au", "action": "Assigned to sarah@eagle.com.au"}
    ]
}
```

### Agent tools

Two new tools registered on `ProcurementAgent` and `ResearchAgent`:

**`manage_rfq(action, rfq_id, data)`**
A single tool handling all RFQ mutations:
- `create` — create a new RFQ from a parts list (items can be vague descriptions or identified products)
- `update_item` — identify a part, confirm it, change quantity, etc.
- `add_supplier` — add a supplier candidate to a line item
- `update_supplier` — change supplier status (shortlist, drop, select) or update price/notes
- `assign` — reassign RFQ to another user
- `update_status` — change overall RFQ status
- `add_note` — append a note to the RFQ
- `link_external` — set NetSuite Opportunity Number or HubSpot Deal Number

Each mutation appends to the `history` array for audit trail.

**`get_rfq(rfq_id, list_all, assigned_to, status)`**
Read access:
- `get_rfq(rfq_id="RFQ-2026-0042")` — full detail of one RFQ
- `get_rfq(list_all=True)` — summary list of all RFQs
- `get_rfq(assigned_to="tom@eagle.com.au")` — RFQs for a specific user
- `get_rfq(status="in_progress")` — filter by status

Returns rendered markdown: summary table for listings, full detail with parts + suppliers for single RFQ.

### Chat UI patterns

**RFQ summary block** — rendered by the agent after each state change:
```markdown
## 📋 RFQ-2026-0042 — Acme Construction
**Status:** In Progress | **Assigned to:** Tom | **Created:** 13 Apr 2026

| # | Description | Part Number | Brand | Qty | Status | Suppliers |
|---|------------|-------------|-------|-----|--------|-----------|
| 1 | Cordless drill skin only | DHP486Z | Makita | 4 | ✅ Confirmed | Sydney Tools ($189), ~~Total Tools~~ |
| 2 | Dumpy level | — | Topcon | 1 | ⚠️ Unidentified | — |
| ... | ... | ... | ... | ... | ... | ... |

**30 items** | 28 confirmed, 2 unidentified | 15 with suppliers
```

**Action buttons** — sent with the summary:
- 📋 Show RFQ Summary
- 🔍 Find Suppliers for Unconfirmed Items
- 📧 Export RFQ (CSV)
- ✅ Confirm All Identified Items

**Natural language interactions:**
- "Create an RFQ for Acme Construction from this screenshot"
- "Line 2 is the Topcon RL-H5A"
- "Find suppliers for all confirmed items"
- "Drop Total Tools from line 1 — no stock"
- "Assign this RFQ to Sarah"
- "Show me all open RFQs"
- "What RFQs does Sarah have?"
- "Link this to NetSuite opportunity 12345"
- "Set the HubSpot deal to D-9876"

### ProcurementAgent prompt updates

Add an "RFQ Management Workflow" section:
- When user provides a list of products (screenshot, pasted text), offer to create an RFQ
- After product identification, auto-update RFQ items from `unidentified` → `identified`
- After supplier search, offer to add results as candidates on the relevant RFQ items
- After each mutation, re-render the RFQ summary
- When user says "show RFQ" or "show my RFQs", use `get_rfq` tool

### New Chainlit command

Add a "New RFQ" command to the commands list:
- Icon: 📋
- Follow-up: "I'll create a new Request for Quote. Who is the customer, and do you have a parts list (screenshot, text, or document)?"
- Intent context: routes to ProcurementAgent with RFQ creation workflow

### Cross-user and cross-profile visibility

- Shared namespace `("rfqs",)` means any user's agent can list/read/update any RFQ
- `assigned_to` field tracks ownership; agent filters by current user by default, shows all when asked
- `history` array provides audit trail of who changed what
- **Both profiles have full access**: Eagle Agent (via ProcurementAgent) and Research Agent both get `manage_rfq` and `get_rfq` tools. This allows the Research Agent to update RFQs with newly found suppliers or confirmed product IDs from web research.
- Limitation: users cannot see each other's chat threads — only the structured RFQ data

### Files to modify
- `includes/tools/product_tools.py` — add `manage_rfq` and `get_rfq` tools (or new file `includes/tools/quote_tools.py`)
- `includes/agents/procurement_agent.py` — register RFQ tools, update system prompt with RFQ workflow
- `includes/agents/research_agent.py` — register RFQ tools, update system prompt with RFQ workflow (same tools, different usage guidance — e.g. "update RFQ with supplier found from web research")
- `includes/prompts.py` — add "New RFQ" intent to `INTENTS`, add RFQ workflow section to research prompt
- `includes/actions.py` — add action handler for new RFQ command

### Files to create
- `includes/tools/quote_tools.py` — RFQ management tool implementations

### Tests
- Unit tests for `manage_rfq` (create, update, supplier ops, assign, link external IDs)
- Unit tests for `get_rfq` (single, list, filter)
- Integration test: create RFQ → identify items → add suppliers → shortlist → export

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
