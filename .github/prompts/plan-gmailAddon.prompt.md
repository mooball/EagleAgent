# Plan: Gmail Workspace Add-on (EagleAgent Sidebar)

**Goal**: Build a private Google Workspace Gmail Add-on for eagle-exports.com.au domain users that provides contextual email information and actions directly within the Gmail sidebar. Communicates with the existing FastAPI backend via JSON API endpoints, authenticated using OIDC identity tokens.

**Deployment model**: Domain-internal only (no Marketplace publishing). Deployed via Apps Script, installed by admin for select users.

**Development model**: Local development using `clasp` CLI. Apps Script source lives in `addon/` directory within this repo. Push to Google via `clasp push`.

---

## Architecture Overview

```
┌─────────────────────────────────┐       ┌─────────────────────────────────┐
│  Gmail Add-on (Apps Script)     │       │  EagleAgent (FastAPI)           │
│                                 │       │                                 │
│  • CardService UI               │──────▶│  POST /api/addon/context        │
│  • ScriptApp.getIdentityToken() │  JWT  │  POST /api/addon/link-email     │
│  • UrlFetchApp → backend        │◀──────│  POST /api/addon/create-rfq     │
│                                 │       │  GET  /api/addon/search         │
│  appsscript.json (manifest)     │       │                                 │
│  Scoped to eagle-exports.com.au │       │  google.auth.verify_id_token()  │
└─────────────────────────────────┘       └─────────────────────────────────┘
```

**Authentication flow**:
1. Apps Script calls `ScriptApp.getIdentityToken()` → returns a signed OIDC JWT
2. JWT sent as `Authorization: Bearer <token>` to FastAPI
3. FastAPI verifies token with `google.auth.transport.requests` + validates `hd` (hosted domain) claim is `eagle-exports.com.au`
4. No refresh tokens involved — Apps Script handles its own token lifecycle

---

## Phase 1: Authentication & Minimal Add-on (THIS PHASE)

### 1.0 Local Development Setup (`clasp`)

`clasp` (Command Line Apps Script Projects) allows local development and deployment without using the browser-based Apps Script editor.

**One-time setup:**

```bash
# Install clasp globally
npm install -g @google/clasp

# Login to Google (opens browser OAuth flow)
clasp login

# Create the Apps Script project (standalone, linked to GCP project later)
cd addon/
clasp create --title "Eagle Procurement" --type standalone
# This creates .clasp.json with the script ID
```

**Directory structure in this repo:**

```
addon/
├── .clasp.json          # clasp project config (scriptId, rootDir)
├── .claspignore         # files to exclude from push
├── appsscript.json      # Apps Script manifest (scopes, triggers, etc.)
├── Code.gs              # Main add-on logic (triggers, UI builders)
└── README.md            # Setup instructions for new developers
```

**`.clasp.json`** (created by `clasp create`, committed to repo):
```json
{
  "scriptId": "<SCRIPT_ID_FROM_GOOGLE>",
  "rootDir": "."
}
```

**`.claspignore`**:
```
README.md
.clasp.json
```

**Daily workflow:**

```bash
cd addon/

# Push local changes to Apps Script
clasp push

# Create a versioned deployment
clasp deploy --description "Phase 1 — context card"

# Open in browser (for debugging/logs)
clasp open

# Pull remote changes (if someone edited in browser)
clasp pull

# View logs (Stackdriver)
clasp logs
```

**Test deployment (developer only):**
- `clasp push` makes changes immediately available to the script owner via "Test Deployments" in the Apps Script editor
- Go to Extensions → Google Workspace Add-ons → Use test deployment
- No admin install needed for the developer during testing

**Production deployment (all target users):**
- `clasp deploy` creates a new immutable version
- Admin Console → Apps → Google Workspace Marketplace apps → Manage apps → Install internal app
- Select the deployment version → Assign to target users/OUs

### 1.1 Prerequisites (Manual / Admin Console)

- [ ] Link Apps Script project to existing GCP project (Project Settings → GCP Project Number)
- [ ] Enable "Google Workspace Marketplace SDK" API on GCP project
- [ ] Configure OAuth consent screen (internal to eagle-exports.com.au org)
  - App name: "Eagle Procurement Assistant"
  - User type: **Internal**
  - Scopes: `openid`, `email`, `profile`, `https://www.googleapis.com/auth/gmail.addons.current.message.readonly`
- [ ] Create Apps Script project at script.google.com (or use `clasp` for local dev)

### 1.2 Apps Script: Manifest (`appsscript.json`)

```json
{
  "timeZone": "Australia/Sydney",
  "exceptionLogging": "STACKDRIVER",
  "runtimeVersion": "V8",
  "oauthScopes": [
    "https://www.googleapis.com/auth/gmail.addons.current.message.readonly",
    "https://www.googleapis.com/auth/gmail.addons.current.message.metadata",
    "https://www.googleapis.com/auth/script.external_request",
    "openid",
    "email"
  ],
  "addOns": {
    "common": {
      "name": "Eagle Procurement",
      "logoUrl": "https://eagle-agent.mooball.net/static/avatars/eagle-icon.png",
      "layoutProperties": {
        "primaryColor": "#1a73e8"
      },
      "homepageTrigger": {
        "runFunction": "onHomepage"
      }
    },
    "gmail": {
      "contextualTriggers": [
        {
          "unconditional": {},
          "onTriggerFunction": "onGmailMessageOpen"
        }
      ]
    }
  }
}
```

### 1.3 Apps Script: Code (`Code.gs`)

```javascript
// ============================================================
// Configuration
// ============================================================
const BACKEND_URL = 'https://eagle-agent.mooball.net';

// ============================================================
// Helper: Authenticated fetch to backend
// ============================================================
function fetchBackend(path, payload) {
  const idToken = ScriptApp.getIdentityToken();
  if (!idToken) {
    throw new Error('Unable to get identity token. Ensure openid scope is granted.');
  }

  const options = {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'Authorization': 'Bearer ' + idToken
    },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  };

  const response = UrlFetchApp.fetch(BACKEND_URL + path, options);
  const code = response.getResponseCode();

  if (code === 401) {
    throw new Error('Authentication failed. Contact admin.');
  }
  if (code === 403) {
    throw new Error('Access denied. Only eagle-exports.com.au users allowed.');
  }
  if (code >= 400) {
    throw new Error('Backend error (' + code + '): ' + response.getContentText());
  }

  return JSON.parse(response.getContentText());
}

// ============================================================
// Trigger: Homepage (no message context)
// ============================================================
function onHomepage(e) {
  var card = CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader()
        .setTitle('Eagle Procurement')
        .setSubtitle('Open an email to see context')
    )
    .addSection(
      CardService.newCardSection()
        .addWidget(
          CardService.newTextParagraph()
            .setText('Select an email to view linked customers, suppliers, and RFQs.')
        )
    )
    .build();

  return [card];
}

// ============================================================
// Trigger: Message opened — fetch context from backend
// ============================================================
function onGmailMessageOpen(e) {
  // Get message metadata
  var accessToken = e.gmail.accessToken;
  GmailApp.setCurrentMessageAccessToken(accessToken);

  var messageId = e.gmail.messageId;
  var message = GmailApp.getMessageById(messageId);
  var subject = message.getSubject();
  var sender = message.getFrom();
  var threadId = message.getThread().getId();

  // Call backend for context
  var context;
  try {
    context = fetchBackend('/api/addon/context', {
      gmail_message_id: messageId,
      gmail_thread_id: threadId,
      subject: subject,
      sender: sender
    });
  } catch (err) {
    return [buildErrorCard(err.message)];
  }

  return [buildContextCard(context, messageId, threadId, subject, sender)];
}

// ============================================================
// UI: Build context card showing linked entities
// ============================================================
function buildContextCard(context, messageId, threadId, subject, sender) {
  var builder = CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader()
        .setTitle('Eagle Procurement')
        .setSubtitle(subject.substring(0, 60))
    );

  // Status section
  var statusSection = CardService.newCardSection().setHeader('Email Status');

  if (context.customer) {
    statusSection.addWidget(
      CardService.newDecoratedText()
        .setTopLabel('Customer')
        .setText(context.customer.name)
        .setStartIcon(CardService.newIconImage().setIcon(CardService.Icon.PERSON))
    );
  }

  if (context.supplier) {
    statusSection.addWidget(
      CardService.newDecoratedText()
        .setTopLabel('Supplier')
        .setText(context.supplier.name)
        .setStartIcon(CardService.newIconImage().setIcon(CardService.Icon.STAR))
    );
  }

  if (context.rfq) {
    statusSection.addWidget(
      CardService.newDecoratedText()
        .setTopLabel('RFQ')
        .setText(context.rfq.rfq_number + ' — ' + context.rfq.status)
        .setStartIcon(CardService.newIconImage().setIcon(CardService.Icon.DESCRIPTION))
    );
  }

  if (context.opportunity) {
    statusSection.addWidget(
      CardService.newDecoratedText()
        .setTopLabel('Opportunity')
        .setText(context.opportunity.title)
        .setStartIcon(CardService.newIconImage().setIcon(CardService.Icon.BOOKMARK))
    );
  }

  if (!context.customer && !context.supplier && !context.rfq && !context.opportunity) {
    statusSection.addWidget(
      CardService.newTextParagraph()
        .setText('<i>No linked entities found for this email.</i>')
    );
  }

  builder.addSection(statusSection);

  // Actions section (Phase 2 — placeholder buttons)
  var actionsSection = CardService.newCardSection().setHeader('Actions');

  actionsSection.addWidget(
    CardService.newTextButton()
      .setText('Link to Customer/Supplier')
      .setOnClickAction(
        CardService.newAction()
          .setFunctionName('onLinkEntity')
          .setParameters({
            messageId: messageId,
            threadId: threadId,
            subject: subject,
            sender: sender
          })
      )
  );

  actionsSection.addWidget(
    CardService.newTextButton()
      .setText('Link to RFQ')
      .setOnClickAction(
        CardService.newAction()
          .setFunctionName('onLinkRfq')
          .setParameters({
            messageId: messageId,
            threadId: threadId,
            subject: subject
          })
      )
  );

  actionsSection.addWidget(
    CardService.newTextButton()
      .setText('Create New RFQ')
      .setOnClickAction(
        CardService.newAction()
          .setFunctionName('onCreateRfq')
          .setParameters({
            messageId: messageId,
            threadId: threadId,
            subject: subject,
            sender: sender
          })
      )
  );

  builder.addSection(actionsSection);

  return builder.build();
}

// ============================================================
// UI: Error card
// ============================================================
function buildErrorCard(message) {
  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('Eagle Procurement'))
    .addSection(
      CardService.newCardSection()
        .addWidget(
          CardService.newDecoratedText()
            .setTopLabel('Error')
            .setText(message)
            .setStartIcon(CardService.newIconImage().setIcon(CardService.Icon.INVITE))
        )
    )
    .build();
}

// ============================================================
// Action stubs (Phase 2 implementations)
// ============================================================
function onLinkEntity(e) {
  // Phase 2: Show search card for customer/supplier linking
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText('Coming soon — Link to Customer/Supplier'))
    .build();
}

function onLinkRfq(e) {
  // Phase 2: Show search card for RFQ linking
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText('Coming soon — Link to RFQ'))
    .build();
}

function onCreateRfq(e) {
  // Phase 2: Create RFQ from email
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText('Coming soon — Create RFQ'))
    .build();
}
```

### 1.4 FastAPI: OIDC Token Verification Dependency

**New file: `includes/dashboard/routes/addon.py`**

```python
"""
Gmail Add-on API endpoints.

Authenticated via Google OIDC identity tokens (from ScriptApp.getIdentityToken()).
Domain-restricted to eagle-exports.com.au.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from pydantic import BaseModel

from config.settings import Config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/addon", tags=["addon"])

# Cache the Google token verifier transport
_google_request = google_requests.Request()

ALLOWED_DOMAIN = "eagle-exports.com.au"


# ── Auth dependency ────────────────────────────────────────────────────────

def verify_addon_token(request: Request) -> dict:
    """Verify Google OIDC identity token from Apps Script.

    Returns the decoded token payload (sub, email, hd, name, etc.).
    Raises 401/403 on failure.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    token = auth_header[7:]

    try:
        # verify_token checks signature, expiry, issuer (accounts.google.com)
        payload = id_token.verify_token(
            token,
            request=_google_request,
            # audience is the GCP project's OAuth client ID
            audience=Config.GOOGLE_ADDON_CLIENT_ID,
        )
    except ValueError as e:
        logger.warning("Addon token verification failed: %s", e)
        raise HTTPException(status_code=401, detail="Invalid identity token")

    # Domain restriction
    domain = payload.get("hd", "")
    if domain != ALLOWED_DOMAIN:
        logger.warning("Addon access denied for domain: %s (email: %s)", domain, payload.get("email"))
        raise HTTPException(status_code=403, detail=f"Domain {domain} not allowed")

    return payload


AddonUser = Annotated[dict, Depends(verify_addon_token)]


# ── Request/Response models ────────────────────────────────────────────────

class ContextRequest(BaseModel):
    gmail_message_id: str
    gmail_thread_id: str
    subject: str | None = None
    sender: str | None = None


class ContextResponse(BaseModel):
    customer: dict | None = None
    supplier: dict | None = None
    rfq: dict | None = None
    opportunity: dict | None = None
    email_tracked: bool = False


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/context", response_model=ContextResponse)
def get_email_context(body: ContextRequest, user: AddonUser):
    """Return linked entities for a Gmail message/thread.

    Looks up EmailTracking by gmail_message_id or gmail_thread_id,
    then resolves linked customer, supplier, RFQ, and opportunity.
    """
    from includes.dashboard.database import get_session
    from includes.dashboard.models import (
        Customer, EmailTracking, Opportunity, RFQ, Supplier,
    )

    session = get_session()
    try:
        # Try message ID first, then thread ID
        tracking = (
            session.query(EmailTracking)
            .filter(EmailTracking.gmail_message_id == body.gmail_message_id)
            .first()
        )
        if not tracking:
            tracking = (
                session.query(EmailTracking)
                .filter(EmailTracking.gmail_thread_id == body.gmail_thread_id)
                .order_by(EmailTracking.id.desc())
                .first()
            )

        if not tracking:
            return ContextResponse(email_tracked=False)

        result = ContextResponse(email_tracked=True)

        if tracking.customer_id:
            customer = session.query(Customer).get(tracking.customer_id)
            if customer:
                result.customer = {"id": str(customer.id), "name": customer.companyname}

        if tracking.supplier_id:
            supplier = session.query(Supplier).get(tracking.supplier_id)
            if supplier:
                result.supplier = {"id": str(supplier.id), "name": supplier.name}

        if tracking.rfq_token:
            rfq = session.query(RFQ).filter(RFQ.rfq_token == tracking.rfq_token).first()
            if rfq:
                result.rfq = {
                    "id": str(rfq.id),
                    "rfq_number": rfq.rfq_number,
                    "status": rfq.status or "draft",
                }

        if tracking.opportunity_id:
            opp = (
                session.query(Opportunity)
                .filter(Opportunity.netsuite_id == tracking.opportunity_id)
                .first()
            )
            if opp:
                result.opportunity = {
                    "id": str(opp.id),
                    "title": opp.title or f"OP{opp.netsuite_id}",
                }

        return result
    finally:
        session.close()
```

### 1.5 FastAPI: Configuration

Add to `config/settings.py`:

```python
# ==================== Gmail Add-on ====================
# OAuth Client ID from GCP project (used as 'audience' for ID token verification)
GOOGLE_ADDON_CLIENT_ID = os.getenv("GOOGLE_ADDON_CLIENT_ID", "")
```

Add to `.env.example`:

```bash
# Gmail Add-on — GCP OAuth Client ID (audience for OIDC token verification)
GOOGLE_ADDON_CLIENT_ID=
```

### 1.6 FastAPI: Register Router

In `main.py` or `includes/dashboard/routes/__init__.py`:

```python
from includes.dashboard.routes.addon import router as addon_router
app.include_router(addon_router)
```

### 1.7 Python Dependency

Add `google-auth` to `pyproject.toml` (likely already present for Gmail service account work):

```toml
"google-auth>=2.0",
```

### 1.8 Testing Phase 1

- [ ] `clasp create` in `addon/` directory, link to GCP project
- [ ] `clasp push` to upload Code.gs + appsscript.json
- [ ] Use test deployment (Extensions → Add-ons → Use test deployment)
- [ ] Open a Gmail message → sidebar should appear with "Eagle Procurement"
- [ ] Verify backend receives request with valid identity token
- [ ] Verify domain restriction works (non-eagle-exports user rejected)
- [ ] Verify context lookup returns linked entities for tracked emails
- [ ] `clasp deploy` for production version
- [ ] Admin Console → Install for target users

---

## Phase 2: Full Feature Implementation (FUTURE)

### 2.1 Link Email to Customer/Supplier

**Apps Script**: `onLinkEntity()` navigates to a search card:
- Text input for search query
- On submit → calls `GET /api/addon/search?type=entity&q=<query>`
- Displays results as selectable list
- On selection → calls `POST /api/addon/link-email` with `{gmail_message_id, gmail_thread_id, link_type, entity_id}`
- Backend reuses existing `api_link_email()` logic (match all thread messages, save domain)

### 2.2 Link Email to RFQ/Opportunity

**Apps Script**: `onLinkRfq()` navigates to a search card:
- Text input for RFQ number or keyword
- On submit → calls `GET /api/addon/search?type=rfq&q=<query>`
- Displays results as selectable list
- On selection → calls `POST /api/addon/link-email` with `{gmail_message_id, gmail_thread_id, link_type: "rfq", entity_id: rfq_token}`
- Backend updates `EmailTracking.rfq_token` for message + all thread messages

### 2.3 Create New RFQ from Email

**Apps Script**: `onCreateRfq()` calls:
- `POST /api/addon/create-rfq` with `{gmail_message_id, gmail_thread_id, subject, sender, body_text}`
- Backend creates RFQ record, links email tracking, returns `{rfq_number, rfq_url}`
- Card navigates to success state with link to RFQ in dashboard

### 2.4 Display Linked Entities (already done in Phase 1 context card)

---

## Phase 3: Polish & Rollout (FUTURE)

- [ ] Add "Open in Dashboard" buttons that link to `/rfqs/<id>`, `/emails?thread=<id>`
- [ ] Add supplier quote status to context card (if email triggered quote pipeline)
- [ ] Cache context lookups in Apps Script `CacheService` (5-minute TTL)
- [ ] Error handling: retry on 5xx, graceful timeout card
- [ ] Admin controls: add/remove users from add-on via Admin Console
- [ ] Logging/metrics: track addon usage via structured logs

---

## Key Technical Details

### OIDC Token Claims

`ScriptApp.getIdentityToken()` returns a JWT with:
```json
{
  "iss": "https://accounts.google.com",
  "azp": "<GCP_CLIENT_ID>",
  "aud": "<GCP_CLIENT_ID>",
  "sub": "1234567890",
  "hd": "eagle-exports.com.au",
  "email": "user@eagle-exports.com.au",
  "email_verified": true,
  "name": "User Name",
  "iat": 1690000000,
  "exp": 1690003600
}
```

- `aud` matches `GOOGLE_ADDON_CLIENT_ID` (your GCP project's OAuth client ID)
- `hd` is the Google Workspace domain — used for access control
- Token lifetime: ~1 hour, automatically refreshed by Apps Script runtime
- No refresh token management needed on either side

### Deployment Without Marketplace

For domain-internal add-ons:
1. Apps Script project linked to GCP project
2. Create deployment (Publish → Deploy as Google Workspace Add-on)
3. Admin Console → Apps → Google Workspace Marketplace → Internal apps → Install
4. Assign to specific organizational units or users
5. No Marketplace review needed for internal-only

### Security Considerations

- **Domain restriction**: Server validates `hd` claim — only `eagle-exports.com.au`
- **Audience validation**: Token `aud` must match expected client ID — prevents token reuse from other apps
- **No secrets in Apps Script**: Identity tokens are ephemeral, no API keys stored
- **HTTPS only**: All backend calls over TLS
- **Rate limiting**: Consider adding rate limiting to addon endpoints (future)

---

## Implementation Log (Updated 2026-07-27)

### ✅ Phase 1 — COMPLETE

**What was built:**
- `addon/` directory with full `clasp` workflow
- `addon/appsscript.json` — manifest with `openid`, `gmail.addons.execute`, `gmail.addons.current.message.*`, `script.external_request` scopes
- `addon/Code.gs` — `onHomepage()`, `onGmailMessageOpen()`, `buildContextCard()`, `buildErrorCard()`, `buildEditorFallbackCard()`, `buildNoActionsCard()`, `fetchBackend()` helper
- `includes/dashboard/routes/addon.py` — `POST /api/addon/context`, `POST /api/addon/link-email`, `GET /api/addon/search`, `POST /api/addon/match`
- `includes/gmail/matching.py` — new shared functions: `find_sender_match()`, `save_sender_domain()`
- `includes/tools/supplier_quote_pipeline.py` — deduplication guard added
- `main.py` — registered addon router
- `config/settings.py` — `GOOGLE_ADDON_CLIENT_ID` setting

**Deviations from original plan:**
1. **Domain corrected**: `eagle-exports.com.au` → `eagle-exports.com` (Google Workspace domain is `.com`, not `.com.au`)
2. **Logo URL**: Uses `EagleAgent.png` at the production URL (`agent.eaglexp.com.au/public/avatars/EagleAgent.png`), not a non-existent `eagle-icon.png` on `mooball.net`
3. **Backend URL**: `agent.eaglexp.com.au` instead of `eagle-agent.mooball.net`
4. **Added `gmail.addons.execute` scope**: Required for action buttons to modify the add-on card UI
5. **Added `urlFetchWhitelist`**: Required by Google Workspace add-on policy
6. **OIDC verification simplified**: No audience (`aud`) check — relies on `hd` domain claim only
7. **Add-on named "Eagle Agent"** instead of "Eagle Procurement"

**Clasp project:**
- Script ID: `1EVIKvVdsNS4qXq7UbODESpuKDk39q_q71i1Dhtm-vKEIumaLqOa43cnt`
- GCP project number: `308081329519`

### ✅ Phase 2 — COMPLETE (Link + Search flows)

**Link to Customer/Supplier flow (match-first):**
1. `onLinkEntity` → `POST /api/addon/match` (shared `find_sender_match()` from `matching.py`)
2. Match found → suggestion card with "Confirm Link" / "Search manually"
3. No match → type chooser (Customer/Supplier) → live search → select → `POST /api/addon/link-email`
4. Domain auto-saved to entity on link (via shared `save_sender_domain()`)

**Link to RFQ flow:**
1. `onLinkRfq` → search card (searches RFQ number, OP number, customer name)
2. Live search via `GET /api/addon/search?type=rfq` (ordered by `created_date DESC`)
3. Select → `POST /api/addon/link-email` with `link_type='rfq'`

**Button visibility rules:**
| Button | Visible when |
|---|---|
| Link to Customer/Supplier | No customer AND no supplier linked |
| Link to RFQ | No RFQ linked |
| Create RFQ | Customer IS linked |

**Shared code refactoring:**
- `find_sender_match()` in `matching.py` — used by both automated sync pipeline and add-on
- `save_sender_domain()` in `matching.py` — used by admin dashboard (`_save_email_domain()`) and add-on (`link-email` with `save_domain=true`)
- `_maybe_trigger_quote_pipeline()` in `addon.py` — triggers supplier quote pipeline on ALL received emails in thread after linking

**Pipeline integration:**
- `supplier_quote_pipeline` triggered when linking completes the RFQ+supplier pair on received emails
- Deduplication guard: `trigger_supplier_quote_pipeline` checks `supplier_pipeline_result` before processing
- Runs on ALL received emails in the thread (not just the first one)

### 🔜 Phase 2 remaining — Create RFQ from Email

**Not yet implemented:**
- `onCreateRfq` → create new RFQ from email, linked to customer
- Backend endpoint `POST /api/addon/create-rfq`

### 🔜 Phase 3 — Polish & Rollout

**Not yet implemented:**
- "Open in Dashboard" buttons
- Supplier quote status on context card
- Ad-hoc pipeline trigger button
- Cache context lookups in `CacheService`
- Admin Console install for target users
- Error: retry on 5xx, graceful timeout card

---

## File Changes Summary (Phase 1)

| File | Action | Description |
|------|--------|-------------|
| `addon/appsscript.json` | CREATE | Apps Script manifest (scopes, triggers, add-on config) |
| `addon/Code.gs` | CREATE | Main add-on logic (triggers, CardService UI, backend calls) |
| `addon/.clasp.json` | CREATE | clasp project config (script ID — created by `clasp create`) |
| `addon/.claspignore` | CREATE | Exclude README and .clasp.json from push |
| `addon/README.md` | CREATE | Setup instructions for clasp, GCP linking, Admin Console |
| `includes/dashboard/routes/addon.py` | CREATE | Addon API endpoints + OIDC auth dependency |
| `includes/dashboard/routes/__init__.py` | EDIT | Register addon router |
| `config/settings.py` | EDIT | Add `GOOGLE_ADDON_CLIENT_ID` |
| `.env.example` | EDIT | Add `GOOGLE_ADDON_CLIENT_ID` |
| `pyproject.toml` | CHECK | Ensure `google-auth` dependency present |
| `.gitignore` | EDIT | Add `addon/.clasp.json` if it contains sensitive script ID (optional) |

---

## Estimated Scope

- **Phase 1** (auth + context card): ~2-3 focused sessions
- **Phase 2** (linking + RFQ creation): ~2-3 sessions
- **Phase 3** (polish): ~1 session
