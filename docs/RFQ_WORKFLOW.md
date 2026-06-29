# RFQ (Request for Quote) Workflow

EagleAgent manages the full RFQ lifecycle — from creation through supplier matching, quote collection, and status tracking. RFQs are stored in PostgreSQL and accessible from both the dashboard and the chat agent.

## Architecture

```
Chat / Dashboard
      │
      ▼
includes/tools/
  ├── quote_tools.py   — LangGraph @tool wrappers (agent-facing API)
  ├── rfq_crud.py      — Sync DB operations + shared pipeline functions
  └── rfq_render.py    — Markdown/HTML rendering for Chat UI display
      │
      ▼
includes/chat/
  └── rfq_actions.py   — Chainlit action callbacks (UI button handlers)
      │
      ▼
includes/agents/
  └── procurement_agent.py  — ProcurementAgent + _try_find_suppliers_pipeline
      │
      ▼
includes/dashboard/routes/
  └── rfqs.py          — HTMX dashboard views and partials
      │
      ▼
PostgreSQL: rfqs + rfq_items + rfq_suppliers + rfq_threads tables
```

## Unified Code Path

The batch "Find All Suppliers" button and the chat message "find suppliers" now invoke **identical code**. The button creates a synthetic user message and routes it through the graph — the same `_try_find_suppliers_pipeline` runs regardless of how the workflow is triggered.

```
Button click                        Chat message
    │                                    │
    ▼                                    ▼
synthetic cl.Message               user types message
("Find suppliers for all               │
 items on RFQ-2026-XXXX")              ▼
    │                            @cl.on_message (main)
    ▼                                    │
_main_pinned(synthetic, tid)             ▼
    │                            Supervisor routes to
    ▼                            ProcurementAgent
@cl.on_message (main)                    │
    │                                    ▼
    ▼                            _try_find_suppliers_pipeline()
Supervisor routes to                     │
ProcurementAgent                   7-step pipeline runs
    │                                    │
    ▼                                    ▼
_try_find_suppliers_pipeline()    IDENTICAL OUTCOME
```

## The 7-Step Find-Suppliers Pipeline

When the user asks to find suppliers (via button or chat), the pipeline runs these steps in order. **Each step streams progress to the user — the agent never goes silent.**

| Step | Name | What It Does | User Sees |
|------|------|-------------|-----------|
| 1 | **Classify** | Assigns match level to all unmatched items: `specific` (part# + description), `branded` (brand + description), `generic` (description only). Also searches product DB. | "Classified 8 items: 4 specific, 2 branded, 2 generic. 3 found in product DB." |
| 2 | **Validate** | Web-checks specific items not found in the product DB for part-number discrepancies (typos, wrong brands). | "Validated 2 items via web search. Line 3: ✅ confirmed. Line 7: 🟠 discrepancy." |
| 3 | **Group** | Groups specific items by brand/supply chain using LLM. Skipped if fewer than 2 specific items. | "Grouped into 3 sourcing groups: Fasteners, Hydraulics, Bearings." |
| 4 | **Find Previous** | Searches purchase history for suppliers who previously supplied each part number. Adds to RFQ. | "Found 5 suppliers from our records. Line 1: Acme Corp, BoltCo..." |
| 4b | **Brand-Linked** | Looks up each item's brand in the supplier-brand link table. Auto-adds top 5 Tier A suppliers per line. | "Added 3 Tier A brand-linked suppliers." |
| 4c | **Cross-Apply** | Within each sourcing group, shares suppliers across peer lines. If Line 1 has Supplier A and Line 2 has Supplier B, both lines get both suppliers. | "Shared 4 suppliers across grouped items." |
| — | **Sort** | Sorts all suppliers on every line by tier, history, location, name. Dashboard refreshes. | (silent) |
| — | **ASK** | Stops and asks the user before any web search. | "Would you like me to search the web for additional suppliers?" |

### Permission Gate

**The agent NEVER searches the web without explicit user permission.** After internal sources are exhausted (steps 1-4c), the pipeline always stops and asks. This applies equally to:

- Batch "Find All Suppliers" button → pipeline → asks
- Chat "find suppliers" message → pipeline → asks
- Per-item "Find Suppliers" button → internal results → "Search Web?" action button
- Per-item "Search Web?" button → web search only (user already confirmed)

## Dashboard Buttons

### Batch Buttons (top of RFQ detail page)

| Button | Triggers | What It Does |
|--------|----------|-------------|
| **Classify & Validate** | `rfq_identify_items` callback | Classifies all unmatched items, validates specific items via internal DB + web discrepancy check |
| **Find Previous Suppliers** | `rfq_find_previous_suppliers` callback | Runs grouping + internal DB search + brand lookup + cross-apply. No web search. |
| **Find New Suppliers** | `rfq_find_new_suppliers` callback | Web search only — requires previous suppliers step to have run first |
| *(Find All Suppliers)* | `rfq_find_all_suppliers` → synthetic message → pipeline | Full 7-step pipeline via graph. Used programmatically; currently no visible button. |

### Per-Item Buttons (on each line)

| Button | Triggers | What It Does |
|--------|----------|-------------|
| **Classify & Validate** | `rfq_identify_items` callback (single item) | Classifies + validates a single line item |
| **Find Suppliers** | `rfq_find_suppliers` callback → Phase 1 only | Searches internal DB for suppliers matching this line. Then asks "Search Web?" |
| **Search Web** (appears after Find Suppliers) | `rfq_find_web_suppliers_for_line` callback | Web search for this specific line — only shown after user clicks |

### Button States

- **Batch buttons** are disabled (greyed out) while the agent is processing any action — prevents race conditions from rapid clicking.
- **Per-item buttons** remain always enabled — clicking "Find Suppliers" on multiple lines concurrently is a valid workflow.

## Thread-Pinning Architecture

When a button action runs (which may take 30+ seconds), the user might navigate to a different RFQ in the dashboard. Without protection, Chainlit's `on_chat_resume` overwrites the session `thread_id` — sending in-progress messages to the wrong conversation.

**Solution:** All RFQ action callbacks use the `_pin_thread()` context manager:

```python
async with _pin_thread() as pinned_tid:
    # Captures thread_id at callback start
    await _send_pinned("Processing...", pinned_tid)  # Always goes to correct thread
    # ... do work ...
    await _send_pinned("Done!", pinned_tid)
```

**`_send_pinned()`** checks if the current thread still matches the pinned one. If the user switched threads, it temporarily swaps back to send the message, then restores.

**`_main_pinned()`** does the same for graph invocations — ensuring agent responses land in the correct thread even if the user has navigated away.

## Code Map

| File | Role |
|------|------|
| `includes/chat/rfq_actions.py` | Chainlit `@cl.action_callback` handlers for all dashboard buttons. Per-item + batch. Thread-pinning. |
| `includes/agents/procurement_agent.py` | `_try_find_suppliers_pipeline` — the 7-step programmatic pipeline. Triggered by "find suppliers" keyword in chat or synthetic button message. |
| `includes/tools/rfq_crud.py` | Sync database functions: `_classify_rfq_items_sync`, `_validate_items_sync`, `_group_rfq_items_sync`, `_find_purchase_suppliers_sync`, `_find_brand_suppliers_sync`, `_cross_apply_suppliers_sync`, `_web_search_suppliers_sync`, `_sort_rfq_suppliers_sync`, plus all CRUD helpers. |
| `includes/tools/quote_tools.py` | LangGraph `@tool` wrappers (`manage_rfq`, `get_rfq`), communication helpers (`_notify_rfq_updated`, `_notify_agent_working`, `_stream_to_user`), re-exports from rfq_crud. |
| `includes/tools/product_tools.py` | `_find_purchase_history_for_part`, `_find_brand_suppliers_with_tier`, `_find_product_by_code` — internal DB search functions. |
| `includes/agent_bridge.py` | Bridge between dashboard and Chainlit sessions. `dispatch_action()`, `notify_dashboard()`, per-session locking. |
| `app.py` | `@cl.on_message` main handler. Validates, extracts intent, invokes graph, streams events. |
| `includes/graph.py` | LangGraph state machine — Supervisor routes to ProcurementAgent / ResearchAgent / GeneralAgent. |
| `templates/rfq_detail.html` | Dashboard RFQ detail page with batch buttons. |
| `templates/partials/_rfq_items_table.html` | Per-item line table with per-item action buttons. |
| `templates/base.html` | Alpine.js `rfqDetail()` component — button handlers, `_sendAction()`, `agentBusy` flag, thread-pinning coordination. |

## Data Model

```
rfqs
  ├── id (UUID, PK)
  ├── rfq_number (String, unique)     — e.g., "RFQ-2026-0042"
  ├── title (String)
  ├── description (Text)
  ├── customer (String)
  ├── status (String)                 — DRAFT | SENT | RECEIVED | AWARDED | CLOSED
  ├── assigned_to (String)            — User email
  ├── item_groups (JSONB)             — {groups: [{label, lines, reason}], ungrouped: [lines]}
  ├── history (JSONB)                 — [{date, user, action}, ...]
  ├── created_by (String)
  ├── created_at, updated_at (DateTime)

rfq_items
  ├── id (UUID, PK)
  ├── rfq_id (FK → rfqs.id)
  ├── line (Integer)
  ├── input_description (Text)        — Original user-provided description
  ├── input_code (String)             — Original user-provided code/part number
  ├── part_number (String)            — Normalized part number
  ├── brand (String)                  — Normalized brand
  ├── product_id (FK → products.id)   — Linked product (if matched)
  ├── quantity (Integer)
  ├── uom (String)                    — Unit of measure (e.g., "ea")
  ├── match (String)                  — unmatched | specific | branded | generic | discrepancy
  ├── notes (Text)
  ├── suppliers (JSONB)               — [{name, supplier_id, contacts, status, price_type, ...}]
  ├── brand_suppliers (JSONB)         — Full brand-linked supplier list (for modal reference)

rfq_threads
  ├── rfq_number (String, PK)
  ├── user_email (String, PK)
  ├── thread_id (String)
  └── created_at (DateTime)
```

## Prompt Files

See `config/prompts/README.md` for the prompt file index — which agent loads each prompt and when.

## Testing

```bash
uv run pytest tests/ -x --timeout=60
```

Key test files:
- `tests/agents/test_procurement_agent.py` — ProcurementAgent initialization and call tests
- `tests/test_actions.py` — Action callback tests (28 tests)
- `tests/tools/test_quote_tools.py` — RFQ management tool tests
- `tests/tools/test_supplier_sourcing.py` — Supplier matching and enrichment tests
