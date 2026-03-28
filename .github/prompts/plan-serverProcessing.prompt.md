# Server-Side Script Processing

**Goal**: Allow admins to invoke long-running scripts (embedding updates, imports, etc.) from the Chainlit chat UI, with proper background execution and progress tracking that works reliably on Railway.

**Depends on**: `plan-actionButtons` (action registry, role-based guards, LangGraph tool pattern).

---

## Phase 1 — Core Infrastructure ✅

### ~~1. Create a background job runner module (`includes/job_runner.py`)~~ ✅
- ~~Build an async job runner using `asyncio.create_subprocess_exec` to spawn scripts as child processes without blocking the Chainlit event loop.~~
- ~~Track jobs in an in-memory dict keyed by job ID (UUID): `{id, script_name, status, started_at, finished_at, pid, output_tail, error}`.~~
- ~~Statuses: `running`, `completed`, `failed`, `cancelled`.~~
- ~~Capture stdout/stderr asynchronously (stream into a bounded deque or ring buffer, ~last 200 lines).~~
- ~~On completion, update status and store exit code.~~
- ~~Add a reaper task (`asyncio.create_task`) that monitors running subprocesses and updates status when they finish.~~
- ~~Guard against duplicate runs: reject if the same script is already `running`.~~
- ~~**Why in-memory, not DB?** Jobs are transient (tied to the process lifetime). If Railway restarts, running jobs are dead anyway. Keeps it simple — no migrations needed. Can upgrade to DB-backed later if needed.~~

### ~~2. Register job runner in app.py startup~~ ✅
- ~~Instantiate the `JobRunner` in `setup_globals()` and store as a module-level singleton.~~
- ~~Start the reaper background task.~~
- ~~Add a shutdown hook to kill any running child processes on app teardown (Chainlit `@cl.on_stop` or `atexit`).~~
- Wired up via `@cl.on_stop` shutdown hook.

### ~~3. Define a script registry (`config/scripts.yaml` or dict in settings)~~ ✅
- ~~Map human-friendly names to actual commands, e.g.:~~
  ```yaml
  update_product_embeddings:
    command: ["uv", "run", "python", "-m", "scripts.update_product_embeddings"]
    description: "Regenerate missing product vector embeddings"
    args_allowed: ["--production"]
  update_supplier_embeddings:
    command: ["uv", "run", "python", "-m", "scripts.update_supplier_embeddings"]
    description: "Regenerate missing supplier vector embeddings"
    args_allowed: ["--production"]
  ```
- ~~Validates that only registered scripts can be invoked (no arbitrary command injection).~~
- ~~Easy to extend: just add an entry when a new script is created.~~
- Implemented as Python dict in `config/scripts.py` (6 scripts registered). Includes `validate_args()` for allowlist enforcement.

---

## ~~Phase 2 — Chainlit Integration~~ ✅

### ~~4. Register job management as admin-only LangGraph tools~~ ✅
- ~~Register `run_script`, `list_jobs`, `get_job_status`, `cancel_job` as LangGraph tools, **admin-only** (uses the action registry pattern from `plan-actionButtons`).~~
- ~~Admins interact naturally: "update the product embeddings", "what jobs are running?", "cancel that job".~~
- ~~Tools call the `JobRunner` from #1.~~
- ~~The `run_script` tool reads from the script registry (#3) — only registered scripts can be invoked.~~
- ~~`list_jobs` returns a formatted table of all jobs (running + recent completed/failed).~~
- ~~These tools are also listed by the `list_available_actions` tool from `plan-actionButtons`.~~
- ~~System prompt additions: when an admin asks about available scripts/jobs/tasks, the agent responds with the script registry contents.~~
- Implemented in `includes/tools/job_tools.py` — factory `create_job_tools(runner)` returns 5 tools: `run_script`, `list_scripts`, `list_jobs`, `get_job_status`, `cancel_job`. Also added `list_scripts` to show admin the registry. Wired into `GeneralAgent` via `job_runner` constructor param. All 5 tool names in `ADMIN_ONLY_TOOLS`. Script awareness added to system prompt via `_build_script_awareness()` in prompts.py (admin-only section listing registered scripts).

### ~~5. Send progress updates to the chat thread~~ ✅
- ~~When a job starts, send a Chainlit message: "Started `update_product_embeddings` (job `abc123`)".~~
- ~~Attach a **Cancel** action button (`@cl.action_callback`) to the start message so the admin can click to cancel.~~
- ~~Optionally stream periodic progress: every N seconds (e.g. 30s), post an update message with the latest output lines. Use `asyncio.create_task` with a loop that checks if the job is still running.~~
- ~~On completion, send a final message: "Completed in 12m 34s" or "Failed (exit code 1) — last output: ...".~~
- ~~Progress messages should be sent to the **thread that initiated the job** (store `thread_id` with the job).~~
- Implemented in `includes/job_progress.py` — `monitor_job(runner, job)` coroutine launched via `asyncio.create_task` from `run_script` tool. Posts start message with Cancel action button, periodic updates every 30s, and completion/failure/cancelled messages. `@cl.action_callback("cancel_job")` added to app.py. 15 tests in `tests/test_job_tools.py`.

---

## ~~Phase 3 — Railway / Production Concerns~~ ✅

### ~~6. Ensure Railway doesn't kill long-running processes~~ ✅
- ~~Railway containers run persistently (not serverless/lambda) — child processes spawned from the main Chainlit process will survive as long as the container is alive.~~
- ~~**No special background job infrastructure needed** (no Celery, no Redis, no worker dyno). `asyncio.create_subprocess_exec` is sufficient for this use case.~~
- ~~Key risks and mitigations:~~
  - ~~**Deploy during a job**: Railway restarts the container → job dies. Mitigation: `list_jobs` tool shows running jobs so admin can check before deploying. Document this.~~
  - ~~**OOM**: Embedding scripts use bounded memory (batch processing, 100 at a time). Should be fine within Railway's container limits.~~
  - ~~**Zombie processes**: The reaper task (from #1) monitors child PIDs and cleans up.~~
- ~~Add `SIGTERM` signal handler in `job_runner.py` to gracefully terminate children when the container shuts down.~~
- Implemented: `JobRunner.start()` registers `SIGTERM` and `SIGINT` handlers via `loop.add_signal_handler()`. On signal, `_handle_signal()` schedules `shutdown()` which terminates all running children. Reaper loop already handles zombie detection. 2 tests added for signal handling.

### ~~7. Add `--production` flag handling~~ DISCARDED
- ~~Both embedding scripts already accept `--production` to switch database URLs.~~
- ~~The script registry (#3) defines which args are allowed per script.~~
- ~~When running on Railway, `PROD_DATABASE_URL` is set in the environment, so scripts should pick it up automatically — but verify and document.~~
- ~~Consider: should scripts invoked from the Railway-hosted app always use the production DB? If so, auto-inject `--production` when `PROD_DATABASE_URL` is set in the environment.~~
- DISCARDED: The `--production` flag is only meaningful when running scripts manually from a local machine to point at the remote DB. When invoked from the chat UI, scripts always run against the local database — which on production IS the production database. Removed `--production` from all `args_allowed` entries in the script registry.

---

## ~~Phase 4 — Testing & Documentation~~ ✅

### ~~8. Write tests for the job runner~~ ✅
- ~~Unit tests for `JobRunner`: start job, track status, cancel, duplicate rejection, output capture.~~
- ~~Use a simple test script (e.g. `sleep 2 && echo done`) as the subprocess target.~~
- ~~Test admin permission checks on LangGraph tools.~~
- ~~Test script registry validation (reject unregistered scripts).~~
- Implemented in `tests/test_job_runner.py` (20 tests): lifecycle, run_script (success, failure, unknown, duplicate, thread_id), output capture, cancel (running, unknown, finished), get_job/list_jobs, script registry validation (6 tests). Uses real subprocesses via `sys.executable -c` with monkeypatched test script entries. Also `tests/test_job_tools.py` (19 tests) covers LangGraph tool wrappers, prompt awareness, and signal handling.

### ~~9. Document the system~~ ✅
- ~~Add `docs/SERVER_SCRIPTS.md` covering: how to add a new script, how to invoke from chat, how to monitor, Railway deployment notes.~~
- ~~Update `README.md` with a brief mention of admin script execution.~~
- ~~Update `copilot-instructions.md` with the new module and patterns.~~
- Created `docs/SERVER_SCRIPTS.md` with: how it works, available scripts table, chat commands, adding a new script, architecture/security overview, Railway notes. Updated `README.md` docs section. Updated `copilot-instructions.md` with project structure (`config/scripts.py`, `job_runner.py`, `job_progress.py`, `job_tools.py`), new "Server-Side Scripts" section with module descriptions and "how to add a script" guide.

---

## Recommended Execution Order

1. **#3** Script registry — define what can be run
2. **#1** Job runner module — core async subprocess management
3. **#2** Wire into app.py startup
4. **#4** LangGraph admin tools — `run_script`, `list_jobs`, `get_job_status`, `cancel_job`
5. **#5** Progress updates and cancel action buttons
6. **#8** Tests
7. **#6** Railway verification and signal handling
8. **#7** Production flag handling
9. **#9** Documentation

---

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Job state storage | In-memory dict | Jobs are process-scoped; DB adds complexity for no benefit here |
| Execution method | `asyncio.create_subprocess_exec` | Non-blocking, captures output, works with existing async architecture |
| Entry point | LangGraph tools + cancel action button | Natural language; builds on action buttons plan |
| Script allowlist | YAML registry | Prevents command injection; self-documenting; easy to extend |
| Admin gate | Existing `ADMIN_EMAILS` + action registry role filtering | Consistent with action buttons plan |
| Worker infrastructure | None (no Celery/Redis) | Overkill for single-container Railway deploy with <10 concurrent scripts |
