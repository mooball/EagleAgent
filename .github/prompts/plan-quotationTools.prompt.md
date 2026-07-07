# Plan: Quotation Tools for EagleAgent

## Overview
Build LangGraph tools that give the EagleAgent the ability to read, interpret, and update RFQ quotation data. The agent should be able to: (1) get a comprehensive snapshot of an RFQ's quotation state, (2) update quote prices, statuses, shipping, and notes per supplier, and (3) parse supplier emails to automatically extract and apply quote data.

## Current State
- RFQ data model now includes: `rfq_items.cost_price/sale_price`, supplier JSONB fields (`quote_cost`, `quote_status`, `quote_currency`, `quote_leadtime`), and `rfqs.supplier_meta` (`shipping_cost`, `notes`, `terms` per supplier).
- The existing `manage_rfq` tool has `add_supplier` and `update_supplier` actions that work with the old `price`/`currency` fields, not the new `quote_*` fields.
- There is no tool for viewing the full quotation state or for parsing supplier emails.

## Target State

### Tool 1: `view_rfq_quotation(rfq_id)` — Comprehensive Markdown Snapshot
- Returns a well-structured Markdown report of the entire RFQ quotation state.
- Includes: items table (description, part#, brand, qty, cost, sale), per-item supplier quotes (price, status, shipping), totals, supplier notes/terms, email thread summaries.
- The agent uses this as its primary context-gathering step before any quotation work.

### Tool 2: `manage_rfq` — Extended with quotation actions
- Add new actions to the existing `manage_rfq` tool in `quote_tools.py`.
- `update_quote`: Set `quote_cost`, `quote_status`, `quote_currency` on a specific item×supplier.
- `select_quote`: Shortcut to mark a supplier as selected (auto-deselects others on that item).
- `decline_quote`: Shortcut to mark as declined and clear quote_cost.
- `set_supplier_meta`: Write `shipping_cost`, `notes`, `terms` to `rfqs.supplier_meta`.
- The existing `add_supplier` action needs updating to include `quote_*` fields.
- The existing `update_supplier` action needs updating to include `quote_*` fields.

### Tool 3: `parse_supplier_email(rfq_id, email_body, supplier_name?)` — Email Interpreter
- Takes raw email body text and an RFQ ID.
- Uses the LLM (via structured output / function calling) to extract: item→price mappings, shipping cost, lead time, currency, and any notes/constraints.
- Automatically calls the quotation management functions to apply extracted data.
- Returns a summary of what was extracted and updated so the agent can confirm with the user.

## Design Decisions

### Extend `manage_rfq` rather than create many small tools
One tool with clear action strings is simpler for the agent to route to. The existing pattern (`manage_rfq(action="...", rfq_id=..., data={...})`) is well-established. Adding quotation actions follows the same pattern.

### Snapshot as a separate tool
`manage_rfq` returns short confirmations. The snapshot is a large context dump (potentially hundreds of lines of Markdown). Different purpose, different return shape. The agent calls `view_rfq_quotation` first, then uses `manage_rfq` to make changes.

### Email parser as a tool (not a prompt)
The email parsing logic should be a dedicated tool that uses structured LLM extraction (not just a prompt). This keeps the extraction logic testable and the main agent's context clean. The tool does the heavy lifting: parse → extract → update → return summary.

### `supplier_meta` at RFQ level
Shipping cost, notes, and terms are RFQ×supplier, not item×supplier. The `set_supplier_meta` action reads/writes `rfqs.supplier_meta` JSONB. The existing `supplier-quote` PATCH endpoint handles item-level quote data separately.

---

## Phase 1 — `view_rfq_quotation` (Snapshot Tool)

### 1.1 Create the snapshot builder function
**File:** `includes/tools/quote_tools.py`

Build a synchronous function `_build_quotation_snapshot(rfq_dict)` that takes the RFQ dict (from `_get_rfq_dict_sync`) and returns a Markdown string.

**Structure — dual view (matrix + per-supplier):**
- **Header:** RFQ number, customer name + NetSuite ID, contact (name, email, phone), status, created date, assigned user, reference, NetSuite opportunity ID, HubSpot deal ID.
- **Price Matrix:** Items as rows, suppliers as columns. Each cell shows price + status symbol (★ selected, ✗ declined, — not quoted). Includes item NetSuite internal IDs and part numbers for cross-reference.
- **Per-Supplier Sections:** Supplier name + NetSuite ID + internal UUID, shipping cost/currency, notes, terms. Sub-table listing every item that supplier was asked to quote on, with NS IDs.
- **Totals row:** Summed cost_price × qty and sale_price × qty.
- **Email Summary:** Latest 3 emails (direction, subject, date).

**Section A — Header + Item×Supplier Price Matrix** (compact, for quick comparison)
```markdown
# RFQ-2026-0039 — Quotation Status
**Customer:** Acme Pty Ltd (NS: 88421) | **Contact:** Jane Smith, jane@acme.com, +61 2 9999 8888
**Status:** awaiting_quotes | **Created:** 2026-06-15 | **Assigned to:** tom@eagle-exports.com
**Reference:** PO-2026-0442 | **NetSuite Opp:** OP71449 | **HubSpot Deal:** 123456

## Price Matrix
| # | Description | Part # | NS ID | Brand | Qty | Cost | Sale | ABC Bearings | XYZ Parts | Global Supply |
|---|-------------|--------|-------|-------|-----|------|------|-------------|-----------|---------------|
| 1 | Bearing 6205 | 6205-2RS | 4421 | SKF | 10 | $45.00 | $62.50 | $42.50 | $48.00 ✗ | $39.00 ★ |
| 2 | Seal Kit | SK-100 | 6892 | Parker | 5 | — | — | $18.00 | — | — |

**Key:** ★ = selected, ✗ = declined, — = not quoted
**Totals:** Cost $450.00 | Sale $625.00
```

**Section B — Per-Supplier Detail** (for in-depth review)
```markdown
## Supplier: ABC Bearings (NS: 5532, ID: f47ac10b-...)
**Shipping:** $15.00 AUD | **Notes:** Deliver Tuesdays only | **Terms:** Net 30

| Line | Item | Part # | NS ID | Qty | Price | Status |
|------|------|--------|-------|-----|-------|--------|
| 1 | Bearing 6205 | 6205-2RS | 4421 | 10 | $42.50 AUD | quoted |
| 2 | Seal Kit | SK-100 | 6892 | 5 | $18.00 AUD | quoted |
```

**Rationale for dual view:** ...

### 1.2 Register the tool
**File:** `includes/tools/quote_tools.py` — `create_quote_tools()`

Add `view_rfq_quotation` to the returned tool list.

### 1.3 Test
Add a test that calls `view_rfq_quotation` with a known RFQ and verifies the Markdown output contains expected sections.

---

## Phase 2 — Extend `manage_rfq` with quotation actions

### 2.1 Add `update_quote` action
**File:** `includes/tools/quote_tools.py` — inside `manage_rfq`

New action string: `"update_quote"`
Data keys: `line` (required int), `name` (required string), plus any of: `quote_cost` (float), `quote_status` (string), `quote_currency` (string, 3-letter ISO), `quote_leadtime` (string).

Implementation: calls `_update_supplier_sync` or directly manipulates the JSONB supplier array, then calls `flag_modified`.

### 2.2 Add `select_quote` shortcut
Data keys: `line` (required int), `name` (required string).

Sets `quote_status` to `"selected"` on the named supplier, sets all other suppliers on that item to `"quoted"` if they were `"selected"`. Copies `quote_cost` to the item's `cost_price`.

### 2.3 Add `decline_quote` shortcut
Data keys: `line` (required int), `name` (required string).

Sets `quote_status` to `"declined"`, clears `quote_cost` to `None`.

### 2.4 Add `set_supplier_meta` action
Data keys: `name` (required string), plus any of: `shipping_cost` (float), `shipping_currency` (string), `notes` (string), `terms` (string).

Reads/writes `rfqs.supplier_meta` JSONB. Merges with existing data (doesn't overwrite unspecified keys).

### 2.5 Update `add_supplier` and `update_supplier`
Accept new optional keys: `quote_cost`, `quote_status`, `quote_currency`, `quote_leadtime`. Store them in the supplier JSONB entry alongside existing fields.

### 2.6 Update `manage_rfq` docstring
Add all new actions to the tool's docstring so the LLM knows they exist.

### 2.7 Tests
Add tests for each new action:
- `update_quote` sets quote_cost and quote_status correctly
- `select_quote` deselects previous selection and copies cost to item
- `decline_quote` clears price and sets status
- `set_supplier_meta` correctly merges into existing meta

---

## Phase 3 — Email Quote Pipeline

Supplier quotes arrive via email in varied formats: plain text body, HTML body, attached PDFs, inline images from brochures. A single `parse_supplier_email` tool is insufficient. The pipeline has three stages:

### 3A — `classify_supplier_email` (Triage)

**File:** `includes/tools/email_parser.py`

```python
@tool
async def classify_supplier_email(email_tracking_id: int) -> str:
```

**What it does:**
1. Looks up the email by `email_tracking_id` in `email_tracking`.
2. Checks if the email is linked to an RFQ (`rfq_id` is not null).
3. Checks if the sender matches any shortlisted supplier on that RFQ (by email domain or name).
4. Performs a lightweight scan of the email body (first ~500 chars) and attachment filenames for quote indicators: prices, currency symbols, part numbers matching RFQ items, keywords like "quote", "quotation", "pricing", "lead time".
5. Returns a classification: `quote_response` | `not_quote` | `needs_review`.

**Why a separate triage step:** Not every email from a supplier is a quote. Order confirmations, delivery updates, and general correspondence shouldn't trigger the full extraction pipeline. This tool lets the agent decide whether to proceed.

### 3B — `extract_email_content` (Gather)

**File:** `includes/tools/email_parser.py`

```python
@tool
async def extract_email_content(email_tracking_id: int) -> str:
```

**What it does:**
1. Fetches the full email from `email_tracking` (body_markdown, body_html, attachments_json).
2. For each non-inline attachment:
   - **PDF:** passes the raw PDF bytes directly to Gemini as a document part (Gemini 2.5 Pro natively understands PDFs — no text extraction layer needed). Asks Gemini to extract all pricing, part numbers, and tabular data into structured Markdown tables.
   - **Image (PNG/JPG):** passes to Gemini as an image part for OCR + content description. Extracts text from brochures, scanned quotes, and product spec sheets.
3. For inline images (cid: references in body): replaces with `[inline-image: filename.png]` — Gemini handles these naturally when processing the body.
4. Assembles a clean content bundle:
   ```
   ## Email Body (Markdown)
   ... (body_markdown from email_tracking)

   ## Attachment: merged.pdf (679 KB)
   [Gemini-extracted content as Markdown tables]
   | Item | Part # | Qty | Unit Price | Total |
   |------|--------|-----|------------|-------|
   | Bearing 6205 | 6205-2RS | 10 | $42.50 | $425.00 |

   ## Attachment: brochure.jpg (120 KB)
   [Gemini vision extraction]
   "Product catalog page showing Bearing 6205 specifications..."
   ```
5. Returns the assembled content bundle.

**Why Gemini-native PDF:** PDF text parsers (PyPDF2, pdfplumber) lose table structure, merged cells, and multi-column layouts. Gemini 2.5 Pro accepts PDFs as native input — it "sees" the document as a human would. No intermediate parsing layer to lose context. The LLM interprets pricing tables as a whole, preserving relationships between items, quantities, and prices.

**Token cost consideration:** Large PDFs consume significant input tokens. The triage step (3A) prevents running this on non-quote emails. For quoting, the accuracy gain from vision-level interpretation justifies the cost.

### 3C — `interpret_quote_response` (Apply)
...same as before, but now takes the *Gemini-extracted* content bundle (which already has clean Markdown tables from any PDFs/images). The interpretation step focuses purely on matching item lines to RFQ items — the heavy extraction work is already done.

### 3C — `interpret_quote_response` (Apply)

**File:** `includes/tools/email_parser.py`

```python
@tool
async def interpret_quote_response(rfq_id: str, supplier_name: str, content_bundle: str) -> str:
```

**What it does:**
1. Calls `view_rfq_quotation` internally to get current RFQ state (item list, existing quotes).
2. Constructs an LLM prompt containing:
   - The RFQ's item list (descriptions, part numbers, quantities)
   - The supplier name
   - The content bundle from `extract_email_content`
   - Instructions to extract structured quote data
3. Uses structured output / JSON mode to extract:
   ```json
   {
     "quotes": [
       {"item_line": 1, "confidence": "high", "price": 41.50, "currency": "AUD", "lead_time": "2 weeks"},
       {"item_line": 3, "confidence": "medium", "price": 23.00}
     ],
     "shipping": {"cost": 25.00, "currency": "AUD"},
     "declined_items": [2],
     "notes": "Volume discount of 10% for orders over $1000",
     "terms": "Net 30",
     "warnings": ["Line 3 price extracted from PDF table — verify manually"]
   }
   ```
4. For each extracted quote: calls `manage_rfq(update_quote, ...)` and `manage_rfq(set_supplier_meta, ...)`.
5. Returns a Markdown summary of what was updated, with confidence levels and any warnings.

**Matching strategy:**
- The LLM matches email line items to RFQ items by description + part number similarity.
- `confidence: high` = exact part number match or unambiguous description match.
- `confidence: medium` = fuzzy description match, no part number.
- `confidence: low` = best guess — the tool still applies it but flags with a warning.
- If supplier_name is not provided, the tool extracts it from the email signature.

**Auto-apply vs confirm:**
The tool auto-applies updates and returns a summary. The agent can undo or correct. This matches the vision of "agent reads email and updates RFQ automatically." For `confidence: low` items, the tool includes prominent warnings in the summary.

### 3D — Dependencies
- **Gemini 2.5 Pro** native PDF + image understanding (already in use).
- Gmail attachment download: reuse the existing proxy endpoint in `includes/dashboard/routes/api.py`.
- No additional PDF parsing libraries needed — Gemini handles PDFs directly.

### 3E — Tests
- `classify_supplier_email`: test with a quote email → `quote_response`; test with a delivery notification → `not_quote`.
- `extract_email_content`: test with a PDF attachment → verify text extraction; test with inline images.
- `interpret_quote_response`: test with sample content bundle → verify correct `manage_rfq` calls and summary output.
- Integration: end-to-end test from email_tracking record → classified → extracted → applied to RFQ.

---

## Phase 4 — Wiring

### 4.1 Integration with Gmail sync pipeline
**File:** `scripts/sync_gmail_mailboxes.py`

The existing `sync_mailbox()` function already:
1. Fetches new emails via Gmail History API
2. Runs three-tier matching (by ID → by subject → by contact/domain)
3. Links emails to RFQs (`rfq_id`) and suppliers (`supplier_id`) in `email_tracking`

**New step after matching:** When an email matches BOTH an RFQ and a supplier, pass it through the quote extraction pipeline:

```python
# After existing matching logic in sync_mailbox():
if tracking.rfq_id and tracking.supplier_id:
    # This email is from a known supplier about a known RFQ — check for quotes
    from includes.tools.email_parser import classify_supplier_email
    classification = classify_supplier_email(tracking.id)
    if classification == "quote_response":
        from includes.tools.email_parser import extract_email_content, interpret_quote_response
        content = extract_email_content(tracking.id)
        supplier_name = tracking.sender_name or tracking.sender_email
        result = interpret_quote_response(tracking.rfq_id, supplier_name, content)
        logger.info(f"[quote-pipeline] Processed email {tracking.id}: {result[:200]}")
```

**Sync vs async consideration:** The sync script runs in a thread pool (`asyncio.to_thread`). The quote pipeline tools use async LLM calls. May need to run the pipeline in a separate async context or use `asyncio.run()` within the thread.

### 4.2 Register tools for agent use
**File:** `includes/agents/` — Add `view_rfq_quotation`, `classify_supplier_email`, `extract_email_content`, `interpret_quote_response` to the appropriate agent profiles for user-initiated "process this email" commands. The automated pipeline handles the common case; the tools let users manually trigger processing for specific emails.

### 4.2 Suggested agent workflow
```
User: "Check if any suppliers have replied with quotes"
  → Agent: for each recent email from shortlisted suppliers:
      → classify_supplier_email → "quote_response" for 2 emails
      → extract_email_content → content bundles
      → interpret_quote_response → updates RFQ
  → Agent: "ABC Bearings quoted $42.50 on line 1 and $18.00 on line 2.
            XYZ Parts declined line 1 but quoted $23.00 on line 3.
            Shipping from ABC is $15.00. See the quotation tab for details."
```

### 4.3 Integration test
End-to-end test:
1. Create RFQ with items and shortlisted suppliers
2. Create an email_tracking record linked to the RFQ from a supplier, with a PDF attachment containing quote data
3. Call `classify_supplier_email` → verifies `quote_response`
4. Call `extract_email_content` → verifies PDF text extraction
5. Call `interpret_quote_response` → verifies RFQ updated with extracted prices
6. Call `view_rfq_quotation` → verifies the snapshot reflects the updates

---

## Open Questions
1. ~~Should email parsing be a single tool?~~ → **Resolved: three-stage pipeline.** `classify` → `extract` → `interpret`. Handles PDFs, inline images, and varying formats properly.
2. ~~Should `view_rfq_quotation` include full email bodies?~~ → **Resolved: summaries only** (latest 3). Fetch specific emails if needed.
3. ~~Should the snapshot be item-first or supplier-first?~~ → **Resolved: dual view.** Matrix + per-supplier detail sections.
4. ~~Image attachments — manual review or vision model?~~ → **Resolved: Gemini-native.** PDFs and images pass directly to Gemini 2.5 Pro as document/image parts. No intermediate text parser — the LLM "sees" the document. Better accuracy for complex pricing tables; justifies token cost for quote-critical emails.
5. **PDF extraction library?** → Not needed. Gemini handles PDFs natively. No PyPDF2/pdfplumber dependency required.
6. **Quote leadtime in matrix UI?** → Defer. Tools handle it; old quotation view has it if needed.
