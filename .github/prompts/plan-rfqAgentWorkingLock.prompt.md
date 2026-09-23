# Plan: RFQ Agent-Working Lock

> Status: **APPROVED — implementing** (2026-09-23)
> Related: [plan-rfqBulkOperations.prompt.md](plan-rfqBulkOperations.prompt.md),
> [plan-grouped-supplier-search.prompt.md](plan-grouped-supplier-search.prompt.md)
> Scope: lock the RFQ read-only while the RFQ-creation pipeline is still
> adding items, with a visible "agent is working" banner.

---

## 0. Approved decisions (2026-09-23)

Three scope questions were put to the user and answered. These supersede the
recommendations in §8.

| # | Question | Decision |
|---|---|---|
| 1 | Scope of read-only | **Lock all** — items *and* the header. Stage 3 also calls `_update_rfq_sync()` for title/notes, so `POST /partial/rfqs/{rfq_id}/update` must be guarded alongside the item endpoints. |
| 2 | Staleness threshold | **600 s** — reuse the existing `_PIPELINE_STALE_SECONDS` constant already used by `_annotate_pipeline_flags()` for the same class of marker. |
| 3 | Supplier operations (swap/merge/drop/shortlist) | **Out of scope.** Stage 3 does not touch suppliers, so they stay unlocked. |

One implementation change from the original design: instead of a bespoke
`hx-trigger="every 10s"` banner, **reuse the existing comms poller idiom**
(`data-processing` + `data-poll-url` consumed by `window._pipelinePoll` in
`base.html`, formerly `_commsPoll` — generalised to drive any polled block). That
pattern already does exactly this job for the comms tab, so
generalising it keeps one polling mechanism in the codebase rather than two.

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
  **600 s** (`_PIPELINE_STALE_SECONDS`, decision 2) is treated as absent. A
  crashed daemon thread therefore self-heals the lock.

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

`_PIPELINE_STALE_SECONDS = 600` — the constant already defined at the top of
`routes/rfqs.py` for exactly this class of marker. Reused rather than
redefined, so both staleness checks stay in step.

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

Wired into the **mutation** endpoints (decision 1 — items + header):

| Endpoint | Line |
|---|---|
| `POST /partial/rfqs/{rfq_id}/update` | 2019 |
| `POST /partial/rfqs/{rfq_id}/update-item` | 2139 |
| `DELETE /partial/rfqs/{rfq_id}/delete-item/{line}` | 2178 |
| `POST /partial/rfqs/{rfq_id}/bulk-update-items` | 2242 |
| `POST /partial/rfqs/{rfq_id}/add-item` | 2282 |

Supplier operations (swap / merge / drop / shortlist / copy / **clear-suppliers**)
are deliberately **not** guarded — decision 3. `clear-suppliers` mutates a
line's supplier list, not its item fields, so it cannot race `_add_items_sync`
or `_update_rfq_sync`.

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
- The banner wrapper carries `data-poll-url` and is polled by the shared
  comms poller (`window._pipelinePoll`) at 4 s while `data-processing="true"` —
  the poll element disappears with the banner, so there is no polling
  overhead when idle. One polling mechanism, not two.
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
| Flag stale > 600 s (crash) | Normal | Allowed (self-healed) |
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
  first tab's poll shows the banner within 4 s.
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

Resolved (see §0):

1. **Scope of read-only** — **resolved: lock all** (items + header). Supplier
   operations stay out of scope.
2. **Staleness threshold** — **resolved: 600 s**, matching the existing
   `_PIPELINE_STALE_SECONDS`.
3. **Lock whole RFQ or items tab only** — **resolved: whole RFQ.** A banner is
   shown on the items tab (where the writes land), and the header edit form is
   suppressed server-side while the flag is active.

Still open (no decision needed to ship v1):

4. **Malformed flag handling** — treat malformed `pipeline_activity` as
   active (lock) or absent (unlock)? Recommend absent + log warning, to
   avoid a stuck lock.
5. **Guard `/api/rfq/{rfq_number}/extract-items`** (agent-initiated
   re-extraction) with the same 409? It is a different async mechanism.
6. **Cross-thread completion push** — worth adding a
   cross-thread-memory notification so Chainlit users get an instant
   `dashboard_refresh` instead of waiting ≤4s? (Poll is the fallback
   either way.)

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

---

## 10. Post-implementation fix — lock DISCOVERY (2026-09-23)

Reported after the first real test: *"run the script, navigate to the RFQ
immediately, and it is not read-only; reload or change tabs and the message
appears."*

### Diagnosis

Verified against the running server (forged session, real HTTP): the server was
correct in every case — banner and read-only markup both present when the lock
existed at render time, `Cache-Control: no-store`, `historyCacheSize = 0`. The
gap between "RFQ created" and "lock stamped" measured **133 ms**, far too short
for a human to hit deliberately.

**The actual defect was discovery, not rendering:** the only element on the items
tab carrying `data-poll-url` was the banner, and the banner only renders
`{% if pipeline_active %}`. So a tab rendered while unlocked had **no poller at
all** and could never learn that a run started. Nothing pushed either — the
create-RFQ pipeline runs in a daemon thread with no chat context, so it emits no
`dashboard_refresh`. Result: any page open (or already rendered) when a run
started stayed editable until a manual reload.

The test workflow made it easy to reproduce: `--reset` deletes the RFQ and
numbering is `max+1`, so the recreated RFQ reuses the **same number** — a stale
tab and the new run share a URL.

### Fix

1. **Always-on state watcher on the items tab.** A hidden
   `#rfq-pipeline-watch` div (rendered while `pipeline_active or rfq_is_fresh`)
   polls a new cheap JSON endpoint
   `GET /partial/rfqs/{id}/pipeline-active` → `{"active", "step", "label"}`.
   `window._pipelineWatch` in `base.html` compares against the page's current
   state and re-renders the items tab **only on a flip**, in either direction.
   This covers the 133 ms window, an already-open page, and unlocking. The
   `start()` early-return re-syncs state from the freshly rendered markup, so our
   own refresh cannot be mistaken for a change and cause a loop.
2. **Watch window** = `_PIPELINE_WATCH_WINDOW_HOURS = 1`, using the existing
   `age_hours` (`0` means "under an hour" *and* "unknown" — both err towards
   watching). Idle old RFQs generate no traffic.
3. **Stamp the lock in the route, before the handoff.** `addon.create_rfq()` now
   calls `_set_rfq_pipeline_activity(rfq_number, "extracting_items")` before
   `trigger_rfq_creation_pipeline()`, so the RFQ is already read-only by the time
   the caller learns its number. If the thread fails to start, the route clears
   the lock so an RFQ can never be stranded locked.
4. **One debounced refresh path** — `_refreshItemsTab()` (1.5 s) shared by the
   watcher and the banner's `pipelineDone` trigger, which can both fire within
   the same second when a run ends.

Tests: `TestFreshnessFlag`, `TestPipelineActiveProbe`,
`TestAddonRouteLocksBeforeHandoff` (+10, total 52 in the lock file).

### 10.1 Follow-up — the watcher never actually ran (2026-09-23)

Reported next: *"when the read-only warning finishes it doesn't reload the item
list, so I get an empty RFQ until I reload."*

Verified against the server first (real run, polling everything the browser
polls): the server is correct throughout — items appear at ~52 s while still
locked, and at the unlock moment the items partial already renders them:

```
 52.0  True    1403   2 item rows   <- items appear, still locked
 53.1  False      0   2 item rows   <- lock cleared, items present
```

So a refresh at unlock would have shown them. The fault was entirely client-side,
and it was **two wiring bugs in `base.html`**, either of which alone stops the
watcher dead:

1. **Definition-after-use.** `_reconcilePolls()` was called (to kick the pollers
   on the initial render) *above* the `window._pipelineWatch = {…}` assignment.
   The first reconcile therefore touched `undefined` and threw a `TypeError` —
   silently, in an event handler. The banner's own poller starts earlier in the
   same function, so it kept working (which is why the banner appeared and
   disappeared correctly), but the watcher never started.
2. **Signature mismatch.** The call site passed no argument while `start(el)`
   read `el.dataset`, so even later reconciles threw.

Fixes: the watcher now reads the element itself (no argument to get wrong), the
`_reconcilePolls()` kick moved below the assignment, every `_pipelineWatch`
reference is guarded, and the state store is the **rendered DOM node**
(`data-active`) rather than a JS variable — so a re-render can't desync it.

Behaviour is now verified without a browser, by driving the extracted script
block in Node with a stub DOM: stale-locked → refresh, current → no refresh (no
loop), unlocked → discovers a new run, element removed → stops.

**Lesson worth keeping:** the previous round of "verification" only proved the
*server* half. Client wiring was asserted, never executed — and both bugs sat in
the unexecuted half. Drive the JS too.

