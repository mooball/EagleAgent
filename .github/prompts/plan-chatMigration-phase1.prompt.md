# Phase 1 — Decouple Business Logic from Chainlit

> Parent: [plan-chatMigration.prompt.md](plan-chatMigration.prompt.md)
> Depends on: [Phase 0](plan-chatMigration-phase0.prompt.md)
> Status: **IN PROGRESS** — started 2026-08-17.

## Goal

Give every piece of business logic an **explicit `ChatContext`** instead of reaching for
ambient `cl.*` globals. Ship it **on Chainlit**, with no user-visible change.

> This phase is independently valuable. If the project stops here, the codebase is
> materially better: testable, no ambient global state, and the thread-pinning hacks
> are gone. It is also the largest single piece of work in the whole plan.

**Success condition:** `import chainlit` appears only in [app.py](app.py) and
`includes/chat/`, enforced by CI.

---

## 1. Scope — measured

| File | Lines | Chainlit coupling |
| --- | --- | --- |
| [includes/chat/rfq_actions.py](includes/chat/rfq_actions.py) | 1,177 | **21** `@cl.action_callback`, 4 pinning helpers, ~40 `user_session` reads |
| [includes/tools/quote_tools.py](includes/tools/quote_tools.py) | 1,189 | `_stream_to_user`, `manage_rfq`, `_notify_rfq_updated`, data-layer rename |
| [app.py](app.py) | 1,289 | **4** `@cl.action_callback` (3 delete-all handlers dropped at parity), all lifecycle hooks, the stream loop |
| [includes/agent_bridge.py](includes/agent_bridge.py) | 260 | `WebsocketSession`, `init_ws_context`, locks |
| [includes/agents/base.py](includes/agents/base.py) | 502 | 1 call (`_notify_retry`, L132–136) |
| [includes/chat/actions.py](includes/chat/actions.py) | 295 | dispatcher + intent handlers |
| [includes/chat/job_progress.py](includes/chat/job_progress.py) | 80 | 4 message sites |
| [includes/chat/supplier_search_gate.py](includes/chat/supplier_search_gate.py) | ~150 | builds 5 `cl.Action` |
| [includes/chat/middleware.py](includes/chat/middleware.py) | 107 | `GeminiRetryNotifier` L97–100 |
| [includes/tools/job_tools.py](includes/tools/job_tools.py) | ~260 | confirmation buttons L52–69 |
| [includes/tools/browser_tools.py](includes/tools/browser_tools.py) | ~360 | `cl.Image` L180–192 |

**Removed from scope by the parity decision:**
[includes/chat/commands.py](includes/chat/commands.py) — its only contents,
`handle_deleteall_command`, serve the dropped delete-all feature. Also drops the
`delete_all_data` entry in [includes/chat/actions.py](includes/chat/actions.py#L172)
and the agent-callable path in
[includes/tools/action_tools.py](includes/tools/action_tools.py#L67).

**Already clean — do not touch:** [includes/graph.py](includes/graph.py),
[includes/chat/document_processing.py](includes/chat/document_processing.py),
[includes/tools/product_tools.py](includes/tools/product_tools.py),
[includes/tools/user_profile.py](includes/tools/user_profile.py).

**Stays Chainlit-coupled by design (deleted in Phase 6):**
[includes/chat/data_layer.py](includes/chat/data_layer.py),
[includes/chat/local_storage_client.py](includes/chat/local_storage_client.py).

---

## 2. The critical constraint

> **Tools do not currently receive `RunnableConfig`.**

Verified: `BaseSubAgent.__call__` takes `config` (base.py:278), merges
`recursion_limit` (L409), and passes it to `sub_agent_graph.ainvoke(config=…)` (L415)
and `model.ainvoke(…, config=config)` (L467) — but tools are bound via
`.bind_tools(tools)` and **never see it**. They read `cl.user_session` /
`cl.context.session` directly. Nothing in the codebase uses `contextvars`.

So there are two ways to deliver context, and we need both:

| Mechanism | Use for | Why |
| --- | --- | --- |
| **Explicit argument** | The 25 in-scope action callbacks, `actions.py`, `job_progress.py`, `supplier_search_gate.py` | We control the call site. Explicit is testable and obvious. |
| **Our own `ContextVar`** | Deep tool calls: `quote_tools._stream_to_user`, `browser_tools`, `job_tools`, `base.py._notify_retry` | Threading an argument through `create_react_agent` → tool would mean changing every tool signature and the agent plumbing. Not worth it. |

The ContextVar is **ours**, set once per run, and settable in tests — which is the
whole difference from `cl.context`. Rejected alternative: `InjectedToolArg` /
`get_runtime()` — viable, but it changes every tool signature and couples tools to
LangChain's injection machinery for no extra benefit.

---

## 3. The abstraction

```python
# includes/chat/context.py
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ActionSpec:
    """Transport-neutral action button."""
    name: str
    label: str
    payload: dict[str, Any] = field(default_factory=dict)
    tooltip: str | None = None


class MessageHandle(Protocol):
    """A message that can still be mutated after sending."""
    id: str
    content: str

    async def stream(self, token: str) -> None: ...
    async def update(self) -> None: ...
    async def remove(self) -> None: ...


@runtime_checkable
class ChatContext(Protocol):
    thread_id: str
    user_email: str
    agent: str                      # "eagle" | "research" | "internal"

    async def say(
        self,
        text: str,
        *,
        actions: list[ActionSpec] | None = None,
        author: str | None = None,
        transient: bool = False,
    ) -> MessageHandle: ...

    async def image(self, path: str, *, name: str) -> None: ...

    async def notify_dashboard(self, command: str, payload: dict | None = None) -> None: ...

    async def rename_thread(self, name: str) -> None: ...

    # Per-run scratch state — replaces cl.user_session
    def get(self, key: str, default: Any = None) -> Any: ...
    def set(self, key: str, value: Any) -> None: ...

    @property
    def cancelled(self) -> bool: ...


_current: contextvars.ContextVar[ChatContext | None] = contextvars.ContextVar(
    "eagle_chat_context", default=None
)


def get_chat_context() -> ChatContext:
    ctx = _current.get()
    if ctx is None:
        raise RuntimeError("No ChatContext bound — call bind_chat_context() first")
    return ctx


def bind_chat_context(ctx: ChatContext) -> contextvars.Token:
    return _current.set(ctx)
```

Implementations:

| Class | Module | Phase |
| --- | --- | --- |
| `ChainlitChatContext` | `includes/chat/context_chainlit.py` | **1** — wraps today's behaviour exactly |
| `TransportChatContext` | `includes/chat/context_transport.py` | 2 — SSE / `assistant_stream` |
| `FakeChatContext` | `tests/conftest.py` | **1** — records calls, no I/O |

`FakeChatContext` supersedes the `fake_cl` fixture from Phase 0 as call sites migrate.

### Scratch-state keys to carry over

From the audit, `cl.user_session` holds exactly these:

| Key | Destination |
| --- | --- |
| `thread_id`, `user_id`, `user`, `chat_profile` | **Promoted to `ChatContext` fields** (`thread_id`, `user_email`, `agent`) |
| `active_graph` | Stays in `app.py` — a Chainlit-lifecycle concern, not business logic |
| `active_msg` | **Promoted to a `ChatContext` field** (`ctx.active_message`), not `ctx.get/set` — see Step 6b |
| `intent_context` | `ctx.get/set` |
| `total_tokens_used` | `ctx.get/set` |
| `pipeline_fixes_{rfq_id}` | `ctx.get/set` |

---

## 4. Deleting the thread-pinning hacks

[includes/chat/rfq_actions.py](includes/chat/rfq_actions.py) lines 25–92 contain four
helpers that exist **only** because `cl.context.session.thread_id` is ambient and
mutable — `on_chat_resume` can overwrite it mid-callback when the user navigates.

| Helper | Lines | Fate |
| --- | --- | --- |
| `_pin_thread()` | 25–45 | **Delete** — `ctx.thread_id` is immutable |
| `_thread_swap()` | 49–65 | **Delete** |
| `_send_pinned()` | 68–77 | **Delete** — becomes `await ctx.say(...)` |
| `_main_pinned()` | 80–92 | **Replace** — see below |

`_main_pinned` currently does `from app import main` and calls it with a synthetic
`cl.Message`. That import direction (`includes/` → `app.py`) is backwards and blocks
everything. Replace with:

```python
# includes/chat/runner.py
async def run_turn(text: str, ctx: ChatContext, *, files: list[dict] | None = None) -> None:
    """Run one agent turn. No Chainlit, no synthetic messages."""
```

`app.py`'s `on_message` becomes a thin adapter that builds a `ChainlitChatContext` and
calls `run_turn`. Action callbacks call `run_turn` directly.

**This is the single highest-value deletion in the phase.** It removes an import cycle,
a whole class of race condition, and ~70 lines of workaround.

---

## 5. Work order

Strictly leaf-first, so each step ships independently and green.

### ~~Step 1 — `includes/chat/context.py` + `ChainlitChatContext` + `FakeChatContext`~~ ✅ · S
- ~~No call sites changed. Unit-test the fake and the Chainlit impl against `fake_cl`.~~
- Built `includes/chat/context.py` (`ActionSpec`, `MessageHandle`, `ChatContext` protocols) plus
  `bind_chat_context` / `reset_chat_context` / `get_chat_context` and two additions the plan
  did not name: `try_get_chat_context()` for the call sites whose current behaviour is a silent
  no-op outside a session (`_stream_to_user`, `_notify_retry`), and a `chat_context(ctx)`
  context manager so binds unwind on exception.
- `includes/chat/context_chainlit.py` holds `ChainlitChatContext` + `ChainlitMessageHandle`.
  `ChainlitChatContext.from_session()` is the only boundary constructor.
- **The thread pinning moved here rather than being deleted outright.** `_send_pinned`'s
  swap-and-restore now lives in `ChainlitChatContext._pinned()`, keyed off an immutable
  `thread_id` captured at construction. Business logic never sees it. The four helpers in
  `rfq_actions.py` still get deleted in Step 6.
- `FakeChatContext` / `FakeMessageHandle` are defined in `tests/conftest.py`, **not** a
  `tests/fakes.py` module: a `tests` package in site-packages shadows any `tests.*` import.
  Exposed as the `chat_ctx`, `bound_chat_ctx`, and `make_chat_ctx` fixtures.
- `tests/chat/conftest.py`'s fake `cl` gained `Image` and `data` — additive only; no Phase 0
  test was modified.
- 38 new tests in `tests/chat/test_context.py` and `tests/chat/test_context_var.py`.
  Suite: 909 passed / 2 skipped (from 869).
- ContextVar propagation confirmed: it crosses `asyncio.create_task` and `asyncio.to_thread`,
  but **not** a raw `threading.Thread`. `middleware.py` uses `loop.create_task`, so it is
  covered; if that ever moves to a bare thread it needs an explicit `ctx`.

### ~~Step 2 — Leaf modules (low risk, builds confidence)~~ ✅ · M
| File | Change |
| --- | --- |
| ~~[includes/chat/supplier_search_gate.py](includes/chat/supplier_search_gate.py)~~ | ~~`build_menu_actions() -> list[ActionSpec]`; `show_search_menu(ctx)`~~ |
| ~~[includes/chat/job_progress.py](includes/chat/job_progress.py)~~ | ~~`monitor_job(..., ctx)` — 4 sites~~ |
| ~~[includes/tools/job_tools.py](includes/tools/job_tools.py)~~ | ~~L52–69 → `get_chat_context().say(..., actions=[...])`~~ |
| ~~[includes/tools/browser_tools.py](includes/tools/browser_tools.py)~~ | ~~L180–192 → `ctx.image(path, name=...)`~~ |
| ~~[includes/agents/base.py](includes/agents/base.py)~~ | ~~L132–136 `_notify_retry` → `ctx.say(..., transient=True)`~~ |
| ~~[includes/chat/middleware.py](includes/chat/middleware.py)~~ | ~~L97–100 `GeminiRetryNotifier` → same~~ |
| ~~[includes/chat/commands.py](includes/chat/commands.py)~~ | ~~L19 → **delete the file** (dropped at parity)~~ — already deleted in `f600998` |

> ~~`middleware.py` and `base.py` fire from **logging handlers / retry paths**, which may
> run on a different task. Confirm the ContextVar propagates; if not, they take an
> explicit `ctx` captured at construction.~~
> **Resolved:** `GeminiRetryNotifier` uses `loop.create_task`, which snapshots the
> context — propagation confirmed by `test_context_var.py`. No explicit `ctx` needed.

- **The ContextVar had to be bound a step early.** Step 3 was supposed to own binding, but
  `job_tools` / `browser_tools` / `base.py` / `middleware.py` all read it from inside a run,
  so they would have been dead on arrival. `app.py:main()` now calls
  `bind_chat_context(ChainlitChatContext.from_session())`. No reset: Chainlit runs each
  `on_message` in its own task, so the binding is already turn-scoped. `runner.py` takes
  this over in Step 3.
- `show_search_menu` takes `ctx=None` and falls back to `get_chat_context()`. The four
  callers in `rfq_actions.py` pass `ctx=_ctx()`, a temporary `ChainlitChatContext.from_session()`
  helper that Step 6 deletes.
- `_notify_retry` and `GeminiRetryNotifier` use `try_get_chat_context()`, preserving today's
  silent no-op when there is no session rather than raising.
- **Also removed an unused `import chainlit` from
  [includes/tools/supplier_search_tools.py](includes/tools/supplier_search_tools.py)** —
  not in the plan's scope table, and never referenced. Free win for the Step 9 CI rule.
- Migrated the ad-hoc `mod.cl` swapping in `tests/test_job_tools.py` to the `bound_chat_ctx`
  fixture — one of the items Phase 0 deferred to this phase.
- `tests/chat/conftest.py`'s `_install_fake_cl` now also patches
  `includes.chat.context_chainlit.cl` with the **same** fake, so `main()` and the context
  share one session store. Without this, 15 Phase 0 stream-loop tests failed on a real
  `cl.context` lookup — a test-harness gap, not a behaviour change, and no Phase 0 test
  file was edited.
- Both 0%-coverage modules now have tests: `tests/chat/test_job_progress.py` (8) and
  `tests/chat/test_supplier_search_gate.py` (16). Suite: 933 passed / 2 skipped (from 909),
  plus 13 browser-agent tests green.

> ⚠️ **Pre-existing bug found, not fixed:** there is no `@cl.action_callback("confirm_run_script")`
> anywhere, so the **Run** button on the `run_script` confirmation does nothing, and
> `monitor_job()` is never called by production code. That is why `job_progress.py` measured
> 0% coverage in Phase 0. Out of scope for a decoupling phase — tracked as **todo.vu #32818**
> (EagleAgent: Admin). Fix once Step 5/6 land the handler registry, and widen the Step 9 CI
> rule to assert every emitted action name has a registered handler.


### ~~Step 3 — `includes/chat/runner.py`~~ ✅ · L
- ~~Extract the stream loop from `on_message` (app.py:701–1290), consuming the pure helpers
  Phase 0 already extracted into `streaming_logic.py`. Emits transport-neutral events.
  `app.py` becomes the adapter. **Phase 0's tests 5.3–5.5 must still pass unchanged** —
  that is the proof this step is behaviour-preserving.~~
- ~~**Also add the per-`thread_id` run lock here**, not in the Phase 2 API layer.~~
- **All 16 Phase 0 stream-loop tests pass unmodified.** `app.py` dropped from 1,289 to ~790
  lines; `main()` is now ~90 lines of adapter.
- `run_turn(text, ctx, *, graph, files, file_metadata, intent_context, dashboard_context,
  on_busy, busy_timeout)`. The `graph` is passed in rather than resolved internally —
  Step 7's registry takes that over.
- The lock lives in a `_RunLock` async context manager wrapping `_run_turn_locked()`, so
  release-on-exception is structural rather than a `finally` that can be edited away.
- **The adapter kept the stop-agent task registry.** `clear_stop` / `register_task` /
  `unregister_task` key off the Chainlit `session_id`, not `thread_id`, so they stay in
  `app.py` around the `run_turn()` call. Inside the loop, `is_stop_requested(session_id)`
  became `ctx.cancelled`.
- **`MessageHandle` grew `author`, a settable `content`, and `save()`.** `save()` holds the
  resilient-persistence fallback (data-layer write when the socket is dead), which is
  Chainlit-specific and therefore belongs in `ChainlitMessageHandle`, not the runner.
- **`active_msg` is now a `MessageHandle`, not a raw `cl.Message`.** That would have silently
  broken `quote_tools._stream_to_user` (`.stream_token()` → `.stream()`), so its conversion
  was pulled forward from Step 4, along with the matching producer in
  `rfq_actions._resume_pipeline_from`. Leaving them inconsistent for a step was the worse option.
- **Mutation-tested the lock**, per the Phase 0 discipline. Disabling the reject branch fails
  1 test; making the lock non-shared fails 3, including the `max_concurrent == 1` assertion.
  Both reverted; suite 941 passed / 2 skipped.
- Not done here: `_main_pinned` and `from app import main` still exist in `rfq_actions.py`.
  They are deleted in Step 6, which is where the callback that uses them gets converted.

**Busy policy as implemented:** `on_message` passes `on_busy="reject"` and answers a rejected
run with "Still working on the previous message — one moment." Action callbacks will pass
`"wait"` when they are converted in Step 6. `/api/stop-agent` never calls `run_turn()`, so it
bypasses the lock automatically.

```python
# includes/chat/runner.py
_run_locks: dict[str, asyncio.Lock] = {}

async def run_turn(
    text: str,
    ctx: ChatContext,
    *,
    files: list[dict] | None = None,
    on_busy: Literal["reject", "wait"] = "reject",
    busy_timeout: float = 120.0,
) -> None:
    lock = _run_locks.setdefault(ctx.thread_id, asyncio.Lock())
    if lock.locked() and on_busy == "reject":
        raise RunInProgress(ctx.thread_id)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=busy_timeout)
    except TimeoutError:
        raise RunInProgress(ctx.thread_id) from None
    try:
        ...
    finally:
        lock.release()
        if not lock.locked() and not _has_waiters(lock):
            _run_locks.pop(ctx.thread_id, None)   # unbounded growth otherwise
```

Rationale: **every** path reaches the graph through `run_turn()` — `on_message`, the 25
in-scope action callbacks, and (from Phase 2) the HTTP endpoints. One lock therefore protects
Chainlit-originated runs *and* new-backend runs against each other. Without it, two
concurrent runs on one `thread_id` corrupt the checkpoint — exactly the failure the
dangling-tool_call repair exists to clean up. This is the precondition for testing both
UIs against the same thread during Phases 2–5.

#### Re-entrancy — resolved, no deadlock risk

Traced: **exactly one callback re-enters the graph** — `on_rfq_find_all_suppliers`
([rfq_actions.py:1077](includes/chat/rfq_actions.py)) via `_main_pinned()`. The other
grep hit (line 44) is a usage example inside `_pin_thread`'s docstring. Nothing invokes
an action callback from *inside* a run, so re-entry is always a **fresh** turn, never a
nested one.

**Therefore the lock does not need to be re-entrant.** Verify this still holds after
Step 6 — a `test_run_lock.py` case asserting `run_turn()` is never called from within
`run_turn()` is cheap insurance.

#### Busy policy — the decision

| Caller | `on_busy` | Why |
| --- | --- | --- |
| `on_message` (user typed) | `"reject"` | The composer is already disabled during a run, so this is a rare edge case. Show "Still working on the previous message…". |
| Action callbacks | `"wait"` | The user clicked a legitimate button; refusing is poor UX. The dashboard already dispatches an `agent-working` CustomEvent ([base.html](templates/base.html#L999)), so the wait is visible. |
| Phase 2 HTTP endpoints | `"reject"` → `409 run_in_progress` | The client can retry or surface it. |
| **Cancel / stop** | **never takes the lock** | `/api/stop-agent` deliberately bypasses `_session_locks` today and must bypass this too. It does not call `run_turn()`, so this is automatic — do not "helpfully" wrap it. |

**Bonus:** this lock **subsumes `_session_locks`** in
[includes/agent_bridge.py](includes/agent_bridge.py) for concurrency purposes. That dict
only exists to stop dashboard actions racing each other's thread pinning — both of which
disappear in this phase. It is deleted in Phase 5 regardless.

> Single-process only. If the web process ever scales beyond one replica this must
> become a Postgres advisory lock. Same constraint as the `run_id` registry and the
> event fan-out — decide the substrate once.

**Behaviour note:** today, two concurrent runs on one thread are *possible* and produce
corruption. Adding the lock changes that to a clean reject-or-wait. That is a deliberate
behaviour change, and the one exception to this phase's "no behaviour change" rule.

### ~~Step 4 — `includes/tools/quote_tools.py`~~ ✅ · M
| Site | Line | Change |
| --- | --- | --- |
| ~~`_stream_to_user`~~ | ~~858–866~~ | ~~`cl.user_session.get("active_msg")` → `get_chat_context()`~~ — done early in Step 3 |
| ~~`manage_rfq` create~~ | ~~829–833~~ | ~~`cl.context.session.thread_id` → `ctx.thread_id`~~ |
| ~~`manage_rfq` post-create~~ | ~~891–899~~ | ~~data-layer rename → `ctx.rename_thread(name)`~~ |
| ~~`_notify_rfq_updated`~~ | ~~851–855~~ | ~~→ `ctx.notify_dashboard("dashboard_refresh")`~~ |
| ~~Local `import chainlit`~~ | ~~13, 663, 829, 889~~ | ~~**remove all four**~~ |

- **`quote_tools.py` now has zero `chainlit` references** — verified by grep, not just the import line.
- `_notify_rfq_updated` / `_notify_agent_working` share a `_notify()` helper that prefers the
  bound context and **falls back to `agent_bridge.notify_dashboard`**. The fallback is needed
  because `supplier_search_tools` reaches these from the RFQ action callbacks, which do not
  bind the ContextVar yet. Delete the fallback in Step 6 once every path binds.
- The create path swapped two silent `try/except Exception: pass` blocks for explicit
  `ctx is not None` guards. Same behaviour outside a session, but a real failure now surfaces
  instead of being swallowed.
- 5 new tests in `tests/tools/test_quote_tools.py` covering thread binding on create, the
  thread rename, the no-context path, and the dashboard notification. Suite: 945 passed / 2 skipped.


### ~~Step 5 — `includes/chat/actions.py`~~ ✅ · M
- ~~`dispatch_action(name, ctx, **kwargs)`. Intent handlers write via `ctx.set("intent_context", …)`.~~
- `ctx` is the second positional parameter and defaults to `get_chat_context()`, so tool-side
  callers (`action_tools.start_new_conversation`) need no change while explicit callers stay explicit.
- Every registered handler now takes `ctx` as its first argument — `dispatch_action` passes it
  positionally, so a handler that forgets it fails loudly rather than silently.
- The permission check reads `ctx.user_email` instead of `cl.user_session.get("user_id")`.
- **`agent_bridge.dispatch_action` had to be updated in the same step.** Its custom-action
  fallback path calls `dispatch_custom_action(name, **payload)`; without an explicit ctx it
  would have raised `RuntimeError: No ChatContext bound`, because the bridge never binds the
  ContextVar. It now passes `ChainlitChatContext.from_session()`. Step 8 revisits this properly.
- Migrated the ad-hoc `actions_mod.cl` monkeypatching in `tests/test_actions.py` to the shared
  fixtures — the last of the mocks Phase 0 deferred. Added coverage for the admin-allowed path,
  the ContextVar fallback, and the new-thread id, which the old test did not have.
- Suite: 948 passed / 2 skipped.

> **`import chainlit` now survives in only 4 files:** `app.py`, `context_chainlit.py`,
> `rfq_actions.py` (Step 6) and `agent_bridge.py` (Phase 5). `includes/tools/` and
> `includes/agents/` are completely clean.


### ~~Step 6 — `includes/chat/rfq_actions.py`~~ ✅ · **XL** — the big one
- ~~Delete the 4 pinning helpers. Convert all 21 callbacks to
  `async def on_x(payload: dict, ctx: ChatContext)`. Registration moves to a plain dict
  so `app.py` owns the `@cl.action_callback` decorators.~~
- `_pin_thread`, `_thread_swap`, `_send_pinned`, `_main_pinned` and `_should_stop` are all
  **deleted**. `from app import main` no longer appears anywhere under `includes/` — the
  import cycle is gone.
- `rfq_actions.py` has **zero** Chainlit references outside two docstring mentions.
- `_user_id(payload, ctx)` replaces the repeated
  `payload.get("user_id") or cl.user_session.get("user_id", "unknown")`, preserving the
  payload-wins-then-session-then-`"unknown"` order that Phase 0 pinned.
- `on_rfq_find_all_suppliers` now calls `run_turn(..., on_busy="wait")` directly. No synthetic
  `cl.Message`, no `_main_pinned`.
- **Removed a dead local**: `internal_summary_lines` in `on_rfq_find_suppliers` was built and
  never read.

> ⚠️ **A Phase 0 test file changed — justification.** `tests/chat/test_rfq_action_callbacks.py`
> called handlers as `on_x(action)`; the new signature is `on_x(payload, ctx)`, so the call
> convention and the fixture had to change. **Every assertion is unchanged.** This was
> anticipated by Phase 0 itself — that file's docstring says "Phase 1 converts these to take an
> explicit ChatContext and deletes the thread-pinning helpers". All 16 still pass.

**Also pulled forward, because Step 6 is what made them correct:**
- `_make_chainlit_adapter` **binds** the ContextVar as well as passing `ctx`, so tools reached
  deeper down (`supplier_search_tools` → `_notify_rfq_updated`) see it. Uses the
  `chat_context()` context manager, so the bind unwinds even on exception.
- With that in place, the temporary `agent_bridge.notify_dashboard` fallback added to
  `quote_tools._notify` in Step 4 was **deleted**.

New tests: `tests/chat/test_rfq_action_registry.py` (26) — no name lost against the 21 former
decorators, every handler is an async `(payload, ctx)` callable, handlers are distinct, the
`app.py` adapter loop registers all 21 with Chainlit, and every button the supplier-search menu
emits has a handler. Suite: 974 passed / 2 skipped.

> **`rfq_find_all_suppliers` still has no `rfq_id` guard** — left as-is deliberately, since
> changing it would mean editing a Phase 0 assertion mid-refactor. Tracked as **todo.vu #32822**.


```python
# includes/chat/rfq_actions.py
RFQ_ACTIONS: dict[str, ActionHandler] = {
    "rfq_refresh": on_rfq_refresh,
    "rfq_identify_items": on_rfq_identify_items,
    ...
}
```

```python
# app.py — one loop replaces 21 decorators
for _name, _handler in RFQ_ACTIONS.items():
    cl.action_callback(_name)(_make_chainlit_adapter(_handler))
```

Convert in this order, easiest first:
`rfq_refresh` (150) → `rfq_dismiss` (547) → `rfq_update_supplier` (161) →
`rfq_pipeline_skip_validation` (598) → `rfq_pipeline_retry_validation` (621) →
`rfq_pipeline_fix_part` (558) → the six `rfq_pipeline_*` supplier handlers (871–1004) →
the five `rfq_find_*` / `rfq_add_brand_supplier` handlers (1063–1177) →
`rfq_find_web_suppliers_for_line` (487) → `rfq_group_items` (1004) →
`rfq_find_suppliers` (374) → `rfq_identify_items` (190) →
`rfq_pipeline_web_search` (698) — **last, it is the heaviest**.

Run the Phase 0 callback tests after each conversion.

#### `rfq_find_all_suppliers` — the one graph re-entry

The only callback that re-enters the graph. Today ([rfq_actions.py:1063–1077](includes/chat/rfq_actions.py))
it builds a synthetic `cl.Message` and calls `app.main()` through `_main_pinned()`.
After this step:

```python
async def on_rfq_find_all_suppliers(payload: dict, ctx: ChatContext) -> None:
    rfq_id = payload.get("rfq_id", "???")
    await run_turn(
        f"Find suppliers for all items on {rfq_id}",
        ctx,
        on_busy="wait",          # dashboard-initiated: queue, don't refuse
    )
```

No synthetic message, no `from app import main`, no pinning. Convert it **immediately
before** `rfq_pipeline_web_search` so the two riskiest conversions are adjacent and get
the same scrutiny.

### ~~Step 6b — Concurrent-run output fixes~~ ✅ · S — *added mid-phase*

Not in the original plan. Found by hand-testing Step 6: clicking an RFQ button and then
typing a chat message runs two workers on one thread, and they fought over two pieces of
single-slot shared state.

- **`active_msg` was a shared session key.** `run_turn` set it on entry and nulled it on exit,
  so a second run silently stole and then destroyed the first run's streaming handle — the
  button's later output went nowhere. It is now `ctx.active_message`, an attribute of the
  **context instance**. Each turn and each callback builds its own context, so runs cannot
  clobber each other. This also deletes a session key rather than merely renaming it.
- **The "agent working" badge was single-slot.** Every handler ends with
  `notify_dashboard("agent_done")`, so whichever worker finished first cleared the badge for
  everyone. `notify_dashboard` now reference-counts `agent_working`/`agent_done` per session
  and emits only the 0↔1 transitions. Unbalanced `agent_done` calls cannot drive it negative.
- 10 tests in `tests/chat/test_concurrent_runs.py`. **Mutation-tested both:** disabling the
  ref-count fails 3 tests; reverting `active_message` to the session key fails the
  two-runs-one-thread streaming test. Suite: 984 passed / 2 skipped.

> **Not fixed here:** a chat message still does not interrupt in-flight button work, because
> 20 of the 21 handlers never call `run_turn` and so never take the per-thread lock. The plan's
> busy-policy table assumed all callbacks would reach `run_turn`; they do not. Tracked as
> **todo.vu #32823**, to be done after Phase 1 ships.

### ~~Step 7 — `includes/agents/registry.py`~~ ✅ · S
- ~~Single source of truth replacing logic spread across `@cl.set_chat_profiles` (app.py:237),
  `on_chat_start` (313–315), `on_chat_resume` (411–428) and `embedded.js`.~~
- `AgentSpec` is frozen, with `graph()` reading `includes.graph` **late** — those globals mutate
  after `setup_globals()`, so an eagerly-bound graph would be stale. Pinned by a test.
- `resolve()` accepts a key, a display label, or a legacy name, and falls back to the default —
  matching the old `else: graph()` branches exactly.
- Three if/elif chains on `chat_profile_name` collapsed to `resolve_agent(...).graph()`.
  `_research_graph()` and `_internal_graph()` in `app.py` became dead and were deleted.
- `ChainlitChatContext.from_session()` now maps profile → agent key through `resolve()` instead
  of its own `_PROFILE_TO_AGENT` dict, so the Step 1 duplicate is gone.
- **One subtlety:** the old resume code only rewrote `chat_profile` for the two legacy names.
  Normalising unconditionally would have set a profile where there was none, which changes the
  `find_supplier` intent default in `main()`. The rewrite is guarded so an absent profile
  stays absent.
- 24 tests in `tests/agents/test_registry.py`. Suite: 1008 passed / 2 skipped.

> **Not done:** `embedded.js` and `base.html` still hardcode `'Eagle Agent'` for the
> RFQ-binding check. `allows_rfq_binding` exists on the spec for it, but wiring the frontend
> to the registry needs an endpoint or a template variable — Phase 2 territory.


```python
@dataclass(frozen=True)
class AgentSpec:
    key: str; label: str; description: str; icon: str
    graph_attr: str                  # "graph" | "research_graph" | "internal_graph"
    intents: list[str]
    allows_rfq_binding: bool
    admin_only: bool = False

AGENTS: dict[str, AgentSpec] = {...}
LEGACY_NAMES = {"EagleAgent": "eagle", "System Admin": "eagle"}
```

### ~~Step 8 — `includes/agent_bridge.py`~~ ✅ · M
- ~~`dispatch_action` builds a `ChainlitChatContext` from the resolved session and calls the
  handler from `RFQ_ACTIONS` **directly**, instead of going through
  `config.code.action_callbacks`.~~
- `RFQ_ACTIONS` is consulted **first**; `config.code.action_callbacks` remains as the fallback
  for the lifecycle actions that legitimately still live in `app.py` (`cancel_job`,
  `stop_agent`, `new_conversation`, `cancel_run_script`). Precedence is pinned by a test.
- The handler runs inside `chat_context(ctx)`, so tools below it read the ContextVar. The bind
  unwinds even when the handler raises — also pinned.
- `WebsocketSession` / `init_ws_context` / the `_thread_id` session pinning **stay** (deleted in
  Phase 5), but are now confined to the lookup at the top of the function.

> **Phase 0 tests: fixture changed, assertions untouched.** `test_bridge_dispatch.py`'s
> `fake_chainlit` fixture now also patches `RFQ_ACTIONS` (to `{}` by default, so each test
> declares which path it exercises) and makes its fake `init_ws_context` set the real
> `context_var`, as the production one does. All 9 original tests pass **unmodified**.
> 6 new tests cover the direct-dispatch path.

Suite: 1014 passed / 2 skipped.


### Step 9 — CI rule · S
```python
# tests/test_no_chainlit_imports.py
ALLOWED = {"app.py", "includes/chat/context_chainlit.py",
           "includes/chat/data_layer.py", "includes/chat/local_storage_client.py",
           "includes/agent_bridge.py"}  # bridge removed from this list in Phase 5

def test_no_chainlit_outside_adapter():
    offenders = [p for p in Path(".").rglob("*.py")
                 if str(p) not in ALLOWED and _imports_chainlit(p)]
    assert not offenders, f"chainlit imported outside the adapter layer: {offenders}"
```
Must catch **local imports inside functions** too — `quote_tools.py` has four, and
`base.py` and `agent_bridge.py` one each.

### Step 10 — Ship and bake · S
Deploy to production on Chainlit. Let it run before starting Phase 2.

---

## 6. Testing strategy

Phase 0's tests are the safety net; **they must not be rewritten during Phase 1.** If a
Phase 0 test needs changing, that is evidence of a behaviour change — stop and justify it.

New tests added by this phase:

| Test | Covers |
| --- | --- |
| `tests/chat/test_context.py` | `ChainlitChatContext` maps `say`/`image`/`notify_dashboard`/`rename_thread` onto the right `cl.*` calls; `FakeChatContext` records faithfully |
| `tests/chat/test_context_var.py` | Bind/unbind, nested binds, `RuntimeError` when unbound, propagation into `asyncio.create_task` and into logging-handler threads (the `middleware.py` case) |
| `tests/chat/test_rfq_action_registry.py` | Every name in `RFQ_ACTIONS` resolves; no name lost vs. the 21 decorators |
| `tests/chat/test_run_lock.py` | Second concurrent `run_turn()` on one `thread_id` is rejected under `on_busy="reject"` and queued under `"wait"`; `busy_timeout` raises `RunInProgress`; different `thread_id`s run in parallel; lock released on exception; `_run_locks` does not grow unboundedly; `run_turn()` is never called from within `run_turn()` |
| `tests/agents/test_registry.py` | 3 agents; legacy `"EagleAgent"` / `"System Admin"` → `"eagle"` |
| `tests/test_no_chainlit_imports.py` | The CI rule |

**Per-callback regression loop.** After each of the 21 conversions:
```
uv run pytest tests/chat/ tests/tools/ -x --timeout=60 -q --no-header
```
Then the full suite before commit.

---

## 7. Risks

| Risk | Mitigation |
| --- | --- |
| **ContextVar doesn't propagate** into a retry callback or logging handler (`base.py`, `middleware.py`) | Test explicitly (`test_context_var.py`). Fall back to explicit `ctx` captured at construction. |
| **21 callbacks is a long grind** — easy to lose focus and let one drift | One commit per callback. Phase 0 tests after each. |
| **`_main_pinned` removal changes re-entry semantics** | Highest-risk deletion in the phase. `rfq_pipeline_web_search` (698) is the main user — convert it **last**, with the most scrutiny. |
| **Behaviour drift disguised as refactor** | The rule that Phase 0 tests are immutable. |
| **The run lock is a real behaviour change** | Concurrent runs on one thread currently corrupt the checkpoint silently; they will now reject or queue per the busy policy. Re-entrancy traced: only `rfq_find_all_suppliers` re-enters, and as a fresh turn — **no deadlock risk**. |
| Merge conflicts with ongoing work | Long-lived branch; rebase often. Steps 1–2 are additive and can land early. |
| `active_msg` handle lifetime | `_stream_to_user` assumes a live message exists. `FakeChatContext` must reproduce the "no active message" case (currently a silent no-op — preserve that). |

---

## 8. Definition of done

- [ ] `includes/chat/context.py` + Chainlit/Fake implementations
- [ ] `includes/chat/runner.py` owns the turn; `app.py` is a thin adapter
- [ ] Per-`thread_id` run lock inside `run_turn()`, covering both Chainlit and (later) HTTP paths
- [ ] All 21 RFQ callbacks converted; `RFQ_ACTIONS` registry in place
- [ ] `_pin_thread` / `_thread_swap` / `_send_pinned` / `_main_pinned` **deleted**
- [ ] `from app import main` no longer appears anywhere under `includes/`
- [ ] `includes/agents/registry.py` is the only place agents are defined
- [ ] `import chainlit` gone from `includes/tools/`, `includes/agents/`, and all of `includes/chat/` except the adapter files
- [ ] `tests/test_no_chainlit_imports.py` green
- [ ] Full suite green; **every Phase 0 test unmodified**
- [ ] Shipped to production and baked

## 9. Gate

> **Did decoupling alone solve enough of the pain?**

A legitimate stopping point. After this phase the codebase is testable and free of
ambient state, but concurrency is still broken and the iframe is still there. Decide
deliberately whether Phase 2 proceeds — the answer may be "not yet", and that is a
valid outcome rather than a failure.
