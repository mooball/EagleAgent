# Gmail Domain-Wide Delegation Setup Guide

## Overview

This guide walks you through enabling Gmail API access for the EagleAgent service account via domain-wide delegation in Google Workspace Admin Console.

**What is domain-wide delegation?**
- Allows the service account to impersonate any user in your Google Workspace domain
- No per-user OAuth flow needed
- Service account acts on behalf of staff members
- Used for batch operations like email automation

**What we're setting up:**
- Service account: `eagleagent@gen-lang-client-0058030584.iam.gserviceaccount.com`
- Domain: `eagle-exports.com`
- Scopes: Gmail (send, read, modify)

---

## Prerequisites

1. **Google Workspace Admin Account**
   - Must have access to Google Workspace Admin Console
   - Admin URL: https://admin.google.com
   - Contact: Your Google Workspace domain administrator

2. **Service Account Key**
   - Already in project: `service-account-key.json`
   - Client ID: Extract from the JSON file (needed below)

---

## Step 1: Get Service Account Client ID

Open `service-account-key.json` and find the `client_id` field:

```json
{
  "client_id": "104280193234398392141",
  "client_email": "eagleagent@gen-lang-client-0058030584.iam.gserviceaccount.com",
  ...
}
```

**Copy the `client_id` value.** You'll need it in Step 3.

---

## Step 2: Open Google Workspace Admin Console

1. Go to https://admin.google.com
2. Sign in with your Google Workspace admin account
3. From the home page, navigate to:
   - **Security** (left sidebar)
   - **API Controls**
   - **Domain-wide Delegation**

---

## Step 3: Add Gmail Scopes to Service Account

1. On the **Domain-wide Delegation** page, find the table of authorized apps
2. Look for an entry with your service account's **client ID** (from Step 1)
   - If not present, click **Add new** and enter the client ID
3. Click **Edit** next to the service account
4. In the **Scopes** field, enter the following scopes (comma or space-separated):

```
https://www.googleapis.com/auth/gmail.modify,https://www.googleapis.com/auth/gmail.readonly,https://www.googleapis.com/auth/gmail.send
```

**Explanation of each scope:**
- `gmail.modify` — Create drafts, manage labels, modify messages
- `gmail.readonly` — Read mailbox and history (for reply tracking)
- `gmail.send` — Send emails on behalf of users

5. Click **Save**

---

## Step 4: Wait for Propagation

Google Workspace typically propagates authorization changes within **5-10 minutes**.

⏳ **Recommended:** Wait at least 5 minutes before testing.

---

## Step 5: Verify Setup (Optional)

Once propagation is complete, run the test script:

```bash
# Install dependencies first (if not already installed)
uv sync

# Test with a real staff member email
uv run python -m scripts.test_gmail_auth tom@eagle-exports.com
```

**Expected output:**
```
✓ Service account key loaded
✓ Credentials created successfully
✓ Gmail API client built
✓ All tests passed! Gmail API is ready for use.
```

**If you get an error like "Permission denied" or "Domain not recognized":**
- Wait another 2-3 minutes (propagation can take up to 10 min)
- Verify the staff email is in the `@eagle-exports.com` domain
- Double-check the scopes were entered correctly (no typos)
- Try a different staff email to isolate the issue

---

## Troubleshooting

### Error: "Invalid OAuth Scope"
- **Cause**: Typo in scope URL or invalid scope
- **Fix**: Copy-paste the scopes exactly from Step 4 above

### Error: "Domain not found"
- **Cause**: Service account not associated with your domain
- **Fix**: Contact Google Workspace support to enable domain-wide delegation for your service account

### Error: "Insufficient permissions"
- **Cause**: User trying to impersonate doesn't have email access
- **Fix**: Ensure the staff email (`tom@eagle-exports.com`) has Gmail enabled

### Error: "Service account credentials not found"
- **Cause**: `service-account-key.json` is missing or in wrong location
- **Fix**: Verify the file exists in project root: `/Volumes/980PRO/tom_home_backup/src/EagleAgent/service-account-key.json`

---

## What's Next?

Once the test script passes:

1. **Database Setup** (Phase 1.4)
   - Create `email_tracking` and `mailbox_sync_cursor` tables
   - Alembic migration needed

2. **Draft Service** (Phase 2)
   - Implement `includes/gmail/draft_service.py`
   - Functions to create drafts with custom headers
   - Generate compose URLs for browser modal

3. **Mailbox Scanning** (Phase 3)
   - Implement incremental sync with Gmail History API
   - Detect sent emails and replies

---

## Reference: Google Workspace Admin Links

- **Admin Console**: https://admin.google.com
- **Domain-wide Delegation**: https://admin.google.com/ac/security/appsecurity/domainwide
- **Service Account Help**: https://support.google.com/a/answer/7378726

---

## Additional Resources

- [Gmail API Documentation](https://developers.google.com/gmail/api/guides)
- [Domain-wide Delegation Setup](https://developers.google.com/workspace/guides/create-credentials#domain-wide_delegation)
- [Google Workspace Security Best Practices](https://support.google.com/a/answer/6009563)

---

## Questions?

Refer to the Gmail Integration Plan at `.github/prompts/plan-gmailIntegration.prompt.md` for full architecture and Phase-by-Phase implementation details.
