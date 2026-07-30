# Plan: Create RFQ from Email Pipeline

**Goal**: When a user clicks "Create RFQ" in the Gmail add-on (or triggers it from the dashboard), an async pipeline processes the email content, extracts line items, and creates a new RFQ linked to the email thread.

**Architecture**: Follows the same pattern as the supplier quote pipeline (`supplier_quote_pipeline.py`): background daemon thread, multi-stage processing, idempotency guard via JSONB result column on `EmailTracking`.

**Trigger**: Manual only for Phase 1 — user clicks "Create RFQ" in the Gmail add-on. Automated classification (Stage 0) deferred to future phase.

---

## Pre-Step: Rename `notes` → `title` + Add New `notes` Field

The RFQ model currently uses `notes` as the title/description field (displayed as "Title" in the UI). We need to fix this naming before adding a real notes field.

**Migration**:
```python
# alembic/versions/xxxx_rename_rfq_notes_to_title.py

def upgrade():
    # Rename existing 'notes' column to 'title' (contains RFQ title/description)
    op.alter_column('rfqs', 'notes', new_column_name='title', type_=sa.String, nullable=True)
    # Add new 'notes' column for customer requirements/delivery notes
    op.add_column('rfqs', sa.Column('notes', sa.Text, nullable=True))

def downgrade():
    op.drop_column('rfqs', 'notes')
    op.alter_column('rfqs', 'title', new_column_name='notes', type_=sa.Text, nullable=True)
```

**Model change** (`includes/dashboard/models.py` — RFQ class):
```python
title = Column(String, nullable=True)       # RFQ title/description (was 'notes')
notes = Column(Text, nullable=True)         # Customer requirements, delivery dates, general context
```

**Code references to update** (rfq.notes → rfq.title):
| File | Change |
|---|---|
| `includes/tools/rfq_crud.py` | `_rfq_to_dict`: `"notes"` → `"title"`; `_create_rfq_sync`: `notes=` → `title=`; `_update_rfq_sync` updatable list; `_add_note_sync` appends to `rfq.notes` (keep as-is — this now writes to the new notes field correctly) |
| `includes/tools/rfq_render.py` | `rfq.get("notes")` → `rfq.get("title")` for the title display |
| `includes/dashboard/routes/rfqs.py` | updatable list, search/sort references |
| `templates/partials/rfq_detail.html` | `rfq.notes` → `rfq.title` in display + form; add new notes display/edit section |
| `templates/partials/_rfq_rows.html` | `rfq.notes` → `rfq.title` in list column |
| `tests/tools/test_quote_tools.py` | `rfq.notes` assertions → `rfq.notes` (these test `add_note` which correctly targets the new notes field) |

**Note on `_add_note_sync`**: This function appends text to `rfq.notes`. After the rename, it naturally writes to the *new* `notes` field (customer requirements) which is the correct behaviour — it was always intended for adding contextual notes, not modifying the title.

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
   ├─ STAGE 2: Create RFQ & link to email (immediate)
   │  ├─ _create_rfq_sync(data, user_id) — auto-generates rfq_number
   │  ├─ Set customer from tracking.customer_id
   │  ├─ Set assigned_to from tracking.user_email
   │  ├─ Set reference from email subject
   │  ├─ Update email_tracking: set rfq_token → new RFQ number
   │  └─ User can now navigate between Gmail and RFQ immediately
   │  NOTE: RFQ.thread_id is for Chainlit — NOT set to gmail_thread_id
   │  NOTE: RFQ created as empty draft — items added in next stage
   │
   ├─ STAGE 3: Extract line items + notes (LLM evaluation)
   │  ├─ Build content bundle via _extract_email_content_sync()
   │  │   (email body + PDF/image attachments + spreadsheets)
   │  ├─ Send to Gemini LLM with extraction prompt
   │  │   Returns: items[], warnings[], title, customer_notes
   │  │   Flags emails with no extractable items (general enquiry)
   │  ├─ _normalize_to_standard_columns() → 5 standard fields:
   │  │   {input_description, input_code, brand, quantity, uom}
   │  ├─ _add_items_sync(rfq_number, items) — bulk-insert into existing RFQ
   │  └─ Update RFQ title + notes from LLM extraction
   │
   └─ STAGE 4: Save result
      └─ EmailTracking.rfq_creation_result ← JSONB idempotency guard
         {
           "rfq_number": "RFQ-2026-0123",
           "items_extracted": 5,
           "extraction_method": "gemini_llm",
           "title": "Komatsu PC200 engine parts",
           "customer_notes": "Required by 15 Aug. Genuine OEM only.",
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
| `includes/tools/rfq_creation_pipeline.py` | Main pipeline: guard checks, LLM item extraction, RFQ creation, result saving |
| `config/prompts/rfq_creation_extract.md` | Prompt for LLM extraction — structured output with items, missing items, confidence |

## Edited Files

| File | Change |
|---|---|
| `includes/dashboard/models.py` | Add `rfq_creation_result` column to `EmailTracking` |
| `alembic/versions/` | Migration for new JSONB column |
| `includes/dashboard/routes/addon.py` | Add `POST /api/addon/create-rfq` endpoint; add blacklist guard to `POST /link-email` domain save |
| `addon/Code.gs` | Implement `onCreateRfq` — calls endpoint, shows notification; show button only when `customer_id` present |
| `config/settings.py` | Add `RFQ_CREATION_PIPELINE_MODEL` env var (documents the pipeline, falls back to DEFAULT_MODEL) |

---

## Reuse Map

| Source Module | What We Reuse |
|---|---|
| `supplier_quote_pipeline.py` | `_extract_email_content_sync()` — builds content bundle (email body + attachments); handles PDF/OCR, image triage, spreadsheet parsing |
| `email_pipeline.py` | `get_pipeline_model()`, `llm_call_with_retry()` — model resolution + retry/fallback infrastructure |
| `rfq_item_import.py` | `_normalize_to_standard_columns()` — normalizes extracted items to 5 standard fields |
| `rfq_crud.py` | `_create_rfq_sync(data, user_id)` — create RFQ record (auto-generates rfq_number), `_add_items_sync(rfq_number, data, user_id)` — bulk insert items |

---

## LLM Model Configuration

Uses the same `get_pipeline_model()` resolution pattern as the supplier quote pipeline (`email_pipeline.py`).

**Pipeline name**: `RFQ_CREATION`

**Steps**:
| Step | Purpose | Env Var (step-specific) | Fallback |
|---|---|---|---|
| `extract` | Extract line items from content bundle | `RFQ_CREATION_EXTRACT_MODEL` | `RFQ_CREATION_PIPELINE_MODEL` → `DEFAULT_MODEL` |

**Resolution order** (same as QUOTE pipeline):
1. `RFQ_CREATION_EXTRACT_MODEL` — step-specific override
2. `RFQ_CREATION_PIPELINE_MODEL` — pipeline-level override
3. `Config.DEFAULT_MODEL` — global default

**Shared with QUOTE pipeline**: The content bundle building step (`_extract_email_content_sync`) internally uses `QUOTE` pipeline models for vision/PDF extraction. This means PDF OCR, image triage, and spreadsheet parsing use `QUOTE_EXTRACT_MODEL` / `QUOTE_PIPELINE_MODEL` — they are shared because it's the same operation regardless of whether we're processing an inbound supplier quote or an inbound customer request.

**Usage in code**:
```python
from includes.email_pipeline import get_pipeline_model, llm_call_with_retry

# Item extraction uses RFQ_CREATION pipeline
response = llm_call_with_retry(
    pipeline="RFQ_CREATION",
    step="extract",
    contents=extraction_prompt,
    temperature=0.1,
    timeout=120000,
)
```

**settings.py addition**:
```python
# RFQ creation pipeline (extract items from customer request emails)
RFQ_CREATION_PIPELINE_MODEL = os.getenv("RFQ_CREATION_PIPELINE_MODEL", "")
```

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

### Stage 2: Create RFQ & Link to Email (Immediate)

The RFQ is created and linked to the email thread **before** item extraction begins. This ensures the user can navigate between Gmail and the RFQ dashboard immediately — even if item extraction is slow or fails entirely.

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

# Link entire email thread to the new RFQ immediately
session.execute(
    text(
        "UPDATE email_tracking SET rfq_token = :token, match_type = 'manual' "
        "WHERE gmail_thread_id = :tid"
    ),
    {"token": rfq_number, "tid": tracking.gmail_thread_id},
)
session.commit()

# At this point the user can refresh the addon and see the linked RFQ,
# navigate to the RFQ detail page, etc. — even before items are extracted.
```

**Notes:**
- `rfq_number` is auto-generated by `_next_rfq_number_sync()` inside `_create_rfq_sync()` — not passed in
- `assigned_to` uses `tracking.user_email` (the mailbox owner), not the add-on user — so if Alice helps Bob, the RFQ is assigned to Bob
- The RFQ model's `thread_id` field is for Chainlit chat threads, NOT Gmail threads — do NOT set it here
- `customer_id` is resolved internally by `_create_rfq_sync` from the customer name lookup
- RFQ is created as an empty draft — items are added in Stage 3

### Stage 3: Extract Line Items (LLM Evaluation)

All extraction goes through the LLM. Deterministic HTML table / CSV parsing was considered but rejected — it produces false positives (many emails have tables that aren't item lists) and misses items in freeform text, PDFs, and images.

```python
from includes.tools.supplier_quote_pipeline import _extract_email_content_sync
from includes.tools.rfq_item_import import _normalize_to_standard_columns
from includes.email_pipeline import llm_call_with_retry

# Build content bundle (email body + PDF/image attachments + spreadsheets)
# NOTE: internally uses QUOTE pipeline models for vision/PDF processing
content_bundle = _extract_email_content_sync(email_tracking_id)

# LLM extraction — uses RFQ_CREATION pipeline model settings
extraction_prompt = _build_extraction_prompt(content_bundle)
response = llm_call_with_retry(
    pipeline="RFQ_CREATION",
    step="extract",
    contents=extraction_prompt,
    temperature=0.1,
    timeout=120000,
)
result = _parse_extraction_response(response.text)
# Returns:
# {
#   "items": [{input_description, input_code, brand, quantity, uom, confidence}, ...],
#   "warnings": ["Customer references previous order — items not extractable", ...],
#   "has_items": true/false,
#   "title": "Komatsu PC200 engine parts",  # concise RFQ title derived from content
#   "customer_notes": "Required by 15 Aug. All parts must be genuine Komatsu OEM."
# }

items = []
if result.get("has_items") and result.get("items"):
    items = _normalize_to_standard_columns(result["items"])["items"]

extraction_method = "gemini_llm"

# Deduplicate by (description + part_number)
items = _deduplicate_items(items)
```

**LLM prompt requirements** (`config/prompts/rfq_creation_extract.md`):
- Extract all line items with 5 standard fields: `input_description`, `input_code`, `brand`, `quantity`, `uom`
- Report confidence per item (`high` / `medium` / `low`)
- Report `warnings[]` — only for genuinely problematic items (e.g. quantity missing entirely, ambiguous reference like "same as last order"). Missing part codes or brands are NOT warnings — many items are adequately described by description alone (e.g. "M16 bolts")
- Set `has_items: false` if the email is a general enquiry with no extractable items
- Extract `title` — a concise description of what the RFQ is about (e.g. "Komatsu PC200 engine parts", "Hydraulic fittings and hoses", "Caterpillar undercarriage components"). Derived from the overall theme of the requested items, not a copy of the email subject.
- Extract `customer_notes` — any customer requirements, delivery dates, conditions, or context that applies to the whole request. Examples:
  - "Required by 15 August 2026"
  - "All parts must be genuine OEM, no aftermarket"
  - "Delivery to Port Moresby warehouse"
  - "Budget approval pending — quote only, no order yet"
  - Combine multiple relevant notes into one block of text
  - Set to empty string if no relevant requirements found
- Handle edge cases:
  - Items in plain text ("we need 50x M12 bolts and 100x washers")
  - Items in PDF / image attachments (already in content bundle)
  - Partial references ("same items as last order" → flag as needs-review)
  - Multiple formats in one email (table + freeform text + attachment)

After extraction, items and notes are saved to the RFQ created in Stage 2:

```python
# Bulk-insert items into the already-created RFQ (if any were extracted)
if items:
    _add_items_sync(
        rfq_number=rfq_number,
        data={"items": items},
        user_id=user_id,
    )

# Update RFQ title and notes from LLM extraction
updates = {}
if result.get("title"):
    updates["title"] = result["title"]
if result.get("customer_notes"):
    updates["notes"] = result["customer_notes"]
if updates:
    _update_rfq_sync(rfq_number, updates, user_id)
```

### Stage 4: Save Result

```python
result = {
    "rfq_number": rfq_number,
    "items_extracted": len(items),
    "customer": customer.companyname,
    "status": "complete",
    "extraction_method": "gemini_llm",
    "title": result.get("title", ""),           # LLM-derived RFQ title
    "customer_notes": result.get("customer_notes", ""),  # requirements/delivery/context
    "raw_items": items,                          # store for debugging
    "warnings": result.get("warnings", []),      # LLM flags (e.g. unextractable references)
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

1. **Rename `notes` → `title` + add new `notes` field** — migration, model update, update all code/template references
2. **Migration + model** — add `rfq_creation_result` column to EmailTracking
3. **Pipeline file** — `includes/tools/rfq_creation_pipeline.py` with guard checks, RFQ creation, LLM item extraction, notes extraction, result saving
4. **API endpoint** — `POST /api/addon/create-rfq` in `addon.py`
5. **Apps Script** — `onCreateRfq` implementation
6. **Tests** — unit tests for guard checks, integration test for full pipeline

---

## Deferred to Future

- **Stage 0: Automated classification** — LLM triage to determine if an email is a new quote request (mirrors `_classify_supplier_email_sync()`). Currently manual (user clicks the button).
- **LLM-suggested subject override** — if the email subject is "Re: Quick question", the LLM could suggest a better RFQ title. (Partially addressed: LLM now extracts a `title` from content, but email subject is still used as `reference`.)
- **Multi-attachment quote requests** — if a customer sends a cover email + attached spreadsheet with items, handle both.
- **Support for multiple customers on one RFQ** — current assumption is one customer per RFQ (matching existing model).

---

## Estimated Scope

- **Core pipeline** (guard checks, two-pass extraction, RFQ creation, result saving): ~1-2 sessions
- **API endpoint + Apps Script**: ~1 session
- **Migration + model update**: 1 small PR
- **Tests**: ~1 session
