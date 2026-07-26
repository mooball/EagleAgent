# Eagle Agent — Gmail Workspace Add-on

Private add-on for eagle-exports.com.au domain users.
Provides contextual email information and actions in the Gmail sidebar.

## Architecture

- **Apps Script** (this directory) — Gmail add-on UI using CardService
- **FastAPI backend** — `includes/dashboard/routes/addon.py` — JSON API endpoints
- **Auth** — OIDC identity tokens (`ScriptApp.getIdentityToken()`), verified
  server-side via `google-auth` library, domain-restricted to `eagle-exports.com.au`

## Setup

### One-time (new developer)

```bash
npm install -g @google/clasp
clasp login
```

### Link to existing Apps Script project

```bash
cd addon/
clasp clone <SCRIPT_ID>
```

Or create a new one:

```bash
cd addon/
clasp create --title "Eagle Agent" --type standalone
# Then link to GCP project 308081329519 in the Apps Script editor:
# Settings → GCP Project → Change project → paste 308081329519
```

### Daily workflow

```bash
cd addon/

# Push local changes
clasp push

# Create versioned deployment
clasp deploy --description "v1.0"

# Open in browser (for logs/testing)
clasp open
```

### Test deployment

1. `clasp push`
2. In Apps Script editor: Extensions → Google Workspace Add-ons → Use test deployment
3. Open Gmail → sidebar should appear

### Production deployment

1. `clasp deploy --description "v1.x"`
2. Admin Console → Apps → Google Workspace Marketplace apps → Internal apps → Install
3. Select deployment version → Assign to target users/OUs
