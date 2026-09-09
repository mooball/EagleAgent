# Plan: RFQ Agent-Working Lock

> Status: **PROPOSED — awaiting review** (2026-09-09)
> Related: [plan-rfqBulkOperations.prompt.md](plan-rfqBulkOperations.prompt.md),
> [plan-grouped-supplier-search.prompt.md](plan-grouped-supplier-search.prompt.md)
> Scope: lock the RFQ items tab read-only while the RFQ-creation pipeline is
> still adding items, with a visible "agent is working" banner.

---

## 1. Background

The Create-RFQ pipeline (`includes/tools/rfq_creation_pipeline.py`) runs
asynchronously in a daemon thread. Its stages:

1. Guard checks + atomic claim (writes `{"status": "processing", …}` into
   `email_tracking.rfq_creation_result`)
2. Create RFQ & link email thread (+ auto-create NetSuite opportunity)
3. LLM item extraction → `_add_items_sync()` → title/notes `_update_rfq_sync()`
4. `_save_rfq_creation_result()` (terminal for complete/partial/error)

**The problem:** the pipeline never notifies the dashboard. While it is still
adding items (stage 3 can take 30s–2min), the user sees a half-populated
`/items` listing, starts editing, and their writes race the pipeline's
`_add_items_sync()` — producing duplicate items and confused state. The email
panel shows "processing", but the RFQ page shows nothing.

**Why server-side, not just UI:** a banner alone doesn't stop edits arriving
from another tab, stale pages, or direct requests. The lock must be enforced
where writes land — the dashboard's item-mutation endpoints.

**Why the pipeline is unaffected:** it calls `_create_rfq_sync()` /
`_add_items_sync()` / `_update_rfq_sync()` directly — it never goes through the
HTTP endpoints we guard.

---

## 2. Goals

1. **Visible banner** — items tab shows "EagleAgent is still adding items…"
   with the current stage, plus a manual **Refresh** button.
2. **Server-enforced read-only** — item mutations return 409 with a clear
   message while the pipeline is active.
3. **Self-clearing** — banner disappears and items appear automatically,
   without a manual refresh.
4. **Crash-safe** — a stale flag can never lock an RFQ permanently.
5. **Zero impact on pipeline writes** — the agent's own item additions always
   succeed.

---

## 3. Design

### 3.1 `rfqs.pipeline_activity` JSONB (new migration)

Flag lives **on the RFQ** (not on `email_tracking`, which only has the
"processing" marker at claim time and is harder to join back from the items
tab):

```json
{
  "kind": "rfq_creation",
  "started_at": "2026-09-09T01:23:45Z",
  "heartbeat_at": "2026-09-09T01:24:10Z",
  "step": "adding_items"
}
```

- `step` ∈ `extracting_items` | `adding_items` | `updating_details` — set at
  each stage boundary so the banner shows progress.
- **Set** at stage 3 start (RFQ exists by then in both paths: created in
  stage 2, or pre-created and passed as `rfq_number` by the addon).
- **Heartbeat** = `_now_iso()` at every stage boundary (cheap; no timer
  thread needed — the pipeline is short-lived).
- **Cleared** in *both* terminal paths:
  - `_save_rfq_creation_result()` (complete / partial / error)
  - `_save_error()` (early failures after the flag exists)
  Both look up the RFQ by `rfq_token`/`rfq_number` and set the column NULL.
- **Staleness rule (read side):** a flag whose `heartbeat_at` is older than
  **30 min** is treated as absent. A crashed daemon thread therefore
  self-heals the lock.

### 3.2 Read-side helper

`_rfq_pipeline_active(rfq) -> dict | None` in `routes/rfqs.py`:

```python
def _rfq_pipeline_active(rfq) -> dict | None:
    act = rfq.pipeline_activity
    if not act:
        return None
    try:
        age = now - iso(act["heartbeat_at"])
    except Exception:
        return act  # malformed → treat as active until staleness clears it? see §8
    return act if age < PIPELINE_STALE_AFTER else None
```

`PIPELINE_STALE_AFTER = timedelta(minutes=30)` module constant. (Existing
`system_settings.py` pattern if we prefer a configurable threshold.)

### 3.3 Server-side guards (409)

A tiny helper near the other RFQ fetch helpers:

```python
def _rfq_lock_response(rfq) -> Response | None:
    act = _rfq_pipeline_active(rfq)
    if not act:
        return None
    return Response(
        content="EagleAgent is still adding items to this RFQ — please wait "
                "a moment and refresh.",
        status_code=409,
    )
```

Wired into the **item mutation** endpoints (all already fetch the RFQ):

| Endpoint | Line |
|---|---|
| `POST /partial/rfqs/{rfq_id}/add-item` | 2249 |
| `POST /partial/rfqs/{rfq_id}/update-item` | 2106 |
| `DELETE /partial/rfqs/{rfq_id}/delete-item/{line}` | 2145 |
| `POST /partial/rfqs/{rfq_id}/bulk-update-items` | 2209 |
| `POST /partial/rfqs/{rfq_id}/clear-suppliers` | 2315 |

409 responses render through the existing HTMX error path (toast:
`window.eaToast(msg, false)`), so the user gets a visible explanation rather
than a silent no-op.

### 3.4 UI banner + read-only rendering

`_rfq_detail_context` passes `pipeline_active` into the items tab.

- **Banner** (top of `_rfq_items_table.html` or the `#rfq-items-container`
  wrapper in `rfq_detail.html`):

  > 🤖 EagleAgent is still adding items from the email — this view is
  > read-only until it finishes. Step: **adding items**.
  > <button>Refresh</button>

- **Read-only variant:** when `pipeline_active`, render the items table
  without the add-item form and without inline-edit inputs / delete buttons
  (plain values instead). A single `{% if pipeline_active %}` guard around
  the editable blocks — the server 409s remain the real protection.
- **Refresh button** re-fetches the items container (existing
  `#main-content` innerHTML swap pattern).

### 3.5 Auto-refresh (self-clearing)

Lightweight endpoint:

```
GET /partial/rfqs/{rfq_id}/pipeline-status
```

- Returns the banner HTML while active (with updated step label).
- When finished, returns empty body + `HX-Trigger: pipelineDone`; a listener
  on the items container re-fetches `#rfq-items-container`, revealing the
  full item list. No manual action needed.
- The banner wrapper polls with `hx-trigger="every 10s"` while rendered —
  the poll element disappears with the banner, so no polling overhead when
  idle.
- Dashboard `dashboard_refresh` events (already pushed after each batch of
  agent work) also re-render the tab, so classic Chainlit users see the
  banner appear without waiting for the poll.

**Note on pushing completion from the pipeline:** `notify_dashboard()` needs a
Chainlit context, but the pipeline runs in a daemon thread where
`try_get_chat_context()` is unavailable. Polling is the reliable transport
for both the classic (iframe) and beta (SSE) UIs; an optional
cross-thread-memory push (see `docs/CROSS_THREAD_MEMORY.md`) is noted in §8.

---

## 4. Behaviour matrix

| State | Items tab | Mutations |
|---|---|---|
| No pipeline running | Normal | Allowed |
| Pipeline active (fresh flag) | Banner + read-only | 409 + toast |
| Pipeline finished | Banner gone, items listed | Allowed |
| Flag stale > 30 min (crash) | Normal | Allowed (self-healed) |
| RFQ not yet created (stage 2) | N/A — page is the email panel, which already shows "processing" | — |

---

## 5. Edge cases

- **Crash mid-stage 3** → flag stays; 30-min staleness clears it. Banner
  keeps polling; once stale, next poll returns "done".
- **Extraction fails** → `_save_rfq_creation_result(status=partial)` clears
  the flag; empty RFQ unlocks for manual entry.
- **Addon path** (pre-created RFQ, `rfq_number` passed) → flag set at stage 3
  start, same as pipeline-created path.
- **Concurrent triggers** → existing atomic claim on `email_tracking`
  prevents double-processing; only the winning run touches the RFQ flag.
- **Item edit in a second tab** → 409 regardless of which tab sent it; the
  first tab's poll shows the banner within 10s.
- **Refresh spam** → `pipeline-status` is a tiny partial, cheap to poll.
- **Bulk operations / supplier ops** (swap-supplier, merge-suppliers, …) →
  see §8, intentionally out of initial scope.
- **`POST /api/rfq/{rfq_number}/extract-items`** (agent-initiated
  re-extraction) → separate mechanism, out of scope; noted in §8.

---

## 6. Testing

New `tests/tools/test_rfq_pipeline_lock.py` (mock `llm_call_with_retry` +
`_add_items_sync` — must NOT hit live NetSuite; same discipline as
`test_opportunity_sync.py`):

- flag set on stage-3 start (both pipeline-created and pre-created paths)
- flag cleared on complete, partial, and error outcomes
- stale flag treated as inactive by `_rfq_pipeline_active`
- 409 returned by each guarded endpoint while active; normal behaviour when
  cleared
- pipeline completes successfully *with* the lock active (its direct
  `_add_items_sync()` writes are not blocked)
- banner rendered when `pipeline_active`; read-only variant hides add-item
  form / edit inputs / delete buttons
- `pipeline-status` returns banner while active, done-trigger when cleared

Existing suite must stay green (1412 passed / 2 skipped baseline).

---

## 7. Files touched

| File | Change |
|---|---|
| `alembic/versions/<new>_add_pipeline_activity_to_rfqs.py` | **new** — `rfqs.pipeline_activity` JSONB |
| `includes/dashboard/models.py` | `RFQ.pipeline_activity = Column(JSONB)` |
| `includes/tools/rfq_creation_pipeline.py` | set flag at stage 3; heartbeat at stage boundaries; clear in `_save_rfq_creation_result()` + `_save_error()` |
| `includes/dashboard/routes/rfqs.py` | `_rfq_pipeline_active()` + `_rfq_lock_response()`; guards on 5 endpoints; `pipeline-status` endpoint; `pipeline_active` in `_rfq_detail_context` |
| `templates/partials/_rfq_items_table.html` | banner, read-only variant, refresh button |
| `templates/partials/rfq_detail.html` | poll wiring / `pipelineDone` listener on `#rfq-items-container` |
| `tests/tools/test_rfq_pipeline_lock.py` | **new** |

---

## 8. Open questions

1. **Scope of read-only** — items-only (add/update/delete/bulk/clear) or
   also supplier operations (swap/merge/drop/shortlist/copy)? Recommend
   items-only first; suppliers can be extended later.
2. **Malformed flag handling** — treat malformed `pipeline_activity` as
   active (lock) or absent (unlock)? Recommend absent + log warning, to
   avoid a stuck lock.
3. **Staleness threshold** — 30 min reasonable? Extraction is usually <2 min.
4. **Guard `/api/rfq/{rfq_number}/extract-items`** (agent-initiated
   re-extraction) with the same 409? It is a different async mechanism.
5. **Cross-thread completion push** — worth adding a
   cross-thread-memory notification so Chainlit users get an instant
   `dashboard_refresh` instead of waiting ≤10s? (Poll is the fallback
   either way.)
6. **Lock whole RFQ or items tab only** — title/notes/quote fields can also
   race stage 3's `_update_rfq_sync()`. Recommend keeping the visible lock
   scoped to items, and relying on the 409s; whole-RFQ lock is a follow-up
   if races appear elsewhere.

---

## 9. Implementation order

1. Migration + model (`pipeline_activity` JSONB).
2. Pipeline set/heartbeat/clear + unit tests (flag lifecycle).
3. Route helper + 409 guards on the 5 endpoints + tests.
4. `pipeline-status` endpoint + poll wiring tests.
5. Template banner + read-only variant + refresh button; JS syntax check
   (`node --check` on extracted inline scripts).
6. Full test suite, then a manual end-to-end run with a real email so the
   user can watch the lock engage and self-clear.
