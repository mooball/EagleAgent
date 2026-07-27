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
   │  ├─ customer_id present on tracking record
   │  ├─ rfq_token / rfq_id NOT already set (not already linked)
   │  └─ rfq_creation_result NOT already set (idempotency)
   │  NOTE: direction NOT checked — manual trigger means user decided this IS a quote request
   │
   ├─ STAGE 2: Extract line items (two-pass)
   │  Pass 1 — Deterministic (free, no LLM):
   │  ├─ Try raw email HTML body → parse_html_table()
   │  └─ Try CSV/XLSX attachments → parse_text_table()
   │  Pass 2 — LLM fallback (only if Pass 1 = 0 items):
   │  ├─ Build content bundle via _extract_email_content_sync()
   │  └─ extract_items_from_text(content_bundle) — Gemini LLM
   │  Final: _normalize_to_standard_columns() → 5 standard fields:
   │      {input_description, input_code, brand, quantity, uom}
   │
   ├─ STAGE 3: Create RFQ
   │  ├─ _create_rfq_sync(data, user_id) — auto-generates rfq_number
   │  ├─ Set customer from tracking.customer_id
   │  ├─ Set assigned_to from tracking.user_email
   │  ├─ Set reference from email subject
   │  ├─ Call _add_items_sync(rfq_number, data, user_id) — bulk-insert items (if any)
   │  └─ Update email_tracking: set rfq_token → new RFQ number
   │  NOTE: RFQ.thread_id is for Chainlit — NOT set to gmail_thread_id
   │  NOTE: If zero items extracted, RFQ created as empty draft
   │
   └─ STAGE 4: Save result
      └─ EmailTracking.rfq_creation_result ← JSONB idempotency guard
         {
           "rfq_number": "RFQ-2026-0123",
           "items_extracted": 5,
           "extraction_method": "direct_parse",
           "customer": "Acme Corp",
           "raw_items": [...],
           "status": "complete",
           "actions": ["Created RFQ with 5 items"],
           "processed_at": "2026-07-27T..."
         }
```

---

## Design Decision: Internal Emails & Direction Guard

**Direction is NOT checked.** The manual trigger (user clicking "Create RFQ") IS the human decision that this email contains RFQ-worthy content, regardless of whether it's incoming, outgoing, or an internal forward.

**Internal forward scenario:**
1. Customer emails `sales@eagle-exports.com` with a quote request
2. Alice forwards it internally to Bob: "can you handle this?"
3. Bob opens Alice's forward in Gmail → add-on sees sender = `alice@eagle-exports.com` → blacklisted → shows "No match" + manual link buttons
4. Bob clicks "Link to Customer" → searches → selects the actual customer
5. "Create RFQ" button appears (because `customer_id` is now set)
6. Bob clicks it → pipeline extracts items → RFQ created

**Safeguard — backend domain-save guard:**
The `POST /link-email` endpoint must check the sender domain against the blacklist before calling `save_sender_domain()`. This prevents accidentally saving `eagle-exports.com` as a customer domain, even if the add-on sends `save_domain: true` for an internal email.

```python
# In POST /link-email handler (addon.py)
from includes.gmail.matching import _GENERIC_DOMAINS  # or a shared blacklist

BLACKLISTED_DOMAINS = {"eagle-exports.com", "eagle-exports.com.au", "eaglexp.com", ...}

if save_domain and sender_domain not in BLACKLISTED_DOMAINS:
    save_sender_domain(session, sender_email, entity_type, entity_id)
```

**UI rule:** "Create RFQ" button is only shown when `customer_id` is set on the tracking record. This naturally gates the feature — user must link a customer first (whether the email is from a customer directly or an internal forward).

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
| `includes/dashboard/routes/addon.py` | Add `POST /api/addon/create-rfq` endpoint; add blacklist guard to `POST /link-email` domain save |
| `addon/Code.gs` | Implement `onCreateRfq` — calls endpoint, shows notification; show button only when `customer_id` present |
| `config/settings.py` | Optional: `RFQ_CREATION_MODEL` env var |

---

## Reuse Map

| Source Module | What We Reuse |
|---|---|
| `supplier_quote_pipeline.py` | `_extract_email_content_sync()` — builds content bundle (Pass 2 fallback only); handles PDF/OCR, image triage, spreadsheet parsing |
| `email_pipeline.py` | `triage_image()`, `classify_image_content()`, `llm_call_with_retry()` — image signature caching, Gemini LLM infrastructure |
| `rfq_item_import.py` | `parse_html_table()`, `parse_text_table()`, `extract_items_from_text()`, `extract_items_from_image()`, `_normalize_to_standard_columns()` — Smart Item Adder parsing functions |
| `rfq_crud.py` | `_create_rfq_sync(data, user_id)` — create RFQ record (auto-generates rfq_number), `_add_items_sync(rfq_number, data, user_id)` — bulk insert items |

---

## Detailed Stage Design

### Guard Checks

```python
# Direction NOT checked — see "Design Decision: Internal Emails" section above.
# The manual trigger means the user has already decided this IS a quote request,
# whether the email is from a customer, an internal forward, or a sent reply.

# Must be linked to a customer (UI enforces this too — button hidden without customer)
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

### Stage 1: (Merged into Stage 2 — Pass 2)

Content bundle building via `_extract_email_content_sync()` is now called only as a fallback in Stage 2's Pass 2, if deterministic parsing produces 0 items. This avoids the cost of PDF/image processing when the email body already contains a parseable HTML table or CSV attachment.

### Stage 2: Extract Line Items (Two-Pass Approach)

The two-pass approach optimizes for cost: deterministic parsing first (free), LLM fallback only if needed.

**Pass 1: Direct parsing (zero LLM cost)**

Before building the expensive content bundle, try parsing the raw email body and attachments directly:

```python
from includes.tools.rfq_item_import import (
    parse_html_table,
    parse_text_table,
    extract_items_from_text,
    _normalize_to_standard_columns,
)

items = []

# 1. Try raw email body HTML (if it contains <table> tags)
email_html = _get_email_body_html(session, email_tracking_id)
if email_html and '<table' in email_html.lower():
    result = parse_html_table(email_html)
    if result and result.get("items"):
        items = _normalize_to_standard_columns(result["items"])["items"]

# 2. Try CSV/XLSX attachments directly (no OCR needed)
if not items:
    for attachment in _get_attachments(session, email_tracking_id):
        if attachment["mime_type"] in ("text/csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
            result = parse_text_table(attachment["content"], "csv")
            if result and result.get("items"):
                items.extend(_normalize_to_standard_columns(result["items"])["items"])
```

**Pass 2: Content bundle + LLM (only if Pass 1 = 0 items)**

If deterministic parsing found nothing, fall back to the expensive path:

```python
if not items:
    # Build content bundle (involves Gemini for PDFs/images)
    from includes.tools.supplier_quote_pipeline import _extract_email_content_sync
    content_bundle = _extract_email_content_sync(email_tracking_id)

    # Pass entire bundle to LLM for extraction
    result = extract_items_from_text(content_bundle)
    if result and result.get("items"):
        items = _normalize_to_standard_columns(result["items"])["items"]

# Record which method succeeded
extraction_method = "direct_parse" if items else "gemini_llm"

# Deduplicate by (description + part_number)
items = _deduplicate_items(items)
```

**Key design principle from Smart Item Adder**: prefer deterministic parsers (BeautifulSoup, csv.reader) over Gemini LLM. Only call the LLM when deterministic parsers produce 0 items.

### Stage 3: Create RFQ

```python
from includes.tools.rfq_crud import _create_rfq_sync, _add_items_sync
from includes.dashboard.models import Customer

# Look up customer
customer = session.query(Customer).get(tracking.customer_id)

# Create RFQ record (rfq_number auto-generated internally)
rfq = _create_rfq_sync(
    data={
        "customer": customer.companyname,
        "assigned_to": tracking.user_email,  # mailbox owner (not necessarily add-on user)
        "reference": tracking.subject,       # email subject as reference
    },
    user_id=user_id,
)
rfq_number = rfq["rfq_number"]

# Bulk-insert items (if any were extracted)
if items:
    _add_items_sync(
        rfq_number=rfq_number,
        data={"items": items},
        user_id=user_id,
    )

# Link entire email thread to the new RFQ
session.execute(
    text(
        "UPDATE email_tracking SET rfq_token = :token, match_type = 'manual' "
        "WHERE gmail_thread_id = :tid"
    ),
    {"token": rfq_number, "tid": tracking.gmail_thread_id},
)
session.commit()
```

**Notes:**
- `rfq_number` is auto-generated by `_next_rfq_number_sync()` inside `_create_rfq_sync()` — not passed in
- `assigned_to` uses `tracking.user_email` (the mailbox owner), not the add-on user — so if Alice helps Bob, the RFQ is assigned to Bob
- The RFQ model's `thread_id` field is for Chainlit chat threads, NOT Gmail threads — do NOT set it here
- `customer_id` is resolved internally by `_create_rfq_sync` from the customer name lookup
- If zero items extracted, the RFQ is created as an empty draft — user can add items manually later

### Stage 4: Save Result

```python
result = {
    "rfq_number": rfq_number,
    "items_extracted": len(items),
    "customer": customer.companyname,
    "status": "complete",
    "extraction_method": extraction_method,  # "direct_parse" or "gemini_llm"
    "raw_items": items,                      # store for debugging
    "actions": [f"Created RFQ with {len(items)} items"],
    "processed_at": _now_iso(),
}

# Add warning if no items could be extracted
if not items:
    result["warnings"] = ["No items could be extracted from the email content. RFQ created as empty draft."]

_save_rfq_creation_result(email_tracking_id, result)
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
2. **Pipeline file** — `includes/tools/rfq_creation_pipeline.py` with guard checks, two-pass item extraction (deterministic → LLM fallback), RFQ creation, result saving
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

- **Core pipeline** (guard checks, two-pass extraction, RFQ creation, result saving): ~1-2 sessions
- **API endpoint + Apps Script**: ~1 session
- **Migration + model update**: 1 small PR
- **Tests**: ~1 session
