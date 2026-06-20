# Plan: Add `sender_email` to EmailTracking

**Status:** Scoped, not started  
**Created:** 2026-06-20  
**Branch:** gmail-integration

## Objective

Add an explicit `sender_email` column to the `EmailTracking` table so the sender
is always stored directly, eliminating direction-dependent interpretation of
`user_email` / `recipient_email`. After this change:

| Field | Always means |
|-------|-------------|
| `user_email` | Eagle mailbox owner (Gmail impersonation target) |
| `sender_email` | Actual sender of the email |
| `recipient_email` | Actual recipient(s) of the email |

## Background

Currently the sender is derived from `direction` + `user_email`/`recipient_email`:

- **Sent/Draft**: sender = `user_email` (staff), recipient = `recipient_email` (external)
- **Received**: sender = `recipient_email` (external), recipient = `user_email` (staff)

This works but requires every template, query, and display to check `direction`.
Making the sender explicit simplifies code and eliminates edge-case bugs.

## Files to Change

### Phase 1: Model + Migration

| File | Change |
|------|--------|
| `includes/dashboard/models.py` | Add `sender_email = Column(String(255), nullable=True)` to `EmailTracking` |
| `alembic/versions/` | New migration: add `sender_email` column + data backfill |

**Migration SQL:**
```sql
ALTER TABLE email_tracking ADD COLUMN sender_email VARCHAR(255);

-- Case 1: user_email is the Eagle mailbox owner (normal case)
UPDATE email_tracking
SET sender_email = user_email
WHERE user_email LIKE '%@eagle-exports.com';

-- Case 2: user_email is external (legacy data) — swap
UPDATE email_tracking
SET sender_email = user_email,
    user_email = recipient_email
WHERE user_email NOT LIKE '%@eagle-exports.com'
  AND recipient_email LIKE '%@eagle-exports.com';

-- Case 3: both are external (edge case, rare)
UPDATE email_tracking
SET sender_email = user_email
WHERE sender_email IS NULL;
```

### Phase 2: Sync Script

| File | Change |
|------|--------|
| `scripts/sync_gmail_mailboxes.py` | Set `sender_email` in all `EmailTracking()` constructor calls and Tier 1 update path |

**`process_message()` changes:**
- Tier 1 (existing thread update): `existing_thread.sender_email = ...`
- Tier 2/3 (new tracking): `sender_email=from_addr if direction == 'received' else user_email`
- Sent emails: `sender_email = user_email` (the Eagle staff member who sent it)

### Phase 3: Draft Service

| File | Change |
|------|--------|
| `includes/gmail/draft_service.py` | `_save_draft_to_tracking()` — set `sender_email = user_email` (the drafter) |

### Phase 4: Templates — Simplify direction-dependent display

| File | Lines | Current | New |
|------|-------|---------|-----|
| `templates/partials/_email_rows.html` | 2-65 | `if received: from=recipient else: from=user_email` | `from = sender_email, to = recipient_email` |
| `templates/partials/admin_emails.html` | 38-49 | Same direction-dependent | Same simplification |
| `templates/partials/rfq_detail.html` | 411-491 | `_commsMsgs` + display rows both check direction | `from = sender_email` directly |

### Phase 5: Routes — Simplify external email detection

| File | Change |
|------|--------|
| `includes/dashboard/routes/admin.py` | `_save_email_domain()` — use `sender_email` instead of direction-dependent logic |

### Phase 6: Backfill Script (new)

| File | Purpose |
|------|---------|
| `scripts/backfill_sender_email.py` | One-off script to populate `sender_email` from existing data using the same migration logic above |

## NOT Changing

These models use `user_email` with the correct semantics (mailbox owner / Eagle user):

- `MailboxScanConfig.user_email` — mailbox to scan
- `MailboxSyncCursor.user_email` — sync progress per mailbox
- `RFQThread.user_email` — chat thread owner
- `get_gmail_client(user_email)` — Gmail API impersonation
- `create_draft_email(user_email, ...)` — staff drafter
- `fetch_message_content(service, ...)` — uses service (already authenticated), `user_email` only in `EmailTracking` save

## Risks

- **Index strategy**: `user_email` is heavily indexed. Consider adding index on `sender_email`.
- **Legacy data edge cases**: Case 3 (both emails external) is unlikely but must handle gracefully.
- **Display regression**: Must verify all template displays show correct sender/recipient after change.
- **Admin email filter**: The filter `user_email = :uf OR recipient_email = :uf` should become `sender_email = :uf OR recipient_email = :uf OR user_email = :uf`.

## Verification

After migration:
1. Check `SELECT COUNT(*) FROM email_tracking WHERE sender_email IS NULL` — should be 0
2. Check `SELECT * FROM email_tracking WHERE user_email NOT LIKE '%@eagle-exports.com'` — should be 0 or only edge cases
3. Test RFQ communications tab — sender/recipient display correct
4. Test admin email logs — from/to display correct, filter works
5. Test creating and sending a draft — `sender_email` populated correctly
6. Test Gmail sync processes new messages — `sender_email` set correctly
