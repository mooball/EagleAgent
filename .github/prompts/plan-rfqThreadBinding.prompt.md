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

### Phase 1: Data model and API ✅ DONE

#### 1.1 Create `rfq_threads` table ✅
- Alembic migration `9f2633750a73_create_rfq_threads_table.py` created.
- `RFQThread` model added to `includes/dashboard/models.py`.

#### 1.2 Add API endpoints ✅
- `POST /api/rfq-thread` with `{rfq_id, thread_id}` — binds/rebinds a thread to an RFQ for the current user.
- `base.html` handles `bind_rfq_thread` postMessage from iframe and POSTs to this endpoint.

#### 1.3 Update `GET /api/latest-thread` — SKIPPED (not needed)
- Thread switching is handled via `data-dashboard-context` + `thread_id` field and postMessage events, not by modifying `GET /api/latest-thread`.

---

### Phase 2: Thread creation and RFQ binding

#### 2.0 Remove "New RFQ" command button from chat ✅ DONE
- Eagle Agent profile sets `await cl.context.emitter.set_commands([])` — no command buttons shown.
- The `new_rfq` action handler remains registered for the dashboard bridge but is not surfaced as a chat command.

#### 2.1 "+ New RFQ" dashboard button creates a new thread ✅ DONE
Simplified from the original plan:
1. Button is now `hx-post="/rfqs/new"` — creates a blank draft RFQ (empty customer) and navigates to its detail view.
2. On the RFQ detail page, `base.html` detects no bound thread and creates one on the first `thread_id` postMessage from the iframe.
3. The `new_rfq` intent is triggered via agent-bridge after the thread is bound.
4. The intent prompt tells the agent to `update` the existing blank RFQ (not create a new one).

#### 2.2 Bind thread on RFQ creation ✅ DONE
- `base.html` `thread_id` handler: when a new thread_id arrives and a `_pendingRfqBind` exists, POSTs to `/api/rfq-thread` to bind the thread.
- `_create_rfq_sync` also stores `thread_id` in `rfq_threads` when provided.
- Thread naming on resume: `on_chat_resume` checks `rfq_threads` and calls `update_thread()` to set the name to `"RFQ-XXXX — Customer"`.

#### 2.3 Handle typed RFQ creation requests (redirect with data carry) ❌ NOT DONE
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

#### 3.1 RFQ detail page triggers thread switch and panel open ✅ DONE
When the user navigates to `/rfqs/RFQ-2026-0042`:
1. The `rfq_detail` route looks up the user's thread for this RFQ (from `rfq_threads`).
2. The thread_id is included in the `data-dashboard-context` JSON: `{"view": "rfq_detail", "entity": "rfq", "id": "RFQ-...", "thread_id": "abc-123"}`.
3. `base.html`'s `updateContext()` detects the `thread_id` in the context and, if it differs from `_activeThreadId`, switches the iframe: `iframe.src = '/chat/thread/' + ctx.thread_id`.
4. If there is no thread for this user+RFQ yet, a new one is created on-demand when the iframe loads and sends its `thread_id` postMessage back.
5. Agent panel auto-opens: `agentState = 'panel'` is set when thread switching occurs.

**Not implemented:** Auto-switch to "Eagle Agent" chat profile if user is on a different profile. Low priority — users rarely switch profiles mid-session.

#### 3.2 Non-RFQ navigation keeps the current thread ✅ DONE (by design)
When the user navigates to `/suppliers/{id}` or `/products/{id}`:
- The context has no `thread_id` field (only RFQ views include it).
- The iframe stays on whatever thread it was on — no switching.
- This is the desired behaviour: viewing a supplier from within an RFQ context shouldn't disrupt the thread.

#### 3.3 RFQ list / home reverts to latest-thread ✅ DONE (by design — leave as-is)
When the user navigates to `/rfqs` (list) or `/` (home):
- No `thread_id` in context.
- The iframe stays on the current thread. Only explicitly viewing an RFQ detail page switches threads.

---

### Phase 4: Thread naming and UX polish

#### 4.1 Name threads on creation ✅ DONE
When an RFQ-bound thread is resumed, `on_chat_resume` looks up the `rfq_threads` binding and calls `update_thread()` to set the thread name to `"RFQ-2026-0042 — Customer Name"`.

#### 4.2 Pre-populate RFQ context in thread ✅ DONE
The `rfq_detail` template includes a condensed RFQ summary in `data-dashboard-context`:
- `id`, `customer`, `status`, `item_count`, `identified_count`, `assigned_to`
- `thread_id` (if bound)
- This is pushed to the agent via `POST /api/dashboard-context` and prepended to every user message.

#### 4.3 Thread indicator in RFQ detail ✅ DONE
Two indicators implemented:
1. **Chat button** on the RFQ detail header — opens the bound thread (or `/chat` if none) and shows the agent panel.
2. **RFQ context banner** in the chat iframe (`embedded.js`):
   - Green banner when the chat thread matches the viewed RFQ's bound thread.
   - Amber warning banner with a "Link" button when the thread differs.
   - Hidden when no binding exists yet.

#### 4.4 "Start new thread" option ❌ NOT DONE
Add an option (button or chat command) to create a fresh thread for the same RFQ. The old thread remains in Chainlit history but the `rfq_threads` row is updated to point to the new thread_id.

Use case: the RFQ has been active for weeks and the thread is very long — the user wants a clean slate without losing history.

#### 4.5 Sidebar persistence ✅ DONE (bonus — not in original plan)
`embedded.js` reads the `sidebar:state` cookie on load and auto-closes the Chainlit sidebar if it was previously collapsed. Known minor issue: brief flash of open→close on load.

#### 4.4 "Start new thread" option
Add an option (button or chat command) to create a fresh thread for the same RFQ. The old thread remains in Chainlit history but the `rfq_threads` row is updated to point to the new thread_id.

Use case: the RFQ has been active for weeks and the thread is very long — the user wants a clean slate without losing history.

---

## Edge Cases

| Scenario | Handling | Status |
|----------|----------|--------|
| User views an RFQ they didn't create | Create a new thread for that user, bind it to the RFQ | ✅ Done (on-demand via thread_id postMessage) |
| User types "create RFQ" in a bound thread | Redirect — create new thread, carry the data | ❌ Not done |
| User types "create RFQ" in an unbound thread with history | Redirect — create new thread, carry the data | ❌ Not done |
| User types "create RFQ" in a fresh empty thread | Allow it — create RFQ and bind thread | ✅ Done |
| Two users working on the same RFQ | Each gets their own thread (junction table) | ✅ Done |
| RFQ created before this feature (no thread) | First time the user views the detail page, create and bind a thread | ✅ Done |
| User clicks "+ New RFQ", then cancels | Thread exists but is unbound — behaves as a normal general thread | ✅ Done |
| Thread becomes very long | "Start new thread" option re-binds a fresh thread | ❌ Not done |
| User navigates RFQ → supplier → back to RFQ | Thread should be restored — the supplier page doesn't clear it | ✅ Done |
| Redirect postMessage lost / iframe doesn't reload | Agent falls back to displaying data summary + "click + New RFQ" instructions | ❌ Not done (part of 2.3) |
| Pending RFQ data expires | TTL on pending queue (e.g. 5 minutes); stale entries silently discarded | ❌ Not done (part of 2.3) |

---

## Summary of Changes by File

| File | Changes | Status |
|------|---------|--------|
| `alembic/versions/9f2633750a73_...` | Migration to create `rfq_threads` table | ✅ Done |
| `includes/dashboard/models.py` | Add `RFQThread` model | ✅ Done |
| `includes/dashboard/routes.py` | Add `POST /api/rfq-thread`, `POST /rfqs/new`; `rfq_detail()` includes `thread_id` in context; sort fix for RFQ listing | ✅ Done |
| `includes/dashboard/context.py` | `format_context_for_prompt()` includes RFQ-specific summary fields | ✅ Done |
| `includes/tools/quote_tools.py` | `_create_rfq_sync` allows empty customer; `add_items` bulk action; create docstring includes `part_number`/`brand` in items schema | ✅ Done |
| `includes/prompts.py` | `new_rfq` intent rewritten: update existing RFQ, explicit field mapping | ✅ Done |
| `app.py` | Eagle Agent commands cleared; `on_chat_start` sends `thread_id` postMessage; `on_chat_resume` names RFQ threads | ✅ Done |
| `templates/base.html` | `updateContext()` thread switching; `thread_id` handler with RFQ bind; `bind_rfq_thread` handler; auto-open panel; auto-open blank RFQ edit form | ✅ Done |
| `templates/partials/rfq_detail.html` | `thread_id` in context; Chat button | ✅ Done |
| `templates/partials/rfq_list.html` | Simplified "+ New RFQ" to `hx-post="/rfqs/new"` | ✅ Done |
| `public/embedded.js` | RFQ context banner (linked/unlinked); sidebar persistence | ✅ Done |
| `public/stylesheet.css` | Banner styles (linked/unlinked); dark mode variants | ✅ Done |
| `tests/` | 13 new tests for thread binding, context, blank RFQ, sort order | ✅ Done |
| `app.py` — pending RFQ data carry in `@cl.on_chat_start` | Phase 2.3: check for pending RFQ payload, auto-trigger creation | ❌ Not done |
| `includes/tools/quote_tools.py` — thread state check | Phase 2.3: detect RFQ creation in dirty/bound thread, redirect | ❌ Not done |
| `templates/base.html` — `create_rfq_thread` handler | Phase 2.3: handle redirect postMessage from agent | ❌ Not done |

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

---

## Remaining Work

Two items are not yet implemented:

### Phase 2.3: Typed RFQ creation in wrong thread (data carry redirect)
**Priority: Low.** The current "+ New RFQ" button flow handles 95% of cases. Typed creation in a wrong thread is an edge case that can be addressed later. The agent's `new_rfq` intent already checks dashboard context and updates existing RFQs, which mitigates the most common variant of this problem.

### Phase 4.4: "Start new thread" option
**Priority: Low.** Only needed when threads become very long after weeks of RFQ activity. Can be a simple button or chat command that creates a fresh thread and rebinds it.
