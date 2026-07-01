# Plan: Bulk Email Suppliers from RFQ

**Status: ✅ Phases 1–4 complete, Phase 5 (Preview) remaining**

## Goal

Allow users to compose a single email template, select multiple shortlisted suppliers, and send (or save as drafts) personalized emails to all of them at once — from the RFQ suppliers view (`/rfqs/{rfq_id}/suppliers`).

## Current State (pre-implementation)

- **Single-supplier email flow** works well: each supplier row had "Edit" (in-app compose modal) and "Edit in Gmail" (one-click draft) buttons.
- The compose modal (`emailModal` in `base.html`) used a Jodit rich text editor with Send / Save Draft / Edit in Gmail actions.
- Email bodies were **fully pre-rendered per supplier** in `_rfq_email_suppliers.html`.

## Implemented (Phases 1–4)

### Phase 1: Editable Contact Info ✅

**`templates/partials/_rfq_email_suppliers.html`:**
- Inline edit icons (✏️ for edit, + for add) next to each supplier's email
- Click reveals inline `<input>` fields for email and contact name
- Save button sends `PATCH /api/rfqs/{rfq_id}/supplier-contact`
- HTMX refreshes panel after save
- Display shows `Contact Name (email@address.com)` or just `email@address.com`

**`includes/dashboard/routes/rfqs.py`:**
- `_build_rfq_supplier_email_data()` now extracts `contact_name` from contacts alongside `email`
- New endpoint `PATCH /api/rfqs/{rfq_id}/supplier-contact`:
  - Updates supplier contacts in `rfq_items.suppliers` JSONB
  - Upserts `Contact` table: finds ANY active contact for the supplier (by `supplier_id`), updates email/name, deactivates stale duplicates
  - Uses `flag_modified()` for reliable JSONB change detection
  - Adds history entry to RFQ

### Phase 2: Checkboxes + Selection ✅

**`templates/partials/_rfq_email_suppliers.html`:**
- Supplier list wrapped in Alpine scope using `Alpine.store('bulkEmail')` for shared state
- Checkbox per supplier row (disabled if no email, `@click.stop` prevents row expand)
- Header: "Select All / Deselect All" toggle, count indicator ("2 of 5 selected"), "Compose Email" button
- Supplier data JSON island (`<script id="bulk-supplier-data">`) for bulk compose

### Phase 3: Bulk Compose Modal ✅

**`templates/partials/_email_bulk_compose_modal.html`** (new):
- Modal with unified header/footer pattern matching single email modal
- Subject: `[OP1234]` prefix label + editable input field
- Jodit editor with `[[SALUTATION]]` and `[[ITEMS_TABLE]]` placeholder chips (yellow highlight, `[[ ]]` syntax avoids Jodit's `{{ }}` template conflict)
- Recipient list showing name, email, item count per supplier
- Buttons: "Cancel" (bordered), "Save All as Drafts" (grey), "Send All" (blue)

**`templates/base.html`:**
- `Alpine.data('bulkComposeModal')` registered via `alpine:init` event (fires before DOM scan)
- Component uses `isOpen` (not `open`) to avoid name collision with `open()` method
- Plain properties (not getters) for `okCount`/`failCount` — getters crash Alpine Proxy
- `_defaultTemplate()` wraps content in Gmail-consistent styling (Arial 14px, 12px margins)
- `_renderEmailFor()` builds per-supplier email: replaces `[[SALUTATION]]` → "Hi John,", `[[ITEMS_TABLE]]` → HTML table built from line_items

**`templates/partials/rfq_detail.html`:**
- Includes bulk compose modal partial
- Passes `window.__bulkUser` (email, name) for signature rendering

### Phase 4: Bulk Send/Draft ✅

- Sequential loop with 500ms delay between API calls
- Progress spinner: "Sending 2 of 3..."
- Result summary: "All 3 sent successfully" or "2 sent, 1 failed: Acme Corp — error msg"
- Per-draft "Open in Gmail" links
- HTMX refresh of suppliers panel on completion (green dots)
- Reuses existing `send-email-draft` and `send-email-direct` endpoints

### Unified Modal Design ✅

Both single and bulk compose modals now share the same layout:
- **Header**: title + expand icon + X close, `border-b` divider
- **Body**: `p-5 space-y-4`, fields with consistent styling
- **Footer**: `bg-gray-50`, `border-t` divider, matching button styles
- **Width**: `max-w-3xl`
- **Backdrop**: `bg-black/40` with click-to-close
- **Escape key**: closes modal
- **Subject**: `[OP1234]` prefix label before editable input (both modals)
- **Buttons**: "Compose" / "in Gmail" on supplier rows (both grey, `bg-slate-500`)
- Tracking ID concatenated at send time, not embedded in editable field

## Remaining: Phase 5 — Preview Feature

The "Preview" accordion at the bottom of the bulk modal lets the user see the final rendered email for a specific supplier before sending.

- Click a recipient in the list to preview their rendered email
- No API call needed — client-side rendering from template + supplier data

## Technical Notes (Alpine.js Lessons Learned)

See `/memories/alpine-js-patterns.md` for full details:
1. Register `Alpine.data()` in `base.html` via `alpine:init`, NOT in HTMX partials
2. Never use ES6 getters in `x-data` objects — they crash Alpine's Proxy
3. Never use same name for boolean property and method (`open` → `isOpen`)
4. Use `Alpine.store()` for shared state, not `$parent` (unreliable with `Alpine.data()`)
5. Don't use `x-trap` without Alpine Focus plugin
6. Jodit strips `contenteditable="false"` spans and `{{ }}` template syntax — use `<strong>` with `[[ ]]` instead
7. Jinja2 processes `{{ }}` inside `<script>` tags — avoid or use `{% raw %}`
8. `tojson` inside double-quoted HTML attributes breaks parsing — use single quotes for `x-data`

## File Changes (Actual)

| File | Change |
|---|---|
| `templates/partials/_rfq_email_suppliers.html` | Inline contact editing, checkboxes, selection header, supplier data JSON |
| `templates/partials/_email_bulk_compose_modal.html` | **New** — bulk compose modal (HTML only, component in base.html) |
| `templates/partials/_email_compose_modal.html` | Redesigned to match bulk modal layout; subject prefix; unified buttons |
| `templates/partials/rfq_detail.html` | Includes bulk modal, passes `window.__bulkUser` |
| `templates/base.html` | `Alpine.data('bulkComposeModal')` component; tracking prefix for single modal |
| `includes/dashboard/routes/rfqs.py` | `PATCH` endpoint, `contact_name` extraction, `flag_modified` |

## Decisions

- **Subject line:** `[OP1234]` prefix label + editable field; concatenated at send time. Both modals follow this pattern.
- **CC support:** Not needed. Confirmed out of scope.
- **Rate limiting:** 500ms client-side delay between sends. Confirmed.
- **Template persistence:** Deferred to future iteration. V1 uses default template with per-session edits.
- **Placeholder syntax:** `[[SALUTATION]]` / `[[ITEMS_TABLE]]` (avoids Jodit `{{ }}` template conflict).
- **Single email buttons:** Renamed to "Compose" / "in Gmail", both grey (`bg-slate-500`).

## Out of Scope

- Reply tracking (already handled by Gmail sync).
- Template versioning or saved templates.
- Attachment support for bulk emails.
- CC/BCC support.
