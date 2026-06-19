# Plan: Gmail Attachment & Inline Image Handling

**Status:** Not started  
**Created:** 2026-06-19  
**Branch:** gmail-integration

## Objective

Improve how EagleAgent handles Gmail attachments and inline images in the email
communications log. Currently we only store attachment metadata (filename, size,
mime type) and show a paperclip emoji placeholder. Inline images are discarded
during HTML→markdown conversion (`ignore_images = True`).

## Background

- Emails are stored as **markdown** in `email_tracking.body_markdown`
- The email preview renders markdown → HTML client-side via `marked.parse()`
- Attachment metadata is stored in `email_tracking.attachments_json`
- Gmail API has no public attachment URLs — must use `users.messages.attachments.get`

## Scope

### Phase 1: Proxy endpoint for on-the-fly attachment serving

**New endpoint:** `GET /api/gmail/attachments/{message_id}/{attachment_id}`

- Fetches raw bytes from Gmail API using the stored `gmail_attachment_id`
- Returns binary response with correct `Content-Type`
- Lightweight in-memory cache (TTL ~1 hour) to avoid repeated Gmail API calls
- Requires `gmail_message_id` (not just attachment_id) to look up the correct mailbox user

**Files:**
- `includes/dashboard/routes/api.py` — new route or new file
- `includes/gmail/__init__.py` — reuse `get_gmail_client()` for auth

### Phase 2: Inline image extraction & cid: rewriting

**Modify** `fetch_message_content()` in `scripts/sync_gmail_mailboxes.py`:

1. During `_walk_parts()`, build a `contentId → attachmentId` map:
   - Check for `Content-ID` header on each part
   - If present, the part is an inline image (not a "real" attachment)
   - Store mapping: `cid_map[content_id] = attachment_id`

2. Before HTML→markdown conversion, rewrite `cid:` URLs:
   ```python
   for cid, att_id in cid_map.items():
       proxy_url = f"/api/gmail/attachments/{message_id}/{att_id}"
       body_html = body_html.replace(f"cid:{cid}", proxy_url)
   ```

3. Change `html2text` config:
   ```python
   h.ignore_images = False  # was True — now images become ![alt](url) in markdown
   ```

4. Store `cid_map` alongside the email (could be part of `attachments_json` or
   a new `inline_images_json` column):
   ```json
   [
     {"content_id": "xyz", "attachment_id": "att123", "mime_type": "image/png"}
   ]
   ```

### Phase 3: Filter decorative/footer images from attachment list

In `_walk_parts()`, classify each part:

| Type | Criteria | Show in attachments? | Store for rendering? |
|------|----------|---------------------|---------------------|
| **Real attachment** | Has `filename`, no `Content-ID` header | ✅ Yes | — |
| **Inline image** | Has `Content-ID` header | ❌ No | ✅ Yes (in cid_map) |
| **Footer image** | Has `Content-ID` + size < 50KB + filename matches `logo\|sig\|icon\|image00` | ❌ No | ✅ Yes |

Footer images are treated like inline images (stored for rendering, hidden from
attachment list).

### Phase 4: Client-side rendering

**Already works** — `marked.parse()` renders `![alt](/api/gmail/attachments/...)`
as `<img>` tags that hit the proxy endpoint. No client-side changes needed.

**Attachment display** — update `attachHtml` in `rfq_detail.html` line 438 to
show real attachments only (inline/footer images already filtered server-side).

## Technical Notes

### Auth for proxy endpoint

The proxy endpoint needs to know which Gmail mailbox to query. The
`gmail_message_id` is globally unique within a Gmail account, so we can look up
the `email_tracking` record to find `user_email`, then use that for
`get_gmail_client()`.

### Cache strategy

```python
_cache: dict[str, tuple[float, bytes]] = {}  # key → (expiry, data)
CACHE_TTL = 3600  # 1 hour
```

Cache key: `{message_id}:{attachment_id}`

### Decorative image filter

```python
_DECORATIVE_PATTERNS = re.compile(
    r'(logo|sig|icon|header|footer|banner|image00\d|~$)',
    re.IGNORECASE
)

def _is_decorative(part: dict) -> bool:
    """True if this inline image is likely decorative (logo, sig, etc)."""
    filename = (part.get("filename") or "").lower()
    size = part.get("body", {}).get("size", 0)
    return bool(
        _DECORATIVE_PATTERNS.search(filename) or
        (size > 0 and size < 30000 and not filename.endswith(('.pdf', '.doc', '.xls')))
    )
```

### Backfill

Existing emails won't have cid_map or rewritten img URLs. Options:
1. Re-sync affected threads (re-fetch `format=full`)
2. On-the-fly: when rendering an email body, if it contains `cid:` references,
   fetch the full message and rewrite on demand
3. Accept that old emails won't have inline images (low priority)

## Dependencies

- None — all infrastructure exists (Gmail API client, markdown pipeline,
  FastAPI routes)

## Files to modify

| File | Change |
|------|--------|
| `scripts/sync_gmail_mailboxes.py` | `_walk_parts`: cid_map, filter decorative images; rewrite cid: before markdown; set `ignore_images = False` |
| `includes/dashboard/routes/api.py` | New `GET /api/gmail/attachments/{msg_id}/{att_id}` endpoint |
| `templates/partials/rfq_detail.html` | Update `attachHtml` to only show real attachments |
| `includes/dashboard/models.py` | Possibly add `inline_images_json` column (or reuse `attachments_json` structure) |

## Risks

- **Gmail API quota**: Each attachment view hits the API. Mitigated by in-memory cache.
- **Large attachments**: Streaming large files through the proxy could be slow.
  Mitigated by only enabling for images initially; PDFs/docs can trigger a
  download instead of inline preview.
- **Auth for multi-user**: Must ensure the proxy endpoint only serves attachments
  the requesting user has access to (same Gmail domain).
