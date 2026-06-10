# Gmail Integration Plan

## Overview
Integrate EagleAgent with Gmail using domain-wide delegation to:
- Create draft emails via Gmail API with custom tracking headers
- Provide dual-path compose UX (in-app rich editor + Gmail draft handoff)
- Send emails directly via API from the in-app editor path
- Read and scan mailboxes for threads/new emails based on Opportunity ID or RFQ number
- Track sent emails against RFQ records with persistent mapping

## Architecture Decision: Dual-Path Workflow (In-App + Gmail)

EagleAgent now supports two compose/send paths from the same modal:
- **In-app editor path** for fast edits and direct send via Gmail API
- **Gmail draft path** for advanced edits, attachments, and Gmail-native compose controls

This approach:
- Preserves fast workflow for most supplier outreach messages
- Keeps Gmail-native capability available for edge cases (attachments/advanced formatting)
- Avoids full-automation concerns for first-contact emails
- Scales toward automation once templates/process are mature
- Keeps an audit trail across both draft and direct-send events

```
Agent generates email content (template + dynamic data)
  ↓
Modal opens with rich in-app editor + actions:
  - Send (direct API send)
  - Save to Draft in Gmail
  - Edit Draft in Gmail
  ↓
Path A (in-app send): messages.send → store sent message_id/thread_id in email_tracking
Path B (Gmail path): drafts.create → open #drafts/<messageId> → user sends in Gmail
  ↓
Mailbox sync/post-send detection associates outgoing events to RFQ tracking records
  ↓
Reply tracking uses thread IDs + fallback token matching
```

**Why dual-path matters for the RFQ use case:**
- Quote Requests to suppliers are first-contact emails — trust and relationship matter
- User can quickly edit/send in-app for routine cases
- User can switch to Gmail for attachments and advanced edits when needed
- Reduces perceived "bot" risk with customers (personal touch maintained)
- Future: once templates are proven, we can toggle to auto-send for repeat contacts

---

## Authentication & Scopes

### Gmail Service Account with Domain-Wide Delegation
- **Service account**: `service-account-key.json` (already in project)
- **Email impersonation**: Service account impersonates individual staff users
- **Domain**: `eagle-exports.com` (all staff mailboxes)

### Required Scopes (to configure in Google Workspace Admin Console)
- `https://www.googleapis.com/auth/gmail.modify` — read/write messages, create drafts, manage labels
- `https://www.googleapis.com/auth/gmail.send` — send on behalf of users (currently scoped but may not be needed for draft workflow)
- `https://www.googleapis.com/auth/gmail.readonly` — read mailbox history (for reply tracking)

**Setup steps:**
1. Go to Google Workspace Admin Console
2. Navigate to **Security** → **API Controls** → **Domain-wide Delegation**
3. Find the service account with client ID from `service-account-key.json`
4. Click **Edit** and add the three scopes above
5. Verify no errors; scopes should show as "Authorized"

### Status Check Script
- Script: `scripts/test_gmail_auth.py`
- Verify scopes are authorized
- Test draft creation with custom headers
- Test impersonation of a staff user

---

## Data Model

### email_tracking table
```sql
CREATE TABLE email_tracking (
    id              SERIAL PRIMARY KEY,
    
    -- Gmail identifiers
    gmail_thread_id VARCHAR NOT NULL,        -- Gmail thread ID
    gmail_message_id VARCHAR,                -- Message ID (populated post-send)
    gmail_draft_id   VARCHAR,                -- Draft ID (populated on create)
    gmail_history_id BIGINT,                 -- Last seen history event for tracking
    gmail_label      VARCHAR DEFAULT 'agent-rfq',
    
    -- User & context
    user_email      VARCHAR NOT NULL,        -- Staff member's email (impersonation)
    
    -- RFQ/Opportunity tracking
    rfq_id          VARCHAR NOT NULL,        -- RFQ ID (primary tracking)
    opportunity_id  VARCHAR,                 -- Fallback: external opportunity/deal ID
    rfq_token       VARCHAR,                 -- e.g. RFQ-12345 (in subject line)
    
    -- Email metadata
    direction       VARCHAR NOT NULL,        -- 'draft' | 'sent' | 'received'
    email_type      VARCHAR NOT NULL,        -- 'rfq_outreach' | 'quote' | 'invoice' | etc.
    subject         VARCHAR,
    recipient_email VARCHAR,
    sent_at         TIMESTAMP,               -- When actually sent by user
    
    -- Workflow state
    draft_url       VARCHAR,                 -- Compose link sent to user
    draft_opened_at TIMESTAMP,               -- When user opened the modal
    sent_confirmed  BOOLEAN DEFAULT FALSE,   -- User confirmed send in Gmail
    
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW(),
    
    UNIQUE(gmail_message_id),
    UNIQUE(gmail_draft_id)
);

CREATE INDEX ix_email_tracking_rfq ON email_tracking(rfq_id);
CREATE INDEX ix_email_tracking_thread ON email_tracking(gmail_thread_id);
CREATE INDEX ix_email_tracking_draft ON email_tracking(gmail_draft_id);
CREATE INDEX ix_email_tracking_opportunity ON email_tracking(opportunity_id);

CREATE TABLE mailbox_sync_cursor (
    user_email       VARCHAR PRIMARY KEY,
    last_history_id  BIGINT NOT NULL,
    updated_at       TIMESTAMP DEFAULT NOW()
);
```

---

## Phase 1: Authentication & Connectivity ✅ COMPLETE

### 1.1 Verify domain-wide delegation setup
- [x] Confirm service account has access to `service-account-key.json`
- [x] Confirm scopes are authorized in Google Workspace Admin Console:
  - [x] `https://www.googleapis.com/auth/gmail.modify`
  - [x] `https://www.googleapis.com/auth/gmail.readonly`
  - [x] `https://www.googleapis.com/auth/gmail.send`
- [x] Scopes documented and verified (tested successfully with harry@eagle-exports.com)

**Status**: Domain-wide delegation working. Service account properly configured with all required Gmail scopes in Google Workspace Admin Console.

### 1.2 Install Google libraries ✅
- [x] `google-api-python-client>=2.100.0` added to `pyproject.toml`
- [x] `google-auth` and related libraries installed
- [x] Verified in virtual environment

### 1.3 Create Gmail client wrapper ✅
- [x] File: `includes/gmail/__init__.py`
- [x] `get_credentials(user_email: str)` — creates delegated credentials using `with_subject()`
- [x] `get_gmail_client(user_email: str)` — returns authenticated Gmail API client
- [x] `test_connection(user_email: str)` — verifies auth by fetching profile and drafts
- [x] Error handling for auth failures and rate limits
- [x] Logging for all API calls
- [x] Credentials caching in memory

**Implementation note**: Uses service account with `with_subject(email)` for domain-wide delegation impersonation.

### 1.4 Test connectivity script ✅
- [x] File: `scripts/test_gmail_auth.py`
- [x] Tests impersonation of a real staff mailbox (successfully tested with harry@eagle-exports.com)
- [x] Creates test draft emails
- [x] Verifies custom headers are readable
- [x] Cleans up test drafts
- [x] Reports success/failure with actionable errors

**Latest test result**: ✅ All tests passing with harry@eagle-exports.com
- Service account loading: ✓
- Credential creation: ✓
- Gmail API connectivity: ✓
- Draft CRUD operations: ✓

---

## Phase 2: Draft Creation & User Handoff ✅ COMPLETE

### 2.1 Draft creation function ✅
- [x] File: `includes/gmail/draft_service.py`
- [x] Function: `create_draft_email(user_email, recipient_email, subject, body_html, rfq_id, email_type, opportunity_id, body_plain) -> dict`
- [x] Parameters all implemented with defaults
- [x] Returns: `{ status, draft_id, thread_id, compose_url, message }`
- [x] Implementation:
  - [x] Build email message with custom headers:
    - [x] `X-Eagle-OP: <rfq_id>`
    - [x] `X-Eagle-Type: <email_type>`
    - [x] `X-Eagle-RFQ: <rfq_id>`
    - [x] `X-Eagle-Opportunity: <opportunity_id>` (optional)
  - [x] Create draft via `users().drafts().create()`
  - [x] Extract `draftId` and `threadId`
  - [x] Generate compose URL for browser modal
  - [x] Store in `email_tracking` table as direction='draft'
  - [x] Log to database: draft URL, created_at, etc.

**Status**: ✅ Tested and working end-to-end. Creates drafts with proper headers, persists to database, generates correct compose URLs.

### 2.2 Compose URL generation ✅
- [x] Function: `generate_compose_url(message_id, user_email) -> str`
- [x] URL format (current): `https://mail.google.com/mail/u/?authuser=${encodeURIComponent(userEmail)}#drafts/${messageId}`
- [x] Special characters in email are properly URL-encoded
- [x] Uses reliable existing-draft open behavior (full Gmail UI)

### 2.3 Frontend modal integration ✅ COMPLETE
- **UI**: RFQ detail page + supplier lists now use dual actions: `Edit` and `Edit in Gmail`.
- **Flow (implemented)**:
  1. User clicks `Edit` (in-app editor path) or `Edit in Gmail` (quick Gmail path)
  2. Subject is normalized with tracking token (`[RFQ-...]`)
  3. Rich HTML body (including product table) is injected from supplier template content
  4. Body tracking marker is appended for scanner fallback
  5. In-app editor path supports:
     - `Send` (direct Gmail API send)
     - `Save to Draft in Gmail`
     - `Edit Draft in Gmail`
  6. Gmail path opens exact draft via popup route (`#drafts/<messageId>`)
  7. Modal only closes intentionally (X/Discard/Done), preventing accidental data loss

- **Editor behaviors now implemented**:
  - Free no-key rich editor (Jodit) in modal
  - Expand/collapse compose view (~80% expanded)
  - Editable `To` field
  - Styled table insertion picker (grid-based)
  - Outgoing HTML normalization for consistent 14px font and visible table formatting

- **Implemented files**:
  - `templates/partials/_rfq_email_suppliers.html`
  - `templates/partials/_rfq_items_table.html`
  - `templates/partials/_email_compose_modal.html`
  - `templates/base.html`
  - `includes/dashboard/routes/rfqs.py`

### 2.3.1 Dual-path email compose UX ✅ COMPLETE
- **Delivered outcomes**:
  - Reusable in-app email editor modal component implemented
  - Direct-send API endpoint implemented: `POST /api/rfqs/{rfq_id}/send-email-direct`
  - Draft-save + draft-open actions implemented in modal footer
  - RFQ supplier buttons updated to `Edit` + `Edit in Gmail`
  - API-sent tracking persisted to `email_tracking` as `direction='sent'`

- **Tracking behavior (implemented)**:
  - Subject token remains mandatory (`[RFQ-...]`)
  - Body tracking marker remains present as scanner fallback key
  - `email_tracking` remains source of truth for draft/sent/reply lifecycle

- **Remaining follow-on work**:
  - Reuse same component pattern across additional non-RFQ email surfaces
  - Complete Phase 3 mailbox sync/status surfacing

### 2.4 Post-send detection (polling + sync job) ⏳ PHASE 3
- **Quick detection (optional poll)**: After draft created, can poll every 5-10 seconds for ~5 minutes
  - Check if draft was sent (moved from drafts to sent folder)
  - Read sent message to extract headers and confirm RFQ association
  - Move `email_tracking` row to 'sent' status, populate `gmail_message_id`, `sent_at`
- **Reliable detection (mailbox sync)**: Primary detection via Phase 3 mailbox scanning job
  - Next scheduled sync (every 5 min) will detect outgoing message in sent folder
  - Read headers to match to RFQ and update `email_tracking`
  - Guarantees we never miss a sent email, even if user closes modal immediately
  - Approach: treat sent messages as outgoing events in history delta, log them if not already tracked

---

## Phase 3: Mailbox Scanning & Reply Tracking

### 3.1 Mailbox sync cursor setup
- Initialize `mailbox_sync_cursor` for each staff user
- On first run, seed with `historyId` from most recent message (avoid scanning all history)
- Store `updated_at` to track sync recency

### 3.2 Incremental sync job
- **Trigger**: Background job every 5 minutes
- **Per user**: Call `users().history().list(startHistoryId=last_cursor)` with label `agent-rfq`
- **Process**:
  - Iterate over `messageAdded` events
  - For each event, fetch message details (subject, headers, thread)
  - If direction is outgoing (check headers), log to `email_tracking` if not already logged
  - If direction is incoming (reply), check for tracking in `email_tracking` by threadId
  - If tracked, mark as 'received' in `email_tracking`
  - If not tracked, attempt fallback: search by RFQ token in subject
- **Update cursor**: After successful processing, store new `historyId` in `mailbox_sync_cursor`

### 3.3 Mailbox search by Opportunity ID
- Function: `search_thread_by_rfq(user_email, rfq_id, limit=10) -> list[dict]`
- Search modes (in order of priority):
  1. Query `email_tracking` by `rfq_id` → return all threads
  2. Query Gmail by `X-Eagle-RFQ` header (via `users().messages().get(format='full')` header extraction)
  3. Query Gmail by RFQ token in subject `[RFQ-<id>]`
  4. Fallback: query body marker text `Tracking ID: RFQ-<id>`
- Returns: list of threads with metadata: threadId, subject, participants, dates, last message preview

### 3.4 Thread details & message content
- Function: `get_thread_messages(user_email, thread_id) -> list[dict]`
- Fetch full thread from Gmail
- Normalize message data (extract headers, body, sender, timestamps)
- Return array of messages in chronological order
- Useful for: displaying conversation history in RFQ UI, detecting customer decisions

---

## Phase 4: RFQ Integration

### 4.1 RFQ model changes ✅ REFACTORED

**Original design issue**: Single-thread fields (`email_thread_id`, `email_draft_id`) don't work for one-RFQ-to-many-threads reality.

**Current design** (refined after Phase 2):
- [x] `email_status` (VARCHAR) — aggregate status for UI display
  - Values: 'no_email_sent' | 'draft_pending' | 'sent' | 'awaiting_reply' | 'auto_closed'
  - Used for quick filtering and status indicators on RFQ list/detail
- [x] `last_email_sent_at` (TIMESTAMP) — most recent email send time
  - Used for sorting by email activity
  - Updated whenever a new email is sent to any supplier on this RFQ
- [x] `supplier_emails` (JSONB) — array of supplier contact info (for multi-supplier RFQs)
  - Format: `[{"email": "supplier1@example.com", "name": "Company A"}, ...]`
  - Denormalized for quick access without joining to suppliers table

**Removed fields** (single-thread design, now only in email_tracking):
- ❌ `email_thread_id` — Each supplier has their own thread; use email_tracking table for full history
- ❌ `email_draft_id` — Belongs in email_tracking table only
- ❌ `email_sent_at` → renamed to `last_email_sent_at` for clarity

**Architectural principle**: 
- **RFQ table**: Denormalization/summary fields for UI and quick queries
- **email_tracking table**: Source of truth for ALL email lifecycle events (draft, sent, received, replied)
  - Supports one RFQ → many threads (one per supplier + email type)
  - Full thread history, header extraction, message IDs, all timestamps

**Alembic migrations**:
- [x] `b2c3d4e5f6a7_add_email_tracking_fields_to_rfqs.py` — initial fields added
- [x] `c3d4e5f6a7b8_refactor_rfq_email_fields_for_many_threads.py` — refactored to summary-only fields
- [x] Both migrations applied successfully to PostgreSQL

### 4.2 RFQ UI enhancements
- **RFQ detail page**:
  - [x] "Email Suppliers" panel with dual actions (`Edit`, `Edit in Gmail`)
  - [x] Reusable rich compose modal with loading/editor/result/error states
  - [x] Buttons in both supplier row and supplier-contact popover
  - [x] Rich email template injection (tables, formatting, signature)
  - [x] Safer close behavior (no outside-click/Escape accidental close)
  - [x] Tabbed RFQ subviews with stable routes:
    - `/rfqs/{rfq_id}/items`
    - `/rfqs/{rfq_id}/suppliers`
    - `/rfqs/{rfq_id}/communications`
    - `/rfqs/{rfq_id}/quotation`
  - [x] Communications tab shows logged events from `email_tracking` (draft/sent visibility)
  - [ ] Show reply thread messages inline (full thread drilldown)
  - [ ] Show per-supplier sent/replied status indicators
- **RFQ list page**:
  - [ ] Add filter: "Has email sent" / "Awaiting supplier replies"
  - [ ] Show email status indicator per RFQ

### 4.3 Agent integration
- **ProcurementAgent** has new tool: `create_and_open_supplier_email_draft`
  - Input: RFQ ID, recipient email, message intent
  - Agent composes email content
  - Agent calls draft creation tool (one draft per supplier)
  - Agent provides user with compose URL
  - UI modal opens the URL
- **Integration tasks**:
  - Update agent system prompt to include email capabilities
  - Add tool definition for draft creation
  - Update ProcurementAgent.get_tools() to return email tools

---

## Phase 5: Email Templates & Personalization

### 5.1 Email template system
- Directory: `includes/gmail/templates/`
- Base template: `supplier_rfq_request.html`
- Features:
  - Insert dynamic RFQ data (items, quantities, required dates)
  - Supplier-specific greeting and context
  - Tone: professional but personable
  - Footer with Eagle Exports branding
- Template rendering: Jinja2

### 5.2 AI-powered draft generation
- Use Gemini to personalize templates based on supplier profile
- Supplier history: past orders, lead time, quality rating
- Tone adjustment: formal vs. casual depending on relationship
- Optional: AI rewrites subject line for better response rates

### 5.3 Future: NetSuite Opportunities & Customers sync
- As part of separate NetSuite integration plan, will sync local DB tables for:
  - NetSuite Opportunities (analogs to RFQs)
  - NetSuite Customers (analogs to suppliers)
- Will enable linking email tracking to NetSuite entities (not just RFQs)
- Will provide richer context for email personalization
- Implementation deferred to NetSuite plan; noted here for continuity

---

## Phase 6: Future Enhancements

### 6.1 Automated sending (once templates proven)
- For known suppliers (repeat contacts, proven templates):
  - Auto-send instead of draft
  - Require supervisor approval for new suppliers
  - Log as 'sent' (direction='sent') immediately

### 6.2 Email content analysis
- On reply receipt: AI summarizes response
- Extract key data: lead times, quotes, conditions
- Populate RFQ data fields from supplier responses

### 6.3 NetSuite Integration (future)
- Link Gmail tracking to NetSuite Opportunities and Customers (once tables are synced)
- Log sent/received emails in email_tracking with opportunity/customer references
- Unified email audit trail linked to deal/quote lifecycle in NetSuite
- See upcoming NetSuite integration plan for scope and timeline

### 6.4 Attachment handling
- Allow user to attach files when opening modal
- Future: AI attaches relevant documents (RFQ PDFs, drawings)
- Track attachment names in email_tracking

---

## Implementation Order
1. **Phase 1** — Auth + connectivity (confirm scopes, test)
2. **Phase 2** — Draft creation + modal UI
3. **Phase 3** — Mailbox scanning + reply tracking (polling job every 5 min)
4. **Phase 4** — RFQ database + UI integration
5. **Phase 5** — Email templates + personalization
6. **Phase 6** — Future enhancements (auto-send, content analysis, NetSuite linking)

**Note on post-send detection:** Primary mechanism is Phase 3 mailbox sync job (reliable, never misses emails). Optional quick-poll (5 min) can run in parallel during Phase 2 for faster user feedback that email was sent.

---

## Setup Tasks (Manual, One-Time)

### Google Workspace Admin Console
- [ ] Navigate to **Security** → **API Controls** → **Domain-wide Delegation**
- [ ] Locate service account (get client ID from `service-account-key.json`)
- [ ] Click **Edit** on the service account
- [ ] Add these scopes:
  ```
  https://www.googleapis.com/auth/gmail.modify
  https://www.googleapis.com/auth/gmail.readonly
  https://www.googleapis.com/auth/gmail.send
  ```
- [ ] Click **Save**
- [ ] Wait ~5-10 minutes for propagation
- [ ] Run `scripts/test_gmail_auth.py` to confirm

### Database
- [ ] Run Alembic migration to add RFQ email fields
- [ ] Create `email_tracking` and `mailbox_sync_cursor` tables

---

## Configuration Variables

Add to `.env`:
```bash
# Gmail Integration (uses domain-wide delegation)
# Service account email (extract from service-account-key.json)
GMAIL_SERVICE_ACCOUNT_EMAIL=eagle-agent@eagle-exports.iam.gserviceaccount.com

# Domain for impersonation (can impersonate any @eagle-exports.com user)
GMAIL_DOMAIN=eagle-exports.com

# Default sender email (used if no specific staff member assigned to RFQ)
GMAIL_DEFAULT_SENDER=rfq-support@eagle-exports.com

# Enable draft-first workflow (true) or auto-send (false, future)
GMAIL_DRAFT_FIRST=true

# Mailbox sync interval (seconds)
GMAIL_SYNC_INTERVAL=300

# Compose URL timeout for polling (seconds)
GMAIL_COMPOSE_POLL_TIMEOUT=300
```

---

## Testing Strategy

### Unit Tests
- `tests/gmail/test_draft_service.py`
  - Test draft creation with headers
  - Test URL generation
  - Test header extraction
- `tests/gmail/test_mailbox_sync.py`
  - Test history delta parsing
  - Test thread matching
  - Test fallback search by subject

### Integration Tests
- `tests/gmail/test_gmail_workflow_e2e.py`
  - Create draft → send → detect in mailbox
  - Create draft → reply from test account → detect reply
  - Multiple suppliers: verify thread isolation

### Manual Testing
- [ ] Create draft to real supplier email
- [ ] Open compose link in browser
- [ ] Edit and send from Gmail UI
- [ ] Verify `email_tracking` shows sent status
- [ ] Reply from test account
- [ ] Verify reply is detected by sync job

---

## Open Questions & Decisions

1. **Draft polling timeout**: How long should we poll for user sending? (Currently 5 min)
   - Could be shorter (1-2 min) if users are expected to send immediately
   - Could be longer if user might review offline

2. **Multiple suppliers per RFQ**: 
   - Should drafts be created for all suppliers at once, or one-by-one?
   - Recommendation: one-by-one with user confirmation between each

3. **Email template repository**: ✅ DECIDED
   - Store in version control (`includes/gmail/templates/`)
   - Can migrate to database for easy editing in the future

4. **Gmail History API expiration handling**: ✅ EXPLAINED
   - Gmail's `historyId` is valid for ~7 days of inactivity
   - If we don't sync a mailbox for >7 days, the stored `historyId` becomes invalid
   - When this happens, Gmail returns 404 error on history.list()
   - **Recovery**: Catch the error, reseed `last_history_id` by fetching the most recent message in the `agent-rfq` label
   - Then resume normal incremental sync from that point
   - This is a rare edge case (should sync at least every 5 min) but must be handled

5. **NetSuite alignment**: ✅ CURRENT DIRECTION
  - Local DB sync for NetSuite Opportunities and Customers is planned
  - Primary fields will be synced for search/linking (similar to current Suppliers table)
  - Email tracking will link to NetSuite entities in addition to RFQs
  - See Section 5.3 above and upcoming NetSuite integration plan

6. **Custom email headers visibility**: ✅ EXPLAINED
  - `X-Eagle-*` headers are embedded in API-created draft MIME content.
  - Gmail UI sending may strip custom headers from externally delivered SMTP message headers.
  - Subject token + body tracking text remain the primary resilient matching keys.
  - For fully automated sends (`drafts.send` / `messages.send`), scanner can rely on Sent-copy metadata + immediate API response IDs.

---

## Success Criteria

### Phase 1 ✅ COMPLETE
- [x] Service account scopes confirmed in Google Workspace Admin
- [x] `test_gmail_auth.py` passes (draft creation, impersonation, header read)
- [x] Domain-wide delegation verified working with real staff mailbox

### Phase 2 ✅ COMPLETE
- [x] Draft creation function working end-to-end
- [x] Draft URL generates correctly and opens in Gmail
- [x] User can edit draft and send from Gmail UI
- [x] In-app rich editor path can send directly via Gmail API
- [x] Custom headers embedded in draft MIME messages (`X-Eagle-*`)
- [x] Drafts persist to `email_tracking` table
- [x] Direct sends persist to `email_tracking` table as `direction='sent'`
- [x] `test_gmail_draft.py` passes all tests
- [x] RFQ schema supports many-threads design
- [x] RFQ supplier views use `Edit` + `Edit in Gmail` actions
- [x] Modal supports both in-app send and Gmail draft handoff
- [x] Product table + formatted HTML template injected into draft body
- [x] Tracking token added to subject and body footer
- [x] Modal UX finalized (editor, spinner, result, retry, intentional-close controls)

### Phase 3 ⏳ NOT YET STARTED
- [ ] Post-send detection populates `email_tracking` within 1 minute (via optional polling)
- [ ] Mailbox sync job detects sent emails within 5 minutes
- [ ] Supplier reply is detected by sync job within 5 minutes

### Phase 4 🟡 IN PROGRESS
- [x] "Send Email" buttons create draft and open modal
- [x] RFQ tab routes implemented for Items/Suppliers/Communications/Quotation
- [x] RFQ Communications tab shows logged email events for testing visibility
- [ ] RFQ UI shows reply thread history details and status indicators

### End-to-End ⏳ PENDING PHASES 3-4
- [ ] Create RFQ → send to supplier → reply received → verified in UI
