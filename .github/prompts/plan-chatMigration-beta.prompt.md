# Plan: Beta Coexistence — New Chat UI Behind a Feature Flag

> Parent: [plan-chatMigration.prompt.md](plan-chatMigration.prompt.md)
> Status: **VALIDATED** (2026-09-06). The beta POC shipped behind the flag and
> was validated in production on Railway. The goal — de-risk the migration by
> running the new chat UI **alongside** Chainlit, exposed only to a small
> allowlist — is met. Remaining migration work is tracked in
> [parity-checklist-chat.md](parity-checklist-chat.md).
>
> **This is a delivery-strategy overlay on Phases 2–3, not a parallel design.**
> The architecture is already decided in the parent plan and is not reopened here:
> Phase 2 supplies the HTTP endpoints over `run_turn()` (see the `runner.py`
> docstring), and the Phase 3 gate fixes the frontend as **bespoke Alpine/HTMX +
> `EventSource` + Preline UI** (decided 2026-08-19). This document only adds:
> *how to ship that safely to a handful of users first.*

---

## Overview

Instead of a big-bang switch, deliver the Phase 2–3 work as an **additive**
feature:

- New UI served at its own path (`/chat-ui`) by the existing FastAPI app.
- Chainlit remains mounted at `/chat`, completely untouched.
- An env-var allowlist (`CHAT_UI_BETA_USERS`) gates access; unlisted users
  never see it. Empty/absent var = feature off.
- The new UI reuses the transport-neutral streaming core
  (`includes/chat/runner.py` + `ChatContext` protocol) with a **second
  adapter** (`context_sse.py`) that streams over SSE.
- Both UIs read and write the **same `threads` / `steps` tables**
  (parent plan §11, Track A — see “Persistence” below), so threads created in
  one appear in the other.

The POC's specific job: prove **SSE through the Railway proxy** and the
runner under real conditions, with minimal risk to existing users.

---

## The load-bearing invariant (must not break)

> `chainlit.threads.id` == LangGraph `configurable.thread_id` ==
> `rfq_threads.thread_id` == `rfqs.thread_id`

The new UI **creates threads**, so this is a POC acceptance criterion, not a
footnote. A thread created in `/chat-ui` must use the same UUID across all four
places, or it orphans checkpoints and RFQ bindings. Verify explicitly:
create a thread in the new UI → open it in Chainlit → bind it to an RFQ →
confirm all four ids match.


---

## Current Architecture (verified)

- `main.py` — FastAPI app with Google OAuth session auth; Chainlit mounted at
  `/chat`; dashboard router included as plain FastAPI routes; `SessionMiddleware`
  (15-day max_age, matches Chainlit).
- `includes/chat/runner.py` — the streaming loop (`astream_events(v2)`),
  token accounting, tool-progress line; **no Chainlit imports**.
- `includes/chat/context.py` — `ChatContext` Protocol (get/set + emit surface).
- `includes/chat/context_chainlit.py` — the Chainlit adapter implementing it.
- `includes/chat/streaming_logic.py` — extracted pure helpers.
- `templates/base.html` + dashboard routes — existing FastAPI/Preline stack to
  model the new UI on (htmx + Preline + Alpine per house style).

The plan follows the same adapter pattern Phase 1 established: a new transport
is a new `ChatContext` implementation, not a change to the core.

---

## Feature-Flag Mechanics

- **Env var:** `CHAT_UI_BETA_USERS` — comma-separated emails
  (e.g. `tom@mooball.net,jane@mooball.net`). Absent/empty = feature off.
- **Server-side gate:** FastAPI middleware checks the session user's email
  against the allowlist for all `/chat-ui/*` routes → unlisted users get 404
  (invisible), not a broken page.
- **UI hint:** dashboard nav shows a "New chat (beta)" link **only** to
  allowed users.
- **Kill switch:** remove the env var (or delete users from it) → everyone
  falls back to Chainlit. This is an env-var change and a service restart on
  Railway — fast, but not literally zero-downtime and not a no-op.

---

## Persistence — Track A (parent plan §11)

**Do not invent a new store, and do not rely on the checkpointer for the thread
list.** Two independent stores exist and they are not interchangeable:

| Store | Contains | Used for |
|---|---|---|
| LangGraph checkpoints | The real message history the model sees | The run itself |
| `threads` / `steps` / `elements` | The rendered transcript + thread list | What the UI displays |

The new UI follows **Track A — keep the schema, swap the writer**:

- Read: `steps WHERE type IN ('user_message','assistant_message')` ordered by
  `createdAt`/`start` → render as messages. Thread list comes from `threads`.
- Write: new messages into the same tables, minus Chainlit's quirks.
- Zero data migration, zero `thread_id` churn, and Chainlit keeps working off
  its own tables the whole time.
- Schema tidy-ups (`createdAt` → `timestamptz`, dropping Chainlit-only columns,
  renaming out of the Chainlit namespace) are **post-cutover**, not POC work.

### Rendering historical messages — sanitise on read

Chainlit ran with `unsafe_allow_html = true`, so historical `steps.output`
contains **baked-in HTML** (notably the token/cost footer `<div>`). The new UI
must **never** render `steps.output` as raw HTML — sanitise it or render as
markdown-only, otherwise old messages show literal markup at best and are an
XSS vector at worst. This is required for the POC because viewing historical
threads is in scope.

### Concurrency — `RunInProgress`

`run_turn()` holds a per-`thread_id` lock and raises `RunInProgress` if a run is
already active. A beta user with both UIs open on the same thread **will** hit
this. The new UI must surface it gracefully (409 → “a run is already active on
this conversation”), not as a stack trace or a silent failure.


---

## New Pieces (all additive, as built)

```
includes/chat/
  context_sse.py        ← ChatContext implementation over an SSE queue
  transcript.py         ← Track A read/write adapter over `threads` / `steps` / `elements`
includes/dashboard/routes/chat_ui.py   ← /chat-ui routes (pages, CRUD, SSE, uploads)
includes/dashboard/routes/api.py       ← +DELETE /api/rfq-thread (unbind only)
templates/chat_ui/
  embed.html            ← the dashboard-panel UI (primary beta surface)
  index.html            ← standalone thread list
  thread.html           ← standalone thread view (lags the panel; see notes)
main.py                 ← +include_router(chat_ui_router); /api/dashboard-context stores
                           thread-keyed entries
```

### SSE endpoint sketch

- `GET /chat-ui/threads/{thread_id}/stream` — Server-Sent Events:
  - `event: token` (`{"text": "..."}`)
  - `event: tool` (`{"name": "search_products", "count": 3}`) → "⏳ Using … (xN)"
  - `event: message` (complete assistant message)
  - `event: action` (action-button payload — later phase)
  - `event: done` / `event: error`
- Runs the existing `runner` loop in the background task; `context_sse`
  buffers events into an `asyncio.Queue` the endpoint drains.
- **Stop** — `POST /chat-ui/threads/{thread_id}/stop` → reuse the existing
  cooperative-cancellation mechanism used by `stop_agent`.
- Reconnect semantics: client re-attaches and re-renders from `steps`
  (the transcript store). If `steps` has fallen behind an interrupted run, the
  checkpoint is the recovery source — the same reconciliation `on_chat_resume`
  already performs via `plan_resume_backfill()`.

### Auth

Reuses the existing session auth (`require_user`, same as dashboard).
No new login surface.

### Known limitation — dashboard action buttons are orphaned in the POC

**Accepted 2026-09-05, still current 2026-09-06.** `dispatch_action()` in
[includes/agent_bridge.py](includes/agent_bridge.py) resolves its target via
`WebsocketSession.get_by_id(session_id)` + `init_ws_context()` — a hard Chainlit
websocket dependency. A beta user working in `/chat-ui` has **no Chainlit
websocket session**, so the nine dashboard-initiated actions (C-A1–C-A9 in the
[parity checklist](parity-checklist-chat.md): “Classify & validate”,
“Find suppliers”, etc.) will not reach the new UI.

The POC does **not** fix this. Its goal is to validate the SSE framework, not to
replicate the whole UI. Consequences to accept and communicate to beta users:

- Dashboard action buttons still target Chainlit. Beta users keep `/chat` open
  (or switch back to it) for those flows.
- Re-pointing the bridge at a resolved context (new-UI context if present, else
  Chainlit) is **Phase 5 work**, done once the direction is confirmed.

### State sharing (decision: SHARED)

Both UIs read/write the same `threads`/`steps` tables → threads are visible in
both. Side-by-side comparison becomes trivial (same RFQ thread in both UIs).
No POC migrations needed.

---

## POC Scope

**In — all shipped and validated (2026-09-06):**

- [x] Allowlist middleware + env var
- [x] Thread list (create / resume / delete) read from `threads`
  - Rename is wired on the standalone page; **pending in the panel** (PATCH
    endpoint exists server-side) — only remaining POC-scope gap.
- [x] **Thread id invariant preserved** on creation (verified across
      `threads.id` / checkpoint / `rfq_threads` / `rfqs`)
- [x] Single thread view: history rendered from `steps`, **sanitised on read**
- [x] Composer → POST message → streamed tokens via SSE
- [x] Tool-progress line "⏳ Using {tool}… (xN)"
- [x] Stop button + cooperative cancellation
- [x] `RunInProgress` surfaced gracefully (409 → friendly message)
- [x] Agent routing via Tools commands — one-shot intent: server maps the
      command to its owning agent + intent context; plain messages use the
      default agent (no user-facing agent select)
- [x] Dark mode (follows dashboard theme)

**Added during the beta (beyond the original POC scope):**

- [x] Compact Preline-style composer (attach button, Tools dropdown with
      command prefills, small send button)
- [x] **File uploads** — ✚ button + drag-drop onto the composer, optimistic
      thumbnails, Chainlit-identical `elements` persistence, legacy attachment
      rendering (was listed "Out" below; moved in)
- [x] **RFQ hard-binding** — on an RFQ the panel is locked to that RFQ's
      thread: no escape to thread history, Clear (unbind+recreate) instead of
      delete, 🔗 badges on bound threads in the list, clicking a bound thread
      navigates the whole dashboard to its RFQ
- [x] **Thread-keyed dashboard context** — `thread:{id}` entries in the
      dashboard-context store end the multi-tab last-writer-wins problem (both
      transports push `_activeThreadId`; both read with their own thread id)

**Out (later phases, still behind the flag):**

- Dashboard action buttons reaching the new UI (Phase 5 — orphaned in POC, see below)
- Chat-emitted action buttons (Phase 4 machinery)
- Token footer / structured metadata
- Thread auto-naming polish
- `steps` schema tidy-ups (post-cutover)
- Full parity checklist items (tracked in [parity-checklist-chat.md](parity-checklist-chat.md))

---

## Rollout & Rollback

1. ✅ Landed the branch → deployed to Railway with `CHAT_UI_BETA_USERS` =
   1 account (2026-09-05).
2. ✅ Chainlit at `/chat` unchanged for everyone else. Note: the planned
   "New chat (beta)" nav link was **superseded** — beta users get the panel
   embed in place of the iframe instead of an extra link.
3. ✅ Validated on Railway: SSE through the proxy, stop button, cross-UI
   thread visibility, thread-id invariant, deep-link resume, RFQ hard-binding,
   multi-tab context isolation, file uploads.
4. ✅ Rollback drill passed: clearing the env var returns everyone to the
   Chainlit iframe.
5. Iterate parity features behind the flag (see parity checklist).
6. **End state:** once the signed-off parity checklist is complete, swap
   `/chat` to the new UI and remove Chainlit + its adapter.

---

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| SSE broken by Railway proxy | This POC exists to find out; flag limits blast radius |
| Regression in shared code | `runner`/`streaming_logic` untouched; tests/chat suite green before deploy |
| **Thread id invariant broken on create** | Explicit POC acceptance check across all four ids |
| **Historical HTML rendered raw** | Sanitise-on-read requirement; never render `steps.output` as HTML |
| Beta users lose dashboard action buttons | Documented limitation; keep `/chat` available for those flows |
| Two UIs racing one thread | Per-thread lock already exists; surface `RunInProgress` as 409 |
| Beta users get confused by two UIs | Link labelled "(beta)"; threads shared so nothing is lost |
| Security of `/chat-ui` without flag | Middleware 404s non-allowlisted users; same session auth |

---

## Open Questions — all answered

- Path: `/chat-ui` ✅ (panel embed on the dashboard replaces the iframe for
  beta users; standalone pages still served at `/chat-ui`).
- Allowlist source: env var ✅ (`CHAT_UI_BETA_USERS`, comma-separated).
- Writer parity: new-UI writes stay Chainlit-readable ✅ — steps use Chainlit's
  own upsert shape, threads/elements use the same tables, cross-UI visibility
  verified both ways.

## Testing

- ✅ `tests/chat/test_sse_context.py` — context_sse event contract, persisted
  step id as message id, transient non-persistence, cancellation.
- ✅ `tests/test_chat_ui_routes.py` — allowlist gate, thread CRUD, 409, SSE
  done, stop, embed routes, **uploads** (persist/type/delete/message attach).
- ✅ `tests/test_dashboard_context.py` — thread-keyed context isolation
  (thread entries win, email fallback).
- ⚠️ **Sanitisation test (B12a)** — verified by inspection only; no automated
  test yet.
- ⚠️ **Invariant test** — validated manually in prod; no automated test.
- ✅ Manual on Railway: SSE through proxy, stop, resume, cross-UI thread
  visibility, rollback.
