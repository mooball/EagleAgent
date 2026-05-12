# Plan: RFQ ↔ Thread Binding

## Objective

Bind each RFQ to a dedicated Chainlit chat thread so that:

1. Each RFQ always opens with its own conversation context — no cross-contamination from unrelated discussions.
2. Returning to an RFQ days later resumes the same thread (full history visible).
3. Each user gets their own thread per RFQ (no shared threads between users).
4. The thread is clearly titled with the RFQ number for easy identification.
5. The "New RFQ" button in the dashboard creates a fresh thread before prompting the user.

---

## Current Architecture

### Thread lifecycle

| Event | What happens |
|-------|--------------|
| Page load | `base.html` calls `GET /api/latest-thread` → resumes the user's most recent Chainlit thread regardless of context |
| New chat | `@cl.on_chat_start` creates a random `uuid4()` thread_id, stores in `cl.user_session` |
| Resume | `@cl.on_chat_resume` restores thread_id from persisted data |
| RFQ creation | `manage_rfq(action="create")` accepts `data.thread_id` but nothing passes it — always `None` |
| Navigation | `data-dashboard-context` broadcasts `{view, entity, id}` but the iframe thread never changes |

### Key files

| File | Role |
|------|------|
| `app.py` | `@cl.on_chat_start` — creates thread_id; `@cl.on_chat_resume` — restores thread_id; `FixedSQLAlchemyDataLayer.update_thread()` — upserts thread rows with name/metadata |
| `includes/agent_bridge.py` | Dashboard → Agent action dispatch; `notify_dashboard()` for Agent → Dashboard messages |
| `includes/chat/actions.py` | `handle_new_rfq()` → `_handle_intent("new_rfq")` — sets intent context and sends follow-up prompt |
| `includes/tools/quote_tools.py` | `manage_rfq(action="create")` — creates RFQ row; `_rfq_to_dict()` — serialises RFQ including `thread_id` |
| `includes/dashboard/routes.py` | `GET /api/latest-thread` — returns most recent thread; `rfq_detail()` — serves RFQ detail view |
| `templates/base.html` | `init()` — fetches latest-thread and switches iframe src; listens for `thread_id` postMessage |
| `templates/partials/rfq_detail.html` | `data-dashboard-context` — broadcasts `{view: "rfq_detail", id: "RFQ-..."}` |
| `templates/partials/rfq_list.html` | "+ New RFQ" button → calls `/api/agent-bridge` with `{action: {name: "new_rfq"}}` |
| `public/embedded.js` | Receives `dashboard_context` postMessages and pushes to server |

### Problems

1. **No thread binding on RFQ creation.** `thread_id` on the RFQ row is always `None` — the create flow never captures the current session thread_id.
2. **No thread switching on navigation.** Viewing an RFQ detail page doesn't switch the chat iframe to the RFQ's thread. The user sees whatever thread they were last on.
3. **No per-user threads.** The `RFQ.thread_id` column is a single value. If two users work on the same RFQ, they'd share a thread (confusing).
4. **"New RFQ" doesn't create a new thread.** Clicking the button dispatches `new_rfq` intent into the *current* thread, which may contain unrelated conversation history.
5. **Thread naming.** Threads have no meaningful name — they show as the first message text in the Chainlit history sidebar.
6. **Stale thread on return.** `GET /api/latest-thread` always returns the most recent thread, not the one relevant to the page being viewed.
7. **"New RFQ" chat command button is misleading.** The Eagle Agent profile shows a "New RFQ" command button in the chat input. This implies creating an RFQ on the current thread, which contaminates thread context. There is no good situation to create an RFQ from within an existing thread via a command button.
8. **Typed RFQ creation requests.** A user can type "create a new RFQ for Acme Construction with 5x cordless drills" in any thread. If the thread is bound to a different RFQ or has unrelated history, the RFQ gets polluted context.

---

## Data Model

### Option A: Separate junction table (recommended)

```sql
CREATE TABLE rfq_threads (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rfq_number  VARCHAR NOT NULL,          -- FK → rfqs.rfq_number
    user_email  VARCHAR NOT NULL,          -- the user who owns this thread
    thread_id   VARCHAR NOT NULL,          -- Chainlit thread UUID
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (rfq_number, user_email)        -- one active thread per user per RFQ
);
```

**Why a junction table?**
- Supports per-user threads without changing the RFQ model.
- Keeps the existing `RFQ.thread_id` column as a legacy/optional field (the thread that *created* the RFQ).
- Easy to query: "give me the thread for this user + RFQ" or "give me all threads for this RFQ".
- Future-proof: could support multiple threads per user per RFQ if desired.

### Option B: Keep using `RFQ.thread_id` column

Simpler but doesn't support per-user threads. Would need to add a `thread_user` column or accept single-user-per-RFQ limitation. Not recommended.

---

## Implementation Plan

### Phase 1: Data model and API

#### 1.1 Create `rfq_threads` table
- Alembic migration to create the table.
- SQLAlchemy model in `includes/dashboard/models.py`.

#### 1.2 Add API endpoints
- `GET /api/rfq-thread?rfq_id=RFQ-2026-0042` — returns the thread_id for the current user + RFQ (creates one if none exists).
- `POST /api/rfq-thread` with `{rfq_id, thread_id}` — binds/rebinds a thread to an RFQ for the current user.

#### 1.3 Update `GET /api/latest-thread`
- Accept an optional `?rfq_id=` parameter.
- If provided, return the RFQ-bound thread for the current user instead of the most recent global thread.
- Fall back to the current behaviour (most recent thread) when no rfq_id is given.

---

### Phase 2: Thread creation and RFQ binding

#### 2.0 Remove "New RFQ" command button from chat
The Eagle Agent profile currently shows a "New RFQ" command button in the chat input area (registered via `includes/chat/actions.py` → `handle_new_rfq`). This button implies creating an RFQ on the current thread, which is always wrong — there is no good scenario for it:

- If the thread is bound to an RFQ → creates a conflicting second RFQ in the same thread.
- If the thread has other history → pollutes the new RFQ's context.
- If the thread is fresh → the dashboard button already handles this correctly.

**Action:** Remove the `new_rfq` entry from the command buttons shown in Eagle Agent's `@cl.on_chat_start` and `@cl.on_chat_resume`. Keep the action handler registered (it's still needed for the dashboard bridge), but don't surface it as a chat command.

Files: `app.py` (remove `new_rfq` from `eagle_commands`)

#### 2.1 "+ New RFQ" dashboard button creates a new thread
Currently the "+ New RFQ" button in `rfq_list.html` calls `/api/agent-bridge` with `{action: {name: "new_rfq"}}`. This dispatches the intent into the *current* thread.

New flow:
1. Button click navigates the iframe to `/chat` (which creates a fresh thread via `@cl.on_chat_start`).
2. Once the iframe finishes loading, dispatch the `new_rfq` intent via the agent bridge.
3. The thread gets bound to the RFQ once the RFQ is actually created (Phase 2.2).
4. If the user cancels (never creates the RFQ), the thread remains unbound and behaves as a normal general-purpose thread.

#### 2.2 Bind thread on RFQ creation
When `manage_rfq(action="create")` runs:
1. Read `thread_id` from `cl.user_session.get("thread_id")`.
2. Read `user_id` from `cl.user_session.get("user_id")`.
3. Insert into `rfq_threads(rfq_number, user_email, thread_id)`.
4. Also store in `RFQ.thread_id` for backwards compatibility.
5. Name the thread: call `update_thread(thread_id, name=f"RFQ {rfq_number} — {customer}")` so the Chainlit sidebar shows a meaningful label.

#### 2.3 Handle typed RFQ creation requests (redirect with data carry)

Users may type "create a new RFQ for Acme Construction with 5x cordless drills" in any thread. The system must ensure the RFQ ends up on a clean thread.

**Decision matrix:**

| Thread state | Action |
|-------------|--------|
| Fresh unbound thread (no prior messages) | Allow — create RFQ and bind thread ✅ |
| Unbound thread with prior history | Redirect — create new thread, carry the data |
| Bound to a different RFQ | Redirect — create new thread, carry the data |
| Bound to the same RFQ | Impossible — RFQ already exists |

**Redirect mechanism (data carry):**

1. The agent detects an RFQ creation intent in a dirty/bound thread.
2. The agent extracts the structured RFQ data from the user's message (customer, items, etc.).
3. The agent stores a "pending RFQ" payload in a server-side queue keyed by `user_email` (e.g. a lightweight DB table `pending_rfq_requests` or an in-memory dict with TTL).
4. The agent responds: *"I'll start a fresh thread for your new RFQ..."* and calls `notify_dashboard("create_rfq_thread")` — a postMessage to the parent frame.
5. `base.html` receives the message and navigates the iframe to `/chat` (fresh thread).
6. `@cl.on_chat_start` checks for a pending RFQ payload for the current user. If found, it auto-triggers the creation flow with the carried data.
7. The RFQ is created and bound to the new clean thread.

**Fallback:** If the redirect mechanism fails (e.g. postMessage lost), the agent should display a message: *"Please click the **+ New RFQ** button in the RFQ listing to start a clean thread. Your data: [summary]."*

Files: `app.py` (`@cl.on_chat_start` — check for pending RFQ), `includes/tools/quote_tools.py` (thread state check before create), `templates/base.html` (handle `create_rfq_thread` postMessage)

---

### Phase 3: Thread switching on RFQ navigation

#### 3.1 RFQ detail page triggers thread switch and panel open
When the user navigates to `/rfqs/RFQ-2026-0042`:
1. The `rfq_detail` route looks up the user's thread for this RFQ (from `rfq_threads`).
2. The thread_id is included in the `data-dashboard-context` JSON: `{"view": "rfq_detail", "entity": "rfq", "id": "RFQ-...", "thread_id": "abc-123"}`.
3. `base.html`'s `updateContext()` detects the `thread_id` in the context and, if it differs from `_activeThreadId`, switches the iframe: `iframe.src = '/chat/thread/' + ctx.thread_id`.
4. If there is no thread for this user+RFQ yet (e.g. viewing someone else's RFQ for the first time), create one on-demand.
5. Auto-open the agent panel if it's currently closed. Set `agentState = 'panel'` and ensure `panelWidth` is at least 30–40% of the viewport width so the thread is comfortably readable.
6. If the user's current chat profile is not "Eagle Agent", auto-switch to it to ensure the RFQ gets consistent tooling (internal + research agents).

#### 3.2 Non-RFQ navigation keeps the current thread
When the user navigates to `/suppliers/{id}` or `/products/{id}`:
- The context has no `thread_id` field (only RFQ views include it).
- The iframe stays on whatever thread it was on — no switching.
- This is the desired behaviour: viewing a supplier from within an RFQ context shouldn't disrupt the thread.

#### 3.3 RFQ list / home reverts to latest-thread
When the user navigates to `/rfqs` (list) or `/` (home):
- No `thread_id` in context.
- Could optionally revert to the most recent thread, or just leave the iframe as-is.
- **Recommended: leave as-is.** Only explicitly viewing an RFQ detail page should switch threads.

---

### Phase 4: Thread naming and UX polish

#### 4.1 Name threads on creation
When an RFQ is created and bound to a thread, call `update_thread()` to set the thread name to the RFQ number (e.g. `"RFQ-2026-0042 — Acme Construction"`). This makes the Chainlit thread history sidebar useful.

#### 4.2 Pre-populate RFQ context in thread
When an RFQ-bound thread is started or resumed, push a rich RFQ summary into the dashboard context so the agent has immediate awareness without needing to call `get_rfq`. The `data-dashboard-context` on the RFQ detail page should include:
- `rfq_id`, `customer`, `status`, `created_date`, `assigned_to`
- Item summary: line count, identified vs unidentified, key part numbers
- Supplier summary: count per item, any shortlisted/selected suppliers

This data is already available in the `rfq_detail` route (the `rfq` dict). Include a condensed version in the context JSON. The agent's system prompt already reads `dashboard_context` — it will automatically have the RFQ state on every message.

#### 4.3 Thread indicator in RFQ detail
Optionally show a small indicator in the RFQ detail header showing which thread is active, e.g.:
- 💬 Thread linked (clickable to open the chat panel if closed)
- Or simply auto-open the agent panel when viewing an RFQ detail page.

#### 4.4 "Start new thread" option
Add an option (button or chat command) to create a fresh thread for the same RFQ. The old thread remains in Chainlit history but the `rfq_threads` row is updated to point to the new thread_id.

Use case: the RFQ has been active for weeks and the thread is very long — the user wants a clean slate without losing history.

---

## Edge Cases

| Scenario | Handling |
|----------|----------|
| User views an RFQ they didn't create | Create a new thread for that user, bind it to the RFQ |
| User types "create RFQ" in a bound thread | Redirect to new thread with data carry (Phase 2.3) |
| User types "create RFQ" in an unbound thread with history | Redirect to new thread with data carry (Phase 2.3) |
| User types "create RFQ" in a fresh empty thread | Allow it — create RFQ and bind thread |
| Two users working on the same RFQ | Each gets their own thread (junction table) |
| RFQ created before this feature (no thread) | First time the user views the detail page, create and bind a thread |
| User clicks "+ New RFQ", then cancels | Thread exists but is unbound — behaves as a normal general thread |
| Thread becomes very long | "Start new thread" option re-binds a fresh thread |
| User navigates RFQ → supplier → back to RFQ | Thread should be restored — the supplier page doesn't clear it |
| Redirect postMessage lost / iframe doesn't reload | Agent falls back to displaying data summary + "click + New RFQ" instructions |
| Pending RFQ data expires | TTL on pending queue (e.g. 5 minutes); stale entries silently discarded |

---

## Summary of Changes by File

| File | Changes |
|------|---------|
| `alembic/versions/` | Migration to create `rfq_threads` table |
| `includes/dashboard/models.py` | Add `RFQThread` model |
| `includes/dashboard/routes.py` | Add `GET /api/rfq-thread`, `POST /api/rfq-thread`, `POST /api/rfq-thread/new`; update `rfq_detail()` to include `thread_id` in context; update `GET /api/latest-thread` to accept `?rfq_id=` |
| `includes/tools/quote_tools.py` | On `create`: capture `thread_id` from session, insert into `rfq_threads`, name the thread. Check thread state before create (redirect if bound/dirty) |
| `app.py` | Remove `new_rfq` from Eagle Agent command buttons; update `@cl.on_chat_start` to check for pending RFQ data and auto-trigger creation; send thread_id via postMessage on start |
| `includes/chat/actions.py` | Keep `handle_new_rfq` action handler (used by dashboard bridge), but no longer surfaced as a chat command |
| `templates/base.html` | `updateContext()` — switch iframe when context includes `thread_id`; auto-open panel to 30–40% on RFQ detail views; handle `create_rfq_thread` postMessage; don't switch on non-RFQ navigation |
| `templates/partials/rfq_detail.html` | Include `thread_id` and condensed RFQ summary in `data-dashboard-context` |
| `templates/partials/rfq_list.html` | Update "+ New RFQ" button to navigate iframe to `/chat` (fresh thread), then dispatch `new_rfq` intent after load |
| `tests/` | Tests for `rfq_threads` CRUD, thread switching logic, thread conflict detection, pending RFQ data carry |

---

## Resolved Decisions

1. **"New RFQ" chat command button:** Remove from chat entirely. The dashboard "+" button is the only entry point for new RFQs. The action handler stays registered for the bridge, but is not surfaced as a command.

2. **Typed RFQ creation in wrong thread:** Don't refuse — redirect to a new thread and carry the data. The agent extracts the structured data, stores it server-side, triggers a new thread via postMessage, and `@cl.on_chat_start` picks up the pending data to auto-create the RFQ.

3. **Fresh empty thread:** Allow RFQ creation directly — the thread is clean, so just bind it. No redirect needed.

4. **Auto-open agent panel on RFQ detail:** Yes. When navigating to an RFQ detail page, auto-open the agent panel to ~30–40% width so the user immediately sees the relevant thread.

5. **Pre-populate thread context:** Yes. When creating/resuming an RFQ thread, inject the RFQ summary (customer, items, status, suppliers) into the thread context so the agent doesn't have to re-scan the RFQ on every message. Use the dashboard_context mechanism to push RFQ data alongside the view/entity info.

6. **Thread cleanup:** Leave orphaned threads for now. Garbage collection can be added later.

7. **Chat profile consistency:** Always use "Eagle Agent" profile for RFQ threads. If the user is on a different profile when they navigate to an RFQ, auto-switch to Eagle Agent.

8. **Pending RFQ storage:** In-memory dict with TTL (e.g. 5 minutes). This is a short-lived event and a server restart would naturally expect a fresh RFQ flow.

---

## Open Questions

None — all design questions have been resolved above.
