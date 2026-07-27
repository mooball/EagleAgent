# Plan: Create RFQ from Email Pipeline

**Goal**: When a user clicks "Create RFQ" in the Gmail add-on (or triggers it from the dashboard), an async pipeline processes the email content, extracts line items, and creates a new RFQ linked to the email thread.

**Architecture**: Follows the same pattern as the supplier quote pipeline (`supplier_quote_pipeline.py`): background daemon thread, multi-stage processing, idempotency guard via JSONB result column on `EmailTracking`.

**Trigger**: Manual only for Phase 1 — user clicks "Create RFQ" in the Gmail add-on. Automated classification (Stage 0) deferred to future phase.

---

## Architecture Overview

```
User clicks "Create RFQ" (add-on or dashboard)
│
├─ POST /api/addon/create-rfq
│  {gmail_message_id, gmail_thread_id}
│
└─ trigger_rfq_creation_pipeline(email_tracking_id, user_id)
   [daemon thread — fire and forget]
   │
   ├─ GUARD CHECKS
   │  ├─ direction == "received" (incoming email only)
   │  ├─ customer_id present on tracking record
   │  ├─ rfq_token / rfq_id NOT already set (not already linked)
   │  └─ rfq_creation_result NOT already set (idempotency)
   │
   ├─ STAGE 1: Build content bundle
   │  Reuse _extract_email_content_sync() from supplier_quote_pipeline.py
   │  └─ Email body + all attachments (PDF, images, spreadsheets) → Markdown string
   │
   ├─ STAGE 2: Extract line items
   │  Reuse rfq_item_import.py (Smart Item Adder) parsing functions:
   │  ├─ parse_html_table() — deterministic HTML table parser (BeautifulSoup)
   │  ├─ parse_text_table() — deterministic CSV/TSV parser
   │  ├─ extract_items_from_text() — Gemini LLM for unstructured text
   │  ├─ extract_items_from_image() — Gemini Vision for screenshots
   │  └─ _normalize_to_standard_columns() → 5 standard fields:
   │      {input_description, input_code, brand, quantity, uom}
   │
   ├─ STAGE 3: Create RFQ
   │  ├─ Generate rfq_number
   │  ├─ Set customer from tracking.customer_id
   │  ├─ Set subject from email subject
   │  ├─ Call _add_items_sync() from rfq_crud.py to bulk-insert items
   │  └─ Update email_tracking: set rfq_token → new RFQ number
   │
   └─ STAGE 4: Save result
      └─ EmailTracking.rfq_creation_result ← JSONB idempotency guard
         {
           "rfq_number": "RFQ-2026-0123",
           "items_extracted": 5,
           "customer": "Acme Corp",
           "status": "complete",
           "actions": ["Created RFQ with 5 items"],
           "processed_at": "2026-07-27T..."
         }
```

---

## New Files

| File | Purpose |
|---|---|
| `includes/tools/rfq_creation_pipeline.py` | Main pipeline: guard checks, orchestrate stages, save result |
| `config/prompts/rfq_creation_extract.md` | (Optional) Prompt for LLM-guided extraction if needed |

## Edited Files

| File | Change |
|---|---|
| `includes/dashboard/models.py` | Add `rfq_creation_result` column to `EmailTracking` |
| `alembic/versions/` | Migration for new JSONB column |
| `includes/dashboard/routes/addon.py` | Add `POST /api/addon/create-rfq` endpoint |
| `addon/Code.gs` | Implement `onCreateRfq` — calls endpoint, shows notification |
| `config/settings.py` | Optional: `RFQ_CREATION_MODEL` env var |

---

## Reuse Map

| Source Module | What We Reuse |
|---|---|
| `supplier_quote_pipeline.py` | `_extract_email_content_sync()` — builds content bundle from email body + all attachments, already handles PDF/OCR, image triage, spreadsheet parsing |
| `email_pipeline.py` | `triage_image()`, `classify_image_content()`, `llm_call_with_retry()` — image signature caching, Gemini LLM infrastructure |
| `rfq_item_import.py` | `parse_html_table()`, `parse_text_table()`, `extract_items_from_text()`, `extract_items_from_image()`, `_normalize_to_standard_columns()` — Smart Item Adder parsing functions |
| `rfq_crud.py` | `_add_items_sync()` — bulk insert RFQ items in one transaction |

---

## Detailed Stage Design

### Guard Checks

```python
# Must be an incoming email
if tracking.direction != "received":
    return  # skip silently

# Must be linked to a customer
if not tracking.customer_id:
    _save_error("No customer linked to this email. Link a customer first.")
    return

# Must not already be linked to an RFQ
if tracking.rfq_token or tracking.rfq_id:
    _save_error(f"Email already linked to RFQ {tracking.rfq_token}.")
    return

# Idempotency: skip if already processed
if tracking.rfq_creation_result:
    logger.info("RFQ creation already processed for email #%d", email_tracking_id)
    return
```

### Stage 1: Build Content Bundle

```python
from includes.tools.supplier_quote_pipeline import _extract_email_content_sync

content_bundle = _extract_email_content_sync(email_tracking_id)
# Returns Markdown string containing:
#   ## Email Body
#   (plain text or HTML → Markdown)
#   ---
#   ## Attachment: quote_request.pdf
#   (PDF text extraction / OCR)
#   ---
#   ## Attachment: items_table.png
#   (Gemini Vision OCR → Markdown table)
#   ---
#   ...
```

This is **exactly** the same function used by the quote pipeline. It already handles:
- PDF text extraction with Gemini Vision fallback for image-based PDFs
- Image triage (skip signatures/logos, OCR content images)
- Spreadsheet parsing (openpyxl → Markdown tables)
- Email body text/HTML → Markdown

### Stage 2: Extract Line Items

The content bundle may contain multiple sections (email body, PDF text, image OCR). We iterate over each section and try deterministic parsing first, then LLM fallback:

```python
items = []
sections = content_bundle.split('\n\n---\n\n')  # split on section separators

for section in sections:
    # Try deterministic first (preferred: zero cost)
    content_type = detect_content_type(
        html=section if looks_like_html(section) else None,
        image_base64=None,   # already processed by _extract_email_content_sync
        plain_text=section
    )
    
    if content_type == "html_table":
        result = parse_html_table(section)
    elif content_type == "csv" or content_type == "tsv":
        result = parse_text_table(section, content_type)
    else:
        # Fall back to Gemini LLM if deterministic parsers produce 0 items
        result = parse_html_table(section) or parse_text_table(section, "csv")
        if not result["items"]:
            result = extract_items_from_text(section)
    
    # Normalize extracted items to 5 standard columns
    if result and result.get("items"):
        normalized = _normalize_to_standard_columns(result["items"])
        items.extend(normalized["items"])

# Remove duplicates by description + part_number
items = _deduplicate_items(items)
```

**Key design principle from Smart Item Adder**: prefer deterministic parsers (BeautifulSoup, csv.reader) over Gemini LLM. Only call the LLM when deterministic parsers produce 0 items.

### Stage 3: Create RFQ

```python
from includes.tools.rfq_crud import _add_items_sync, _create_rfq_sync

# Look up customer
customer = session.query(Customer).get(tracking.customer_id)

# Generate RFQ number (reuse existing logic)
rfq_number = _generate_rfq_number(session)

# Create RFQ record
rfq = _create_rfq_sync(
    rfq_number=rfq_number,
    customer=customer.companyname,
    customer_id=customer.id,
    created_by=user_id,
    subject=tracking.subject,
    thread_id=tracking.gmail_thread_id,
    status="draft",
)

# Bulk-insert items
if items:
    _add_items_sync(
        rfq_number=rfq_number,
        items_data={"items": items},
        user_email=user_id,
    )

# Link email thread to new RFQ
session.execute(
    text(
        "UPDATE email_tracking SET rfq_token = :token, match_type = 'manual' "
        "WHERE gmail_thread_id = :tid"
    ),
    {"token": rfq_number, "tid": tracking.gmail_thread_id},
)
session.commit()
```

### Stage 4: Save Result

```python
_save_rfq_creation_result(email_tracking_id, {
    "rfq_number": rfq_number,
    "items_extracted": len(items),
    "customer": customer.companyname,
    "status": "complete",
    "actions": [f"Created RFQ with {len(items)} items"],
    "processed_at": _now_iso(),
})
```

---

## API Endpoint

### `POST /api/addon/create-rfq`

**Request:**
```json
{
  "gmail_message_id": "18abc123...",
  "gmail_thread_id": "18abc123..."
}
```

**Response (immediate — fire and forget):**
```json
{
  "status": "processing",
  "message": "RFQ creation started. Refresh the card to see the new RFQ."
}
```

The pipeline runs in a background thread. The user refreshes the add-on context card to see the linked RFQ once processing completes.

### Error states (written to `rfq_creation_result`, shown on next context card load):

```json
{
  "status": "error",
  "error": "No customer linked to this email.",
  "processed_at": "..."
}
```

---

## Apps Script Changes

### `onCreateRfq(e)`

```javascript
function onCreateRfq(e) {
  var result;
  try {
    result = fetchBackend('/api/addon/create-rfq', {
      gmail_message_id: e.parameters.messageId,
      gmail_thread_id: e.parameters.threadId
    });
  } catch (err) {
    return CardService.newActionResponseBuilder()
      .setNotification(
        CardService.newNotification().setText('Error: ' + err.message)
      )
      .build();
  }

  return CardService.newActionResponseBuilder()
    .setNotification(
      CardService.newNotification().setText(
        'RFQ creation started. Refresh the card to see the new RFQ.'
      )
    )
    .build();
}
```

The context card already handles showing newly linked RFQs — when the user refreshes (opens another email then comes back), `onGmailMessageOpen` re-queries `/api/addon/context` which will now return the new RFQ.

---

## Database Migration

### New column on `email_tracking`

```python
# alembic/versions/xxxx_add_rfq_creation_result.py

def upgrade():
    op.add_column('email_tracking',
        sa.Column('rfq_creation_result', JSONB, nullable=True))

def downgrade():
    op.drop_column('email_tracking', 'rfq_creation_result')
```

### Model update

```python
# includes/dashboard/models.py — EmailTracking class
rfq_creation_result = Column(JSONB, nullable=True)
```

---

## Implementation Order

1. **Migration + model** — add `rfq_creation_result` column
2. **Pipeline file** — `includes/tools/rfq_creation_pipeline.py` with guard checks, content extraction (reuse), item extraction (reuse Smart Item Adder), RFQ creation, result saving
3. **API endpoint** — `POST /api/addon/create-rfq` in `addon.py`
4. **Apps Script** — `onCreateRfq` implementation
5. **Tests** — unit tests for guard checks, integration test for full pipeline

---

## Deferred to Future

- **Stage 0: Automated classification** — LLM triage to determine if an email is a new quote request (mirrors `_classify_supplier_email_sync()`). Currently manual (user clicks the button).
- **LLM-suggested subject override** — if the email subject is "Re: Quick question", the LLM could suggest a better RFQ subject.
- **Extract deadlines from email** — populate RFQ deadline field from content.
- **Extract reference numbers** — PO numbers, tender references from content.
- **Multi-attachment quote requests** — if a customer sends a cover email + attached spreadsheet with items, handle both.
- **Support for multiple customers on one RFQ** — current assumption is one customer per RFQ (matching existing model).

---

## Estimated Scope

- **Core pipeline** (guard checks, Stage 1-4): ~1-2 sessions
- **API endpoint + Apps Script**: ~1 session
- **Migration + model update**: 1 small PR
- **Tests**: ~1 session
