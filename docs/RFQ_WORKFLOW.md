# RFQ (Request for Quote) Workflow

EagleAgent manages the full RFQ lifecycle — from creation through supplier matching, quote collection, and status tracking. RFQs are stored in PostgreSQL and accessible from both the dashboard and the chat agent.

## Architecture

```
Chat / Dashboard
      │
      ▼
includes/tools/
  ├── quote_tools.py   — LangGraph @tool wrappers (agent-facing API)
  ├── rfq_crud.py      — Sync DB operations (create, read, update, delete)
  └── rfq_render.py    — Markdown/HTML rendering for Chat UI display
      │
      ▼
includes/chat/
  └── rfq_actions.py   — Chainlit action callbacks (UI buttons)
      │
      ▼
includes/dashboard/routes/
  └── rfqs.py          — HTMX dashboard views and partials
      │
      ▼
PostgreSQL: rfqs + rfq_items + rfq_suppliers + rfq_threads tables
```

## RFQ Lifecycle

```
1. CREATE         2. IDENTIFY ITEMS       3. FIND SUPPLIERS
   │                   │                       │
   ▼                   ▼                       ▼
New RFQ-2026-0042   Parse/Search items     Match suppliers
Status: DRAFT       from description       by brand/category
                    or product DB               │
                                          ▼
                                    4. SEND QUOTES
                                        │
                                        ▼
                                    5. COLLECT RESPONSES
                                        │
                                   ┌────┴────┐
                                   ▼         ▼
                            6. COMPARE    7. AWARD / CLOSE
                            QUOTES
```

### 1. Create RFQ

- **From Chat**: Agent creates RFQ via `create_rfq` tool — generates sequential RFQ number (e.g., `RFQ-2026-0042`)
- **From Dashboard**: Admin creates RFQ via the `/rfqs` page
- RFQ starts in `DRAFT` status

### 2. Identify Items

- Agent parses the RFQ description to identify line items
- Items can be matched against the `products` table by part number, description, or brand
- Each item gets a line number, part number, description, and quantity

### 3. Find Suppliers

The agent uses multiple strategies to find suitable suppliers:

1. **Brand match** — If items have a known brand, find suppliers carrying that brand
2. **Category match** — Match supplier categories to item categories
3. **Product history** — Find suppliers from past purchase transactions
4. **Previous RFQ suppliers** — Reuse suppliers from similar past RFQs
5. **Web search** — Fall back to ResearchAgent for new supplier discovery

### 4. Send Quotes

- **From Dashboard**: Admin can send quote request emails to suppliers via the email modal
- **Dual-path workflow**: In-app editor (direct Gmail API send) or Gmail draft handoff
- Each sent email is tracked in `email_tracking` with RFQ and supplier references

### 5. Collect & Compare Responses

- Supplier responses are linked to the RFQ via email tracking
- Admin can record supplier prices against each line item
- The dashboard shows a comparison view of all supplier quotes

### 6. Award / Close

- RFQ status can be updated: `DRAFT` → `SENT` → `RECEIVED` → `AWARDED` → `CLOSED`
- Awarded RFQ details are stored for future reference

## Tool Reference

All tools are available to the `ProcurementAgent` and `ResearchAgent` (when `include_rfq_tools=True`):

### RFQ Management

| Tool | Description |
|---|---|
| `create_rfq` | Create a new RFQ with title and description |
| `get_rfq` | Retrieve an existing RFQ by number |
| `update_rfq` | Update RFQ metadata (title, description, status, assignee) |
| `list_rfqs` | List RFQs with optional status/user filters |

### Item Management

| Tool | Description |
|---|---|
| `add_items` | Add line items to an RFQ (part number, description, quantity) |
| `update_item` | Update an existing line item |
| `delete_item` | Remove a line item |
| `identify_items` | Parse RFQ description to auto-identify items from product DB |
| `group_items` | Group items into sections for the quote request |

### Supplier Management

| Tool | Description |
|---|---|
| `add_supplier` | Add a supplier to an RFQ |
| `update_supplier` | Update supplier quote details (price, currency, notes) |
| `clear_suppliers` | Remove all suppliers from an RFQ |
| `find_suppliers` | Search for matching suppliers by item brand/category |
| `find_new_suppliers` | Discover new suppliers via web research |

### Workflow

| Tool | Description |
|---|---|
| `update_status` | Advance RFQ status (DRAFT → SENT → RECEIVED → AWARDED → CLOSED) |
| `assign` | Assign RFQ to a team member |
| `add_note` | Add internal notes to an RFQ |
| `link_external` | Link an external reference (email, document URL) |

## Rendering

RFQ data is rendered in two contexts:

- **Chat UI**: `rfq_render.py` produces Markdown summaries with supplier comparison tables and item lists. Rendered via Chainlit `Msg` elements.
- **Dashboard**: `templates/rfq_detail.html` provides a full HTMX page with supplier cards, item table, email history, and action buttons.

## Dashboard Views

| Route | View | Description |
|---|---|---|
| `/rfqs` | List | All RFQs with search, status filter, pagination |
| `/rfqs/{id}` | Detail | Full RFQ with items, suppliers, emails, notes |
| `/partial/rfqs` | Fragment | HTMX partial for list updates |
| `/partial/rfqs/{id}` | Fragment | HTMX partial for detail updates |

## Thread Binding

Each RFQ can be bound to a Chainlit conversation thread via `rfq_threads` table. This enables:

- **Dashboard → Chat**: Clicking an RFQ in the dashboard opens the bound chat thread
- **Chat → Dashboard**: Agent operations on an RFQ notify the dashboard to refresh
- **Thread pinning**: Long-running RFQ workflows stay on the same thread across sessions

## Data Model

```
rfqs
  ├── id (UUID, PK)
  ├── rfq_number (String, unique)     — e.g., "RFQ-2026-0042"
  ├── title (String)
  ├── description (Text)
  ├── status (String)                 — DRAFT | SENT | RECEIVED | AWARDED | CLOSED
  ├── assigned_to (String)            — User email
  ├── customer_name (String)
  ├── created_at, updated_at (DateTime)

rfq_items
  ├── id (UUID, PK)
  ├── rfq_id (FK → rfqs.id)
  ├── line_number (Integer)
  ├── part_number (String)
  ├── description (Text)
  ├── quantity (Integer)
  ├── unit (String)
  └── item_group (String)             — Section/group name for grouping

rfq_suppliers
  ├── id (UUID, PK)
  ├── rfq_id (FK → rfqs.id)
  ├── supplier_id (FK → suppliers.id)
  ├── price (Float)
  ├── currency (String)
  ├── notes (Text)
  └── status (String)                 — PENDING | QUOTED | DECLINED

rfq_threads
  ├── rfq_number (String, PK)
  ├── user_email (String, PK)
  ├── thread_id (String)
  └── created_at (DateTime)

email_tracking
  ├── rfq_id (String)
  ├── supplier_id (FK → suppliers.id)
  ├── direction (String)              — OUTBOUND | INBOUND
  └── ... (message tracking fields)
```
