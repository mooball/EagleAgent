# Phase 0 — Baseline & Parity Contract

> Parent: [plan-chatMigration.prompt.md](plan-chatMigration.prompt.md)
> Status: **PROPOSAL — not approved, not started.**
> Prerequisite phase (dependency bumps, `astream_events` v1 → v2) must land first.

## Goal

Pin down the current behaviour so Phase 1 can refactor without silently breaking it.

Two deliverables:
1. A signed-off **feature-parity checklist** — the definition of "done" for the whole project.
2. **Characterisation tests** covering the untested core.

Phase 0 changes **no behaviour**. The only production code it may touch is
mechanical extraction of pure helpers (see §3), which must be provably no-op.

---

## 1. The problem Phase 0 has to solve first

`on_message` in [app.py](app.py) is a **single ~590-line coroutine** (lines 701–1290)
that mixes Chainlit I/O, LangGraph streaming, and pure decision logic. All the
behaviour worth pinning down — checkpoint repair, the repetition guard, the fallback
chain — is buried inside it and unreachable from a test.

Current state of test coverage for this area:

| File | What it actually covers |
| --- | --- |
| [tests/test_rfq_actions.py](tests/test_rfq_actions.py) | Re-implements classification logic "without requiring Chainlit". Tests a copy, not the code. |
| [tests/test_actions.py](tests/test_actions.py) | Monkeypatches `actions_mod.cl.user_session` / `.Message` (lines 122–145). |
| [tests/test_job_tools.py](tests/test_job_tools.py) | Mocks the whole `cl` module. |
| [tests/test_agent_bridge.py](tests/test_agent_bridge.py) | `notify_dashboard` + `handle_bridge_request`. **Not** `dispatch_action`'s session lookup. |

**Nothing exercises `on_message`, the streaming loop, `on_chat_resume`, or any of the
28 action callbacks.**

So Phase 0 is: *extract the pure logic, then test it.*

---

## 2. Deliverable A — the parity checklist

A checklist file, reviewed and signed off before any Phase 1 code. Derived from
`.chainlit/config.toml` plus observed behaviour. Each row is
**keep / drop / change**, decided explicitly.

### From `.chainlit/config.toml`

| Setting | Value | Decision needed |
| --- | --- | --- |
| `features.unsafe_allow_html` | `true` | **Drop** — only exists for the token-footer `<div>` (app.py:1237). Becomes structured metadata. |
| `features.spontaneous_file_upload.accept` | images, PDF, text, audio, xlsx/xls | Keep. Must be enforced **server-side** after migration. |
| `features.spontaneous_file_upload.max_files` | `20` | Keep |
| `features.spontaneous_file_upload.max_size_mb` | `50` | Keep (note: `document_processing.py` uses `MAX_FILE_SIZE_MB=100` — **reconcile**) |
| `features.auto_tag_thread` | `true` | **Change** — profile moves from thread tag to `threads.metadata.agent` |
| `features.edit_message` | `true` | **Decide** — out of scope for v1? |
| `features.user_message_autoscroll` | `true` | Keep — becomes ours to implement |
| `features.assistant_message_autoscroll` | `true` | Keep — becomes ours to implement |
| `features.latex` | `false` | Drop |
| `features.allow_thread_sharing` | `false` | Drop |
| `features.favorites` | `false` | Drop |
| `features.audio.enabled` | `false` | Drop |
| `project.session_timeout` | `7200` | N/A after migration (stateless) |
| `project.user_session_timeout` | `1296000` | Keep — must stay matched to `SessionMiddleware` `max_age` in [main.py](main.py) |
| `project.transports` | `["websocket"]` | N/A — replaced by SSE. **The Railway-proxy reason it exists still applies to SSE.** |
| `project.allow_origins` | `["*"]` | **Tighten** — same-origin only after migration |

### Behavioural items not in config

- [ ] Thread list: create / rename / delete / resume
- [ ] Thread auto-naming (`"{RFQ} — {customer}"`)
- [ ] Chat profile switcher (3 agents)
- [ ] Composer command buttons per profile (`INTENTS` / `RESEARCH_INTENTS`)
- [ ] Welcome message variants (first-visit × known-name = 4 branches × 3 profiles)
- [ ] Transient "⏳ Using {tool}… (xN)" progress line
- [ ] Token/cost footer
- [ ] Stop button + cooperative cancellation
- [ ] File upload → vision/text extraction
- [ ] Browser screenshot inline image
- [ ] Dark mode; sidebar collapse state
- [ ] Markdown + code block rendering

### Action callbacks — enumerate every one

**28 total: 21 in [includes/chat/rfq_actions.py](includes/chat/rfq_actions.py), 7 in
[app.py](app.py).** "Action buttons work" is not a parity criterion — we could lose
 twenty of these and still tick that box. Each row below is a distinct user-facing
capability and must be individually verified.

The split by **trigger source** matters, because the two groups need different
replacement machinery:

#### A. Dashboard-initiated — 8 actions, 9 call sites

Invoked from `_sendAction()` in [templates/base.html](templates/base.html#L999) →
`POST /api/agent-bridge`. These need the **client-side call path** rebuilt (Phase 5),
not a button-rendering mechanism.

| Action | Handler | base.html | Trigger in UI |
| --- | --- | --- | --- |
| `rfq_update_supplier` | rfq_actions:161 | 1033 | Supplier status change |
| `rfq_identify_items` | rfq_actions:190 | 1065 | "Classify & validate all items" |
| `rfq_identify_items` | rfq_actions:190 | 1079 | Same action, **single line** variant |
| `rfq_find_suppliers` | rfq_actions:374 | 1095 | "Find suppliers" for one line |
| `rfq_group_items` | rfq_actions:1004 | 1116 | "Group items" |
| `rfq_find_all_suppliers` | rfq_actions:1063 | 1122 | "Find suppliers for all items" |
| `rfq_find_previous_suppliers` | rfq_actions:1080 | 1127 | "Search our records" |
| `rfq_find_brand_suppliers` | rfq_actions:1154 | 1132 | "Find brand-linked suppliers" |
| `rfq_find_new_suppliers` | rfq_actions:1130 | 1137 | "Search the web for new suppliers" |

#### B. Chat-emitted — 13 actions

Rendered as `cl.Action` buttons attached to an agent message. These need a
**button-rendering + round-trip mechanism** (Phase 4).

| Action | Line | Purpose |
| --- | --- | --- |
| `rfq_refresh` | 150 | Refresh the dashboard view |
| `rfq_find_web_suppliers_for_line` | 487 | Web search, single line |
| `rfq_dismiss` | 547 | Dismiss a pipeline prompt |
| `rfq_pipeline_fix_part` | 558 | Correct a mis-parsed part number |
| `rfq_pipeline_skip_validation` | 598 | Skip validation, resets the fix counter |
| `rfq_pipeline_retry_validation` | 621 | Retry validation |
| `rfq_pipeline_web_search` | 698 | **Heaviest** — 6 messages, 3 dashboard notifies |
| `rfq_pipeline_previous_suppliers` | 871 | Supplier-search menu option |
| `rfq_pipeline_brand_suppliers` | 898 | Supplier-search menu option |
| `rfq_pipeline_new_domestic` | 925 | Supplier-search menu option (AU) |
| `rfq_pipeline_new_international` | 954 | Supplier-search menu option (intl) |
| `rfq_pipeline_supplier_search_done` | 983 | Close the supplier-search menu |
| `rfq_add_brand_supplier` | 1104 | Add a brand-linked supplier |

The five `rfq_pipeline_*` supplier options plus `_done` are built by
[includes/chat/supplier_search_gate.py](includes/chat/supplier_search_gate.py) — verify
them as one menu, not six independent buttons.

#### C. System actions — 7, all in [app.py](app.py)

| Action | Line | Notes |
| --- | --- | --- |
| `new_conversation` | 605 | |
| `delete_all_data` | 611 | Opens the confirmation prompt |
| `confirm_delete_all` | 617 | **Destructive** — wipes all user data |
| `cancel_delete_all` | 636 | |
| `cancel_run_script` | 645 | |
| `cancel_job` | 655 | |
| `stop_agent` | 672 | Also reachable via `/api/stop-agent`, which **bypasses the bridge lock** |

#### Decisions this list forces

- Is any of these dead? `rfq_dismiss` and `rfq_refresh` look vestigial — **check usage
  before porting**, and drop rather than migrate if unused.
- `rfq_identify_items` serves two distinct UI affordances (all-items vs single-line).
  Confirm the payload shapes differ and both are covered.
- Group B is the only reason a generic "action button" rendering mechanism is needed at
  all. If it shrank, the Phase 4 surface shrinks with it.

**Gate:** the user signs this off. Anything not on it is out of scope.

---

## 3. Deliverable B — make the core testable (behaviour-preserving)

Extract pure functions out of `on_message` **without changing any logic**. Each
extraction is mechanical: cut the block, give it arguments, call it from the same place.

Proposed new module: `includes/chat/streaming_logic.py`

| Function | Extracted from | Signature |
| --- | --- | --- |
| `plan_checkpoint_repair()` | app.py:840–898 | `(messages: list[BaseMessage]) -> RepairPlan` |
| `detect_repetition()` | app.py:952–962 | `(buffer: list[str]) -> bool` |
| `extract_ai_text()` | app.py:101 (already a function) | move as-is |
| `extract_model_end_text()` | app.py:1006–1049 | `(output: Any) -> str` |
| `build_token_footer()` | app.py:~1230 | `(prompt: int, completion: int, total: int, elapsed: float) -> str` |
| `plan_resume_backfill()` | app.py:451–483 | `(ckpt_messages, existing_steps) -> list[AIMessage]` |
| `select_graph()` | app.py:313–315, 422–428 | `(profile: str \| None) -> str` (returns agent key, not the graph) |
| `normalise_profile_name()` | app.py:411–419 | `(name: str \| None) -> str` |

`RepairPlan` is a small dataclass so the ≤2 / ≥3 branch is *data*, not control flow:

```python
@dataclass(frozen=True)
class RepairPlan:
    strategy: Literal["none", "inject", "remove"]
    dangling: list[dict]          # tool_calls with no ToolMessage
    corrupt_message_ids: list[str]  # only for strategy == "remove"
```

> **Rule for this phase:** if an extraction requires changing an `if`, a threshold, or
> an ordering, **stop** — that belongs in Phase 1. Extractions must be pure cut-and-lift.

---

## 4. Shared test double

Eight test files currently roll their own Chainlit mock. Consolidate into
[tests/conftest.py](tests/conftest.py) alongside the existing fixtures
(`test_postgres_pool`, `test_checkpointer`, `test_store`, `stub_chat_model`,
`local_storage_client`, `test_thread_id`).

```python
# tests/conftest.py

class FakeMessage:
    """Records everything instead of hitting a socket."""
    def __init__(self, content="", author=None, elements=None, actions=None):
        self.id = str(uuid.uuid4())
        self.content = content
        self.author = author
        self.elements = elements or []
        self.actions = actions or []
        self.sent = self.updated = self.removed = False
        self.tokens: list[str] = []
        self.fail_on_update = False          # for the resilient-persist test

    async def send(self):   self.sent = True; return self
    async def update(self):
        if self.fail_on_update:
            raise RuntimeError("socket closed")
        self.updated = True
    async def remove(self):  self.removed = True
    async def stream_token(self, tok):
        self.tokens.append(tok); self.content += tok


@pytest.fixture
def fake_cl(monkeypatch):
    """Patch `cl` in every module that imports it. Returns a recorder."""
    rec = SimpleNamespace(messages=[], window_messages=[], session={}, commands=[])

    def _message(**kw):
        m = FakeMessage(**kw); rec.messages.append(m); return m

    for modname in (
        "app",
        "includes.chat.rfq_actions",
        "includes.chat.actions",
        "includes.chat.job_progress",
        "includes.chat.supplier_search_gate",
        "includes.chat.middleware",
        "includes.tools.quote_tools",
        "includes.tools.browser_tools",
        "includes.tools.job_tools",
    ):
        ...  # monkeypatch.setattr(f"{modname}.cl", stub, raising=False)
    return rec
```

**Note:** several modules import `chainlit` *locally inside functions*
([includes/tools/quote_tools.py](includes/tools/quote_tools.py) lines 13/663/829/889,
[includes/agents/base.py](includes/agents/base.py) line 132,
[includes/agent_bridge.py](includes/agent_bridge.py) line 129). Those need
`sys.modules["chainlit"]` patched rather than a module attribute — worth handling once
in the fixture rather than per test.

---

## 5. The tests

### 5.1 Checkpoint repair — `tests/chat/test_streaming_logic.py`

Target: app.py:840–898. Thresholds: `<= 2` → inject; `> 2` → remove.

| Case | Input | Expected |
| --- | --- | --- |
| Clean | all `tool_calls` have matching `ToolMessage` | `strategy="none"` |
| 1 dangling | 1 orphan | `strategy="inject"`, 1 synthetic `ToolMessage` |
| **2 dangling** | 2 orphans | `strategy="inject"`, 2 synthetic — **boundary** |
| **3 dangling** | 3 orphans | `strategy="remove"` — **boundary** |
| 3 across 2 AIMessages | 2+1 | `strategy="remove"`, both message ids listed |
| Orphan with no `.id` | AIMessage lacking id | excluded from `corrupt_message_ids` (app.py:872 filters on `if m.id`) |

Assert the synthetic content string exactly:
`"[Error: previous operation was interrupted. Please retry if needed.]"`

### 5.2 Repetition guard

Target: app.py:952–962. Four conditions must **all** hold, so test each boundary:

| Case | Buffer | Expect |
| --- | --- | --- |
| Below chunk threshold | 50 chunks, repeating | `False` (needs `> 50`) |
| 51 chunks, tail ≤ 60 chars | short tail | `False` |
| 51 chunks, tail > 60, snippet repeats 3× | | `False` (needs `>= 4`) |
| 51 chunks, tail > 60, snippet repeats 4× | | `True` |
| Long non-repeating prose | | `False` — **guard against false positives** |

The false-positive case matters most: a wrongly-triggered guard truncates a legitimate
answer, which is worse than the bug it prevents.

### 5.3 Buffer discard on tool start

Target: app.py:968. Currently only reachable through the loop, so this one needs the
loop driven with a fake event sequence:

```python
events = [
    {"event": "on_chat_model_stream", "data": {"chunk": chunk("thinking about it")}},
    {"event": "on_tool_start",        "name": "search_products", "data": {}},
    {"event": "on_chat_model_stream", "data": {"chunk": chunk("the real answer")}},
]
# assert final rendered content == "the real answer"  (reasoning discarded)
```

Also assert the inverse: **no** `on_tool_start` → buffer is flushed, not dropped.

### 5.4 Fallback chain

Target: app.py:1124 / 1131 / 1140. Assert precedence, not just presence:

| Scenario | Expected source |
| --- | --- |
| Buffer non-empty | buffer (fallback 1) |
| Buffer empty, `last_ai_text` set | `last_ai_text` (fallback 2) |
| Both empty, checkpoint has a message | `aget_state()` last message (fallback 3) |
| All three empty | empty message, no crash |

### 5.5 Resilient persistence

Target: app.py:1171–1206. Set `FakeMessage.fail_on_update = True`, assert
`data_layer.create_step()` is called with a `StepDict` whose `type` is
`"assistant_message"` and whose `output` matches the message content. Assert the
`if _dl and msg.content.strip()` guard (app.py:1179) — empty content must **not** write.

### 5.6 Resume backfill

Target: app.py:451–508.

| `len(ai_messages)` | `len(existing_assistant_steps)` | gap | backfilled |
| --- | --- | --- | --- |
| 3 | 3 | 0 | none |
| 3 | 1 | 2 | last 2 |
| 1 | 3 | −2 | none (`gap > 0` guard) |

Assert `metadata["recovered_from_checkpoint"] is True` on every written step.

### 5.7 Token accounting + footer

Accumulate `usage_metadata` across multiple `on_chat_model_end` events; assert the
running totals and the footer string. **This test is deliberately written against the
rendered values, not the HTML**, so it survives the move to structured metadata.

### 5.8 Attachments

Extend [tests/test_file_attachments.py](tests/test_file_attachments.py) /
[tests/test_document_processing.py](tests/test_document_processing.py):
PDF → text + rendered pages; xlsx → dataframe text; image → base64 part; oversize →
rejected; unknown MIME → handled. Assert the "re-attach elements to a new `cl.Message`"
persistence trick (app.py:754–757) records the elements.

### 5.9 Action callbacks — representative sample

**28 total** (21 in [includes/chat/rfq_actions.py](includes/chat/rfq_actions.py),
7 in [app.py](app.py)). Don't test all of them; pick 8 covering each distinct shape:

| Callback | Line | Shape being covered |
| --- | --- | --- |
| `rfq_refresh` | rfq_actions:150 | Trivial — `notify_dashboard` only |
| `rfq_identify_items` | rfq_actions:190 | Long-running + `_pin_thread` + 4 messages |
| `rfq_find_suppliers` | rfq_actions:374 | Emits `cl.Action` buttons |
| `rfq_pipeline_fix_part` | rfq_actions:558 | Read/write `pipeline_fixes_{rfq_id}` counter |
| `rfq_pipeline_web_search` | rfq_actions:698 | The heaviest — 6 messages, 3 `notify_dashboard` |
| `rfq_pipeline_skip_validation` | rfq_actions:598 | Counter reset |
| `stop_agent` | app.py:672 | Cancellation |
| `confirm_delete_all` | app.py:617 | **Destructive** — must be pinned |

For each: assert the messages sent, the session keys touched, and the
`notify_dashboard` calls — *not* the exact prose.

### 5.10 Bridge dispatch (currently untested)

[tests/test_agent_bridge.py](tests/test_agent_bridge.py) covers `handle_bridge_request`
but not `dispatch_action`. Add: session lookup failure, thread pinning
(`_thread_id` in payload), lock serialisation, and `/api/stop-agent` bypassing the lock.

---

## 6. Test layout

```
tests/
  conftest.py                       ← add FakeMessage + fake_cl fixture
  chat/
    __init__.py
    test_streaming_logic.py         ← 5.1, 5.2, 5.7  (pure, fast)
    test_stream_loop.py             ← 5.3, 5.4, 5.5  (driven fake event stream)
    test_resume.py                  ← 5.6
    test_rfq_action_callbacks.py    ← 5.9
  test_agent_bridge.py              ← extend with 5.10
  test_file_attachments.py          ← extend with 5.8
```

Matches the existing `tests/agents/` + `tests/tools/` convention. Run with the existing
task: `uv run pytest tests/ -x --timeout=60 -q --no-header --ignore=tests/agents/test_browser_agent.py`

---

## 7. Production snapshot

Recorded 2026-08-16 (read-only).

| Table / query | Count |
| --- | ---: |
| `threads` | 2,289 |
| `steps` | 39,013 |
| `steps` where `type = 'assistant_message'` | 20,031 |
| `elements` | 1,201 |
| `users` | 10 |
| `feedbacks` | 5 |
| `rfq_threads` | 1,487 |
| `steps` with `recovered_from_checkpoint` | 3 |
| `threads` with a non-empty `tags` | 1,691 |
| `rfqs` with a non-null `thread_id` | **2** |

### What this changes

- **`RFQ.thread_id` is effectively vestigial.** Two rows, against 1,487 in
  `rfq_threads`. The parent plan treats both as load-bearing; in practice only the
  junction table matters. Phase 6 can almost certainly drop the column, and the
  Q5 answer should not spend effort preserving it.
- **The tag → `threads.metadata.agent` migration touches 1,691 rows.** Small enough
  to do in one statement, but no longer hypothetical.
- **`feedbacks` has 5 rows, not zero.** Open question 3 in the parent plan assumed it
  was unused. It is *nearly* unused — 5 ratings from 10 users — so dropping it is
  defensible, but that should be a decision rather than an assumption.
- **Checkpoint reconciliation has fired 3 times in production.** Rare but real, which
  justifies keeping the logic through the migration rather than dropping it as
  speculative.
- **39,013 steps** sets the scale for any Phase 6 read-side transform (HTML footer
  sanitisation in particular). Not large, but not a no-op either.

---

## 8. Definition of done

- [ ] Parity checklist written and **signed off by the user**
- [x] `includes/chat/streaming_logic.py` extracted; `app.py` calls into it; **no behaviour change**
- [x] Test fakes in `tests/chat/conftest.py` (scoped to this directory rather than global)
- [x] Tests written and green — 94 in `tests/chat/`, suite at 869 passed / 2 skipped (from 775)
- [x] Full suite green
- [x] Prod snapshot recorded above
- [ ] The 8 ad-hoc `cl` mocks in existing tests migrated to the shared fakes *(deferred to Phase 1, when ChatContext lands)*
- [ ] Coverage measured on `app.py` + `includes/chat/` as a baseline

### What was built

| File | Contents |
| --- | --- |
| `includes/chat/streaming_logic.py` | `plan_checkpoint_repair`, `plan_resume_backfill`, `detect_repetition`, `extract_ai_text`, `extract_chunk_texts` |
| `tests/chat/test_streaming_logic.py` | 53 — repair/backfill/repetition/chunk parsing, plus a differential suite vs. verbatim copies of the original inline code |
| `tests/chat/test_stream_loop.py` | 16 — `main()` driven over scripted `astream_events`, covering buffer discard, fallback chain, token accounting, resilient persistence |
| `tests/chat/test_bridge_dispatch.py` | 9 — session lookup, thread pinning, error handling, per-session locking |
| `tests/chat/test_rfq_action_callbacks.py` | 16 — 5 callbacks, one per distinct shape |
| `tests/chat/conftest.py` | `FakeMessage`, `FakeGraph`, event builders, `patch_cl`, `fake_data_layer` |

### Behaviours pinned as characterisation, not endorsement

Flagged for a decision during Phase 1 rather than changed here:

- `plan_resume_backfill` raises `AttributeError` on a step with an explicit `None`
  output; the caller swallows it, silently skipping backfill for that thread.
- `rfq_find_all_suppliers` has no `rfq_id` guard, unlike its siblings, so a missing
  id sends the agent a prompt containing `"???"`.
- `rfq_update_supplier` attributes the change to the literal string `"unknown"` when
  no `user_id` is in session.
- The repetition guard's window is 40 **chunks**, so the amount of text examined
  varies ~6× with provider chunk size.

## 9. Gate

> **Is the parity checklist small enough to be finishable, and do the tests actually
> fail when the behaviour is broken?**

The second half is **verified**. Four mutations were run, each turning the expected
tests red and green again on restore:

| Mutation | Caught by |
| --- | --- |
| `_MAX_DANGLING_TO_PATCH` 2 → 3 | 3 repair tests |
| `_stream_buffer.clear()` on `on_tool_start` → no-op | `test_text_before_tool_call_is_discarded` |
| `async with lock:` → `if True:` | `test_actions_on_one_session_do_not_interleave` |
| `skip_validation` resume stage `"group"` → `"validate"` | `test_skip_validation_resumes_at_the_group_stage` |

One mutation initially **escaped**: `gap <= 0` → `gap == 0` in `plan_resume_backfill`.
The test used 1 checkpoint message against 3 steps, where the buggy slice comes out
empty anyway, so it was green for the wrong reason. Resized to 3 against 4, where the
faulty guard returns two already-rendered messages, it is caught. Worth remembering
that a passing characterisation test proves nothing until a mutation has been run
against it.
