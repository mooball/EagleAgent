# HubSpot Integration Plan

## Overview
Integrate EagleAgent with HubSpot CRM to:
- Create/update Deals from RFQs
- Send emails to suppliers (RFQ outreach) and customers (invoices/quotes) via Gmail
- Log all sent emails and replies against specific HubSpot Deals
- Sync supplier/customer contacts bidirectionally

## Architecture Decision: Email

HubSpot does NOT send emails itself — it only logs them. HubSpot's auto-association of
inbox emails to deals is unreliable (client-confirmed). We use **explicit logging**:

```
EagleAgent sends via Gmail API (domain-wide delegation, impersonating staff user)
        ↓
Email logged to HubSpot immediately via Engagements API (explicit deal + contact association)
        ↓
gmail_thread_id stored in our DB → tracked for replies
        ↓
Poll job (every 5 min) checks tracked threads for new messages
        ↓
Reply detected → logged to HubSpot with same deal association
```

**Why this approach:**
- 100% reliable deal association (we control which deal, not HubSpot guessing)
- Works when a supplier has multiple active deals
- No per-user OAuth flow — service account impersonates any domain user
- Same pattern handles supplier outreach AND customer invoices
- Scales to 20+ users with minimal API calls (batch thread checks)

---

## Authentication

### HubSpot API
- **Method**: Service Key (replacement for deprecated Private Apps)
- **Storage**: Environment variable `HUBSPOT_ACCESS_TOKEN` in `.env`
- **Token lifespan**: Long-lived, no refresh needed
- **Scopes**:
  - `crm.objects.deals.read` / `write`
  - `crm.objects.contacts.read` / `write`
  - `crm.schemas.deals.read`
  - `sales-email-read`

### Gmail API (sending + reply tracking)
- **Method**: Google Service Account with Domain-Wide Delegation
- **Service account**: `service-account-key.json` (already in project)
- **Delegation scopes to configure in Google Admin**:
  - `https://www.googleapis.com/auth/gmail.send` (send as user)
  - `https://www.googleapis.com/auth/gmail.readonly` (read tracked threads)
- **Domain**: `eagle-exports.com` (all staff mailboxes)
- **Impersonation**: Service account impersonates individual staff users when sending/reading

## Dependencies
```
hubspot-api-client   # Official HubSpot Python SDK
google-auth          # Already installed (Google OAuth)
google-api-python-client  # Gmail API client
```

---

## Data Model

### email_tracking table
```sql
CREATE TABLE email_tracking (
    id              SERIAL PRIMARY KEY,
    gmail_thread_id VARCHAR NOT NULL,        -- Gmail thread to monitor
    gmail_message_id VARCHAR NOT NULL,       -- Specific message ID
    user_email      VARCHAR NOT NULL,        -- Staff member's email (for impersonation)
    hubspot_deal_id VARCHAR NOT NULL,        -- HubSpot deal to associate with
    hubspot_contact_id VARCHAR,             -- HubSpot contact (supplier/customer)
    direction       VARCHAR NOT NULL,        -- 'sent' | 'received'
    purpose         VARCHAR NOT NULL,        -- 'rfq_outreach' | 'customer_invoice' | etc.
    subject         VARCHAR,
    recipient_email VARCHAR,
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE(gmail_message_id)
);

CREATE INDEX ix_email_tracking_thread ON email_tracking(gmail_thread_id);
CREATE INDEX ix_email_tracking_deal ON email_tracking(hubspot_deal_id);
```

---

## Phase 1: Authentication & Connectivity ✅ COMPLETE

### 1.1 Install SDK & configure credentials
- [x] Added `hubspot-api-client` to `pyproject.toml`
- [x] Added `HUBSPOT_ACCESS_TOKEN` to `.env` and `config/settings.py`
- [x] Created `includes/hubspot/__init__.py` with client wrapper

### 1.2 Test connectivity
- [x] Script: `scripts/test_hubspot_auth.py`
- [x] Verified token — Portal 41681098, AUD, Australia/Brisbane
- [x] Confirmed pipelines: "Netsuite pipeline" + "New Business Pipeline"
- [x] Confirmed 271 deal properties, stages match expected

---

## Phase 2: Deal Sync

### 2.1 Map RFQ fields → Deal properties
- Explore the 271 existing deal properties, identify which map to RFQ fields
- Document mapping: RFQ model field → HubSpot property name
- Determine which pipeline to use (Netsuite pipeline for existing, New Business for new?)

### 2.2 Create Deals from RFQs
- API: `POST /crm/v3/objects/deals`
- Required: `dealname`, `pipeline`, `dealstage`, `amount`
- Custom properties: rfq_number, supplier count, etc.
- Associate deals with contacts (supplier contacts)
- Store `hubspot_deal_id` on RFQ model

### 2.3 Update Deal stages as RFQ progresses
- API: `PATCH /crm/v3/objects/deals/{id}`
- Map RFQ status → HubSpot deal stage
- Stage mapping:
  - New Deal → RFQ created
  - In progress - Supplier → Sending to suppliers
  - In progress - Customer → Quotes received, preparing for customer
  - Quote Sent / Quote Sent >$5000 → Sent to customer
  - In Negotiation → Customer negotiating
  - Closed won / Closed lost / Lost Customer → Final states

---

## Phase 3: Email Integration (Gmail + HubSpot logging)

### 3.1 Gmail service account setup
- Verify domain-wide delegation is active for service account
- Add Gmail scopes to delegation config in Google Admin:
  - `https://www.googleapis.com/auth/gmail.send`
  - `https://www.googleapis.com/auth/gmail.readonly`
- Test: send a test email impersonating a staff user
- Test: read threads from a staff mailbox

### 3.2 Send supplier outreach emails
- Compose email from RFQ data (AI-drafted or template)
- Send via Gmail API impersonating the assigned staff member
- Store `gmail_thread_id` + `gmail_message_id` in `email_tracking` table
- Log to HubSpot via Engagements API with explicit deal + contact association

### 3.3 Reply tracking (poll job)
- Background job runs every 5 minutes
- Query `email_tracking` for active threads (grouped by user_email)
- Batch-fetch thread message counts via Gmail API (impersonating each user)
- For threads with new messages:
  - Fetch new message details
  - Log reply to HubSpot (same deal association)
  - Insert new row in `email_tracking` (direction='received')
- Performance: 20 users × ~50 active threads = 1000 thread checks, batched 100/request = ~10 API calls

### 3.4 Future: Customer invoice emails
- Same infrastructure, different `purpose` value
- Compose invoice/quote email → send → track → log
- No architectural changes needed

---

## Phase 4: Contact Sync

### 4.1 Supplier contacts → HubSpot
- Search HubSpot by email (avoid duplicates)
- Create/update contacts from supplier.contacts
- Map: supplier.name → company, contact fields
- Store `hubspot_contact_id` on supplier contact records

### 4.2 Associate contacts with deals
- API: `PUT /crm/v3/objects/deals/{dealId}/associations/contacts/{contactId}`
- Run when creating deals or sending emails

### 4.3 Customer contacts (future)
- Same pattern for customer-side contacts
- Needed for invoice email tracking

---

## Implementation Order
1. **Phase 1** — Auth + connectivity ✅ DONE
2. **Phase 2** — Deal sync (read properties, create/update deals)
3. **Phase 3** — Email (Gmail delegation → send → track replies → log to HubSpot)
4. **Phase 4** — Contact sync + associations

## Setup Tasks (one-time, manual)
- [x] Create HubSpot Service Key with scopes
- [x] Add token to `.env`
- [ ] Add Gmail scopes to domain-wide delegation in Google Admin Console:
  - Admin Console → Security → API Controls → Domain-wide Delegation
  - Find service account client ID → Edit → Add scopes:
    - `https://www.googleapis.com/auth/gmail.send`
    - `https://www.googleapis.com/auth/gmail.readonly`


## Open Questions
- ~~What deal pipeline/stages exist in your HubSpot portal?~~ **Answered — see below**
- ~~Are there custom deal properties already set up, or do we create them?~~ **All properties already exist. Will discover via API.**
- ~~Do you have transactional email enabled, or do we need to check?~~ **Believed enabled — will verify in Phase 1.**
- Which email templates should we use (or create new ones)?
- ~~Should contact sync be bidirectional (HubSpot → EagleAgent) or one-way?~~ **Bidirectional.**

## Deal Pipeline Stages
| Stage | RFQ Mapping (suggested) |
|-------|------------------------|
| New Deal | RFQ created |
| In progress - Supplier | Finding/contacting suppliers |
| In progress - Customer | Awaiting customer input |
| In Negotiation | Negotiating with suppliers |
| Quote Sent >$5000 | Quote sent, value > $5000 |
| Quote Sent | Quote sent, value ≤ $5000 |
| Closed won | RFQ awarded |
| Closed lost | RFQ lost |
| Lost Customer | Customer churned |
