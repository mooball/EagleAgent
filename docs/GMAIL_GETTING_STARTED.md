# Gmail Integration - Getting Started Guide

## ✅ What We've Set Up

### 1. **Dependencies Added**
- `google-api-python-client>=2.100.0` — Gmail API client library
- `google-auth` — Already included via google-genai

### 2. **Gmail Client Module** (`includes/gmail/__init__.py`)
Provides:
- `get_service_account_info()` — Load service account credentials
- `get_credentials(user_email)` — Create impersonation credentials for a staff user
- `get_gmail_client(user_email)` — Get Gmail API client
- `test_connection(user_email)` — Verify Gmail API access

### 3. **Test Script** (`scripts/test_gmail_auth.py`)
- Tests service account key readability
- Tests credential creation with domain-wide delegation
- Tests Gmail API connectivity
- Tests draft creation/deletion
- Provides detailed troubleshooting on failure

### 4. **Setup Documentation** (`docs/GMAIL_SETUP.md`)
- Step-by-step Google Workspace Admin Console configuration
- Screenshots and exact steps for enabling Gmail scopes
- Troubleshooting guide

---

## 🔧 What You Need to Do (Google Workspace Admin)

### Step 1: Get Service Account Client ID
```json
// From service-account-key.json
"client_id": "104280193234398392141"
```

### Step 2: Open Google Workspace Admin Console
- URL: https://admin.google.com
- Navigate: Security → API Controls → Domain-wide Delegation

### Step 3: Add Gmail Scopes
Find the service account (client ID: `104280193234398392141`) and add these scopes:
```
https://www.googleapis.com/auth/gmail.modify,https://www.googleapis.com/auth/gmail.readonly,https://www.googleapis.com/auth/gmail.send
```

### Step 4: Wait 5-10 Minutes
Google needs time to propagate the changes.

### Step 5: Test
```bash
uv run python -m scripts.test_gmail_auth tom@eagle-exports.com
```

Expected output:
```
✓ Service account key loaded
✓ Credentials created successfully
✓ Gmail API client built
✓ Test draft created: <draft_id>
✓ Test draft deleted successfully
✓ All tests passed! Gmail API is ready for use.
```

---

## 📋 Implementation Checklist

**Phase 1: Authentication & Connectivity**
- [x] Install dependencies
- [x] Create Gmail client wrapper
- [x] Create test script
- [x] Document setup process
- [ ] **TODO: Configure scopes in Google Workspace Admin** ← YOU ARE HERE
- [ ] Run test_gmail_auth.py and verify success

**Phase 2: Draft Creation & Modal UI** (Next)
- [ ] Create Alembic migration for email_tracking tables
- [ ] Create draft_service.py with draft creation logic
- [ ] Implement compose URL generation
- [ ] Add RFQ UI modal component
- [ ] Integrate with ProcurementAgent

**Phase 3: Mailbox Scanning** (After Phase 2)
- [ ] Implement incremental sync job
- [ ] Create mailbox_sync_cursor logic
- [ ] Implement reply detection

**Phase 4+**: See plan in `.github/prompts/plan-gmailIntegration.prompt.md`

---

## 🧪 Quick Test (After Configuring Scopes)

```bash
# Pick any staff email from your organization
uv run python -m scripts.test_gmail_auth tom@eagle-exports.com

# Or use another staff member
uv run python -m scripts.test_gmail_auth harry@eagle-exports.com
```

The script will:
1. Load service account credentials ✓
2. Create delegation credentials for the staff user ✓
3. Connect to Gmail API ✓
4. Fetch user profile and drafts ✓
5. Create a test draft ✓
6. Delete the test draft ✓

---

## 📝 Code Usage Examples

### Get Gmail Client for a Staff User
```python
from includes.gmail import get_gmail_client

service = get_gmail_client("tom@eagle-exports.com")
profile = service.users().getProfile(userId="me").execute()
print(f"Connected to: {profile['emailAddress']}")
```

### Test Connection
```python
from includes.gmail import test_connection

result = test_connection("tom@eagle-exports.com")
if result["status"] == "ok":
    print("Gmail API is ready!")
    print(f"Total messages: {result['details']['messages_total']}")
else:
    print(f"Error: {result['message']}")
```

### Create a Draft Message
```python
import base64
from includes.gmail import get_gmail_client

service = get_gmail_client("tom@eagle-exports.com")

message = {
    "raw": base64.urlsafe_b64encode(
        b"From: tom@eagle-exports.com\n"
        b"To: supplier@example.com\n"
        b"Subject: [RFQ-12345] Quote Request\n\n"
        b"Dear Supplier,\n\nCould you provide a quote...\n"
    ).decode()
}

draft = service.users().drafts().create(userId="me", body={"message": message}).execute()
print(f"Draft created: {draft['id']}")
```

---

## 🔗 Documentation Links

- **Gmail Integration Plan**: `.github/prompts/plan-gmailIntegration.prompt.md`
- **Setup Guide**: `docs/GMAIL_SETUP.md`
- **Gmail API Docs**: https://developers.google.com/gmail/api/guides
- **Domain-Wide Delegation**: https://developers.google.com/workspace/guides/create-credentials#domain-wide_delegation

---

## ⚠️ Troubleshooting

**Issue**: Test script says "Permission denied"
- **Solution**: Scopes not yet authorized in Google Workspace Admin, or propagation not complete. Wait 5-10 minutes and retry.

**Issue**: Staff email email not found
- **Solution**: Ensure email is in @eagle-exports.com domain and has Gmail enabled.

**Issue**: Service account key not found
- **Solution**: Verify `service-account-key.json` exists in project root.

**Issue**: "Invalid OAuth Scope"
- **Solution**: Typo in scope URL. Copy-paste from docs/GMAIL_SETUP.md.

For more troubleshooting, see `docs/GMAIL_SETUP.md` or the test script output.

---

## 🚀 Next Steps

1. **Immediately**: Configure Gmail scopes in Google Workspace Admin (see docs/GMAIL_SETUP.md)
2. **After scopes propagate**: Run `uv run python -m scripts.test_gmail_auth`
3. **Once tests pass**: Start Phase 2 (database + draft service)

---

## Questions?

- Read `docs/GMAIL_SETUP.md` for Google Workspace Admin setup
- Read `.github/prompts/plan-gmailIntegration.prompt.md` for architecture & implementation phases
- Check test script output for specific error messages
