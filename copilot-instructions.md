# ⚠️ AI BEHAVIOR RULES — READ FIRST ⚠️

**CRITICAL: These rules override all other instincts. Violating them is unacceptable.**

## 1. Propose, Don't Implement
**NEVER write code or edit files without explicit user approval.**
When you identify a bug or feature:
1. Investigate and diagnose the root cause
2. Present your findings and proposed fix
3. WAIT for "yes", "go ahead", "implement that", or similar explicit approval
4. Only THEN make changes

## 2. Never Commit Without Approval
**NEVER commit changes without explicit approval.** The user always wants to test changes locally before they're committed. Even after receiving approval to write code, wait for a separate explicit instruction to commit (e.g., "please commit", "commit now", "commit this"). Do not commit speculatively.

## 3. Never Modify Production Data Without Approval
Do not run INSERT/UPDATE/DELETE on the production database without explicit approval.
Stop at diagnosis and ask. Read-only queries are fine.

## 4. Preline UI Adoption Prompts
**When adding or modifying UI, proactively flag Preline adoption opportunities.**
New UI work should use Preline components by default. When a task touches existing UI,
tell the user if it would be a good moment to migrate that area to Preline —
even if it adds a little effort — and let them decide. See "Frontend / UI" below.

---

# Copilot Instructions for EagleAgent

## Language & Tooling
- Python 3.12, managed with `uv` (no `pip` or `venv` commands).
- Use `~=` (compatible-release) pinning for all dependencies in `pyproject.toml`.
- Use type hints on functions, class attributes, and public APIs.
- Prefer standard library over extra deps when reasonable.

## Project Structure

```
main.py                    # FastAPI ASGI entry point — Google OAuth, session middleware, dashboard + chat UI
config/
  settings.py              # Non-secret configuration (Config class, env overrides)
  scripts.py               # Script registry — allowlist of runnable server-side scripts
  mcp_servers.yaml         # MCP server definitions
includes/
  agents/                  # Multi-agent system
    __init__.py             # Convenience exports (BaseSubAgent, GeneralAgent, Supervisor, etc.)
    base.py                 # BaseSubAgent ABC — contract all sub-agents must follow
    supervisor.py           # Supervisor node — hybrid rule-based + LLM routing
    general_agent.py        # GeneralAgent — general conversation, tools, MCP
    procurement_agent.py    # ProcurementAgent — supplier/product lookup tools
    research_agent.py       # ResearchAgent — Google Search grounding, optional RFQ tools
    sysadmin_agent.py       # SysAdminAgent — admin script/job management
    browser_agent.py        # BrowserAgent — web automation (disabled in main graph)
    registry.py             # AGENTS — the single definition of each agent
  tools/                   # Tool definitions
    browser_tools.py        # Headless browser automation
    user_profile.py         # User profile management tools (remember/get/forget)
    action_tools.py         # Action button tools for LangGraph
    job_tools.py            # Script execution tools (admin-only)
    product_tools.py        # Product/supplier database search tools
    quote_tools.py          # RFQ/quote workflow tools
  chat/                    # Chat-transport modules (all transport-neutral)
    context.py              # ChatContext protocol + ContextVar — the transport boundary
    context_sse.py          # SseChatContext — queue-backed implementation over SSE
    transcript.py           # Thread/step/element storage (app-owned async engine)
    runner.py               # run_turn() — owns the agent turn and the per-thread run lock
    actions.py              # Action registry and dispatcher
    rfq_actions.py          # RFQ_ACTIONS — (payload, ctx) handlers for dashboard buttons
    streaming_logic.py      # Pure stream-decision helpers (checkpoint repair, repetition guard)
    document_processing.py  # PDF/image/text/audio processing for file attachments
    middleware.py           # OAuth redirect + Gemini retry notifier middleware
    supplier_search_gate.py # Post-classification supplier search prompt
    job_progress.py         # Progress messages for background jobs
  dashboard/               # FastAPI dashboard modules
    routes/                 # Route modules (dashboard views, RFQs, chat UI, API, addon)
    routes/chat_ui.py       # /chat-ui — threads, messages, SSE stream, upload, stop
    context.py              # In-memory store for current dashboard view per user
    database.py             # SQLAlchemy sync session for dashboard read queries
    models.py               # SQLAlchemy ORM models (Supplier, Product, Brand, etc.)
  agent_bridge.py           # Dashboard button → chat thread dispatch; cooperative cancellation
  prompts.py                # System prompt builder — dynamic, role-aware, profile-aware
  job_runner.py             # Async background job runner — subprocess management, reaper, signal handling
  mcp_config.py             # MCP server configuration loader
templates/                  # Jinja2 dashboard templates (base.html, suppliers.html, products.html, etc.)
public/
  vendor/preline/           # Vendored Preline UI components
  avatars/, *.png           # Branding assets
scripts/                    # Admin scripts (import_products, import_suppliers, etc.)
docs/                       # All documentation except README.md
tests/                      # All tests (pytest, pytest-asyncio)
  agents/                   # Agent-specific tests
  chat/                     # Chat-transport tests
  tools/                    # Tool-specific tests
```

**Conventions:**
- Import agents via the package: `from includes.agents import GeneralAgent, Supervisor`
- Import chat modules: `from includes.chat.actions import dispatch_action`
- Import dashboard modules: `from includes.dashboard.models import Product, Supplier`
- Intra-package imports use direct paths to avoid circular imports: `from includes.agents.base import BaseSubAgent`

## Architecture

One FastAPI application serves everything:

1. **`main.py`** — the ASGI entry point. Google OAuth (via `fastapi-sso`), session middleware, dashboard HTML routes, the dashboard context API, `/api/agent-bridge`, and the chat UI router (`/chat-ui`).
2. **Chat UI** (`includes/dashboard/routes/chat_ui.py` + `includes/chat/`) — threads, messages, SSE streaming, attachments, actions. It renders into the dashboard's side panel as a same-document embed: no iframe, no separate chat server.
3. **LangGraph** — the multi-agent orchestration behind every turn.

Business logic never sees a transport. It talks to a `ChatContext` (`includes/chat/context.py`), of which `includes/chat/context_sse.py` is the only implementation. See `docs/CHAT_UI.md`.

The dashboard and chat meet at `includes/agent_bridge.py`: a button click is dispatched into the chat thread bound to that RFQ. If no thread is bound yet, the bridge resolves or creates one first, so an action can never run in an unrelated conversation.

### Agents and intents

Agents are defined once, in `includes/agents/registry.py` (`AGENTS`). A thread remembers which agent handled its last turn (thread metadata), and a composer command routes to whichever agent declares that intent — `_intent_route()` in `chat_ui.py` is the routing table; unknown intents fall back to the default agent.

- **Eagle Agent** (default) — Multi-agent graph: Supervisor → GeneralAgent | ProcurementAgent | ResearchAgent
- **Research Agent** — Standalone research graph (Google Search grounding, no RFQ tools)
- **Internal Agent** — Standalone ProcurementAgent graph (DB-only, no web/research/RFQ tools)

## Multi-Agent Architecture

### Supervisor Pattern
The system uses a **LangGraph StateGraph** with a Supervisor that routes to sub-agents:

```
User → Supervisor → [GeneralAgent | ProcurementAgent | ResearchAgent] → Supervisor → ... → FINISH
```

- **Supervisor** (`includes/agents/supervisor.py`): Hybrid routing — rule-based keyword matching first, LLM structured output (`RouteDecision`) as fallback.
- **Sub-agents** extend `BaseSubAgent` and are called as graph nodes.
- The graph loops: Supervisor → agent → Supervisor, until `next_agent == "FINISH"`.

### BaseSubAgent Contract
All sub-agents must extend `BaseSubAgent` (`includes/agents/base.py`). The base class handles:
- Message trimming (max 30 messages, configurable via `max_messages`)
- System prompt injection
- Model invocation via `create_react_agent`
- Checkpoint cleanup (`RemoveMessage`)

**To add a new agent:**
1. Create `includes/agents/my_agent.py`, extending `BaseSubAgent`.
2. Implement sync hooks (`get_tools`, `get_system_prompt`) or async hooks (`get_tools_async`, `get_system_prompt_async`) — async takes priority if both exist.
3. Add the agent to `includes/agents/__init__.py` exports.
4. Register it as a node in `includes/graph.py`'s `setup_globals()` function.
5. Add it to the `RouteDecision` literal type in `supervisor.py`.
6. Add routing logic in the Supervisor (keyword rules and/or LLM prompt).

### MCP Integration
- MCP servers are defined in `config/mcp_servers.yaml`.
- `GeneralAgent.get_tools_async()` loads MCP tools dynamically via `langchain-mcp-adapters`.
- MCP tool loading is graceful — failures log warnings but don't crash the agent.

## Environment & Configuration

### Configuration Module (`config/settings.py`)
- **Non-secret configuration** (model names, data dirs, database URLs, OAuth domains, admin emails) lives in `config/settings.py`.
- Version-controlled with sensible defaults; overridable via environment variables.
- Import: `from config import config` then `config.YOUR_SETTING`.
- To add a setting: add to the `Config` class with `os.getenv("VAR_NAME", "default")`.

### Secrets
- **Secrets** (API keys, OAuth secrets) go in `.env` (git-ignored), read via `os.getenv()`.
- Keep `.env.example` updated with placeholder values for new secrets.
- Never put secrets in `config/settings.py`.

### Deployment
- **Railway** (Singapore region) via Docker.
- Dockerfile uses non-root `eagleagent` user (uid 1000) with `HEALTHCHECK`.
- Secrets are Railway environment variables; non-secret config is baked into the image via `config/settings.py`.

## Persistence

### PostgreSQL
- **Checkpointer**: `AsyncPostgresSaver` (LangGraph checkpoint persistence across turns).
- **Store**: `AsyncPostgresStore` (cross-thread memory — user profiles, preferences).
- **Chat storage**: `includes/chat/transcript.py` owns the `threads`/`steps`/`elements`/`users` tables over an app-owned async engine (the table names are historical — they were Chainlit's).
- **Migrations**: Alembic (`alembic/versions/`).
- Connection URLs configured in `config/settings.py` (`DATABASE_URL`, `CHECKPOINT_DATABASE_URL`).

### Connecting to Databases

When you need to run ad-hoc SQL queries or inspect data directly, use this pattern:

**Local database:**
```python
from sqlalchemy import create_engine, text
# Local: postgresql+psycopg://postgres:postgres@localhost:5432/eagleagent
e = create_engine("postgresql+psycopg://postgres:postgres@localhost:5432/eagleagent")
```

**Production database (from local machine):**
```python
import os
from sqlalchemy import create_engine, text

# Read PROD_DATABASE_URL from .env (NOT DATABASE_URL — that's local)
with open('.env') as f:
    for line in f:
        if line.strip() and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())

url = os.environ['PROD_DATABASE_URL']
# Convert postgresql:// → postgresql+psycopg:// so SQLAlchemy uses the psycopg driver
if url.startswith('postgresql://'):
    url = 'postgresql+psycopg://' + url[13:]

e = create_engine(url)
with e.connect() as c:
    result = c.execute(text("SELECT id, subject FROM email_tracking WHERE id = 6738"))
    for row in result:
        print(dict(row._mapping))
e.dispose()
```

**Key points:**
- Always use `postgresql+psycopg://` (not plain `postgresql://`) — SQLAlchemy needs the driver specified.
- Production URL is in `.env` as `PROD_DATABASE_URL` (Railway proxy: `shortline.proxy.rlwy.net`).
- Close the engine with `.dispose()` when done — don't leave connections open.
- For async connections (used in the app itself), see `config/settings.py` and the `asyncpg` driver.
- Dashboard routes use `get_session()` from `includes/dashboard/database.py` for sync reads.

### File Attachments
- Uploaded via `POST /chat-ui/upload`, written to `DATA_DIR/attachments/`.
- Served to browser via Starlette `StaticFiles` mount at `/files`.
- Tracked as `elements` rows by `includes/chat/transcript.py`; a row is pending until the message that carries it is sent.
- No cloud storage — files stay on the application host.

## ⚠️ Chat Architecture — keep business logic transport-neutral

**Chainlit has been removed** and the chat UI is in-house. The original migration
decoupled all business logic from the transport, and that separation is what makes
a handler usable from a chat button, a dashboard button, and (soon) an automated
trigger. `tests/test_action_coverage.py` still guards the action registry, but
**nothing automatically enforces transport-neutrality any more** — that is a review
responsibility. Follow these rules on any chat-related work.

### 1. Business logic talks to `ChatContext`, never to a transport

Business logic talks to the user through **`ChatContext`** (`includes/chat/context.py`):

```python
await ctx.say("text", author="EagleAgent", actions=[ActionSpec(...)])
await ctx.image(path, name="Screenshot")
await ctx.notify_dashboard("dashboard_refresh")
await ctx.rename_thread("RFQ-1 — Acme")
ctx.cancelled          # user pressed stop
ctx.active_message     # the message currently streaming, if any
```

Deep tool calls that cannot take an argument use `get_chat_context()` (raises if unbound) or `try_get_chat_context()` (returns `None` — use when the current behaviour is a silent no-op outside a session).

`includes/chat/context_sse.py` is the only implementation. Nothing in
`includes/` (other than that module) should know how a message is delivered.

> If you find yourself needing transport detail, the answer is almost always a
> new method on `ChatContext` instead.

### 2. `includes/` must not import `main` at module scope

`main.py` depends on `includes/`, never the reverse — a cycle there once blocked
the whole refactor. (`agent_bridge.handle_bridge_request` lazily imports
`get_current_user` from `main`; that is a known wart, not a pattern to copy —
prefer passing the user in.)

### 3. Every agent turn goes through `run_turn()`

`includes/chat/runner.py` owns the turn: the stream loop, checkpoint repair, token footer, resilient persistence, and the **per-`thread_id` run lock**. Never call `graph.astream_events(...)` directly — you would bypass all of it and risk corrupting the checkpoint.

- `on_busy="reject"` — user-typed messages.
- `on_busy="wait"` — dashboard-initiated work that should queue.

### 4. Action handlers are `(payload, ctx)` and live in a registry

```python
async def on_my_action(payload: dict, ctx: ChatContext) -> None: ...
RFQ_ACTIONS = {"my_action": on_my_action, ...}   # includes/chat/rfq_actions.py
```

Two registries feed one dispatcher: `RFQ_ACTIONS` (signature `(payload, ctx)`) and
`includes/chat/actions.py` (signature `(ctx, payload=...)`). `_action_handler()` in
`chat_ui.py` resolves either. **Every button you emit must have a handler** —
`tests/test_action_coverage.py` fails otherwise.

Dashboard buttons reach the same handlers through
`agent_bridge.handle_bridge_request` → `chat_ui.dispatch_action_to_thread`, which
routes into the RFQ's bound thread and creates/binds one if it is missing.

### 5. Agents are defined once, in `includes/agents/registry.py`

Adding or renaming an agent means editing `AGENTS` only — not routing, graph
selection, or thread handling. `_intent_route()` in `chat_ui.py` maps a composer
command to whichever agent declares that intent.

### 6. Scratch state is per-*thread*; per-run state belongs on the context

`ctx.get/set` is a per-thread scratch dict, loaded before a turn and flushed after
it. Anything belonging to a single run belongs on the **context object** — as
`active_message` does. The run lock allows only one run per thread, so two runs can
no longer fight over shared state, but the distinction still matters for anything
that must not survive a turn.

## Chat UI (`includes/dashboard/routes/chat_ui.py`)

The chat UI is part of the FastAPI app: threads, messages, SSE streaming,
attachments and actions, all under `/chat-ui`. It renders into the dashboard's side
panel as a same-document embed.

- `_run_task()`: builds an `SseChatContext`, resolves the thread's agent, then calls `run_turn()`.
- `GET /threads/{id}/stream`: drains the run's event queue as SSE (replays from the start on reconnect).
- `dispatch_action_to_thread()`: how a dashboard button or a chat button starts work outside a turn.
- `_active_runs`: per-thread registry of in-flight runs; powers busy checks, `GET /active-runs` and stopping.
- `POST /api/stop-agent` takes a `thread_id` and stops **only** that run — there is deliberately no stop-all.

See `docs/CHAT_UI.md` for the full picture.

## Dashboard (`main.py`, `includes/dashboard/`)

The FastAPI dashboard serves HTML pages for managing suppliers, products, RFQs, and users.

- **Routes** (`includes/dashboard/routes/`): Full-page renders and HTMX partial responses. Uses Jinja2 templates from `templates/`.
- **Context** (`includes/dashboard/context.py`): In-memory store keyed by user email — tracks which dashboard page/entity the user is viewing. Injected into the agent's system prompt so it knows the user's current context.
- **Database** (`includes/dashboard/database.py`): `get_session()` provides SQLAlchemy sync sessions for read queries.
- **Models** (`includes/dashboard/models.py`): SQLAlchemy ORM models — `Supplier`, `Product`, `Brand`, `SupplierBrand`, `Transaction`, etc.
- **Agent Bridge** (`includes/agent_bridge.py`): Bidirectional communication — dashboard can dispatch messages to the agent, agent can notify dashboard to refresh via `cl.send_window_message`.

## Frontend / UI (Jinja2 + HTMX + Alpine + Tailwind v4 + Preline)

- Stack: Jinja2 templates in `templates/`, HTMX partials, Alpine.js 3, Tailwind CSS **v4** (CSS-first config in `input.css`, built with the standalone CLI — **no Node**), and **Preline UI** (vendored in `public/vendor/preline/`, see its `VERSION` file).
- **Preline is the default for all NEW UI work** — especially the chat UI. Use its `data-hs-*` components (dropdowns, modals, overlays, toasts, tooltips, tabs, chat bubbles, etc.) instead of hand-rolling markup + JS.
- **When updating existing UI**, proactively flag Preline adoption opportunities to the user — e.g. "this change would be a good moment to switch this modal to a Preline overlay" — even if it adds a little effort. Let the user decide; do not silently do a big-bang restyle.
- Preline is opt-in per component and additive: its JS only activates elements carrying `data-hs-*` attributes; its CSS variants only emit when a template uses them. HTMX-swapped fragments auto-initialise (MutationObserver), same as Alpine.
- **Never mix Alpine and Preline JS on the same element tree** — a dropdown is either Alpine's or Preline's, not both.
- `@tailwindcss/forms` is intentionally omitted (legacy plugin; the standalone v4 CLI cannot load it vendored) — style form controls with utilities as today.
- Custom component CSS lives at the bottom of `input.css` (`.btn`, `.tip`, etc.).
- Rebuild CSS after any class/template change with the standalone v4 CLI: `tailwindcss -i input.css -o public/tailwind.min.css --minify` (binary at `/tmp/tailwindcss4` locally; the Dockerfile downloads the same version at deploy). `public/tailwind.min.css` is untracked.
- Preline kitchen-sink probe: `/public/probe.html` (with its `@source` line in `input.css` — remove both together when retiring it).
- v4 gotchas: `flex-shrink-0`→`shrink-0`, `outline-none`→`outline-hidden`, `shadow-sm`→`shadow-xs`; bare `ring` is 1px/currentColor; Preflight no longer gives buttons `cursor: pointer` (restored in `input.css` base layer).

## Action Buttons (`includes/chat/actions.py`)

Actions replace the old `/` slash commands with action buttons and LangGraph tools.
- **Registry**: `@register_action(name, label, description, icon, admin_only)` decorator registers a handler. Handlers take `(ctx, **kwargs)`.
- **Dispatcher**: `dispatch_action(name, ctx)` checks the user's role before executing admin-only actions. `ctx` defaults to the bound `ChatContext`.
- **Filtering**: `get_actions_for_user(user_id)` returns actions visible to the given user's role.
- **Discovery**: Users can type `help`, `actions`, `menu`, `commands`, or `show actions` to see buttons mid-conversation.
- **LangGraph tools**: `includes/tools/action_tools.py` exposes `list_available_actions` and `start_new_conversation` so the agent can invoke them via natural language.
- **System prompt**: `build_system_prompt()` dynamically includes a list of available actions based on the user's role.

**To add a new action:**
1. In `includes/chat/actions.py`, add a `@register_action(...)` decorated async handler taking `(ctx, **kwargs)`.
2. That is all — `chat_ui._action_handler()` resolves the name from the registry, and the chat UI renders it as a button whenever you pass `ActionSpec(name="your_action_name", ...)`. There is no per-action decorator to add anywhere.
3. Optionally add a LangGraph tool wrapper in `includes/tools/action_tools.py`.
4. If admin-only, add the tool name to `ADMIN_ONLY_TOOLS` in `includes/graph.py`.

> **RFQ buttons are different** — they go in `RFQ_ACTIONS` in `includes/chat/rfq_actions.py` as `(payload, ctx)` handlers. Same dispatcher, different signature; do not add a decorator per button.

## Prompts (`includes/prompts.py`)
- `build_system_prompt()` is the primary prompt builder — dynamic, role-aware, profile-aware.
- Prompts include user profile context, available tools, current date/time.
- Role-based access: admin users get additional tools; staff get a filtered set.
- Admin emails configured in `config/settings.py` (`ADMIN_EMAILS`).
- `_build_script_awareness()` adds a section for admins listing registered scripts and job management workflow.

## Server-Side Scripts (`config/scripts.py`, `includes/job_runner.py`)

Admin users can run registered scripts from the chat. See `docs/SERVER_SCRIPTS.md` for full details.

- **Script registry** (`config/scripts.py`): Allowlist of runnable scripts with command, description, and allowed args.
- **JobRunner** (`includes/job_runner.py`): Spawns scripts as async subprocesses, tracks status in memory, captures output (200-line ring buffer), reaper polls every 2s, SIGTERM/SIGINT handlers for graceful shutdown.
- **Progress** (`includes/chat/job_progress.py`): Posts chat messages on start (with Cancel button), every 30s, and on completion/failure.
- **LangGraph tools** (`includes/tools/job_tools.py`): `run_script` (confirmation flow), `list_scripts`, `list_jobs`, `get_job_status` (by ID or script name), `cancel_job`. All admin-only.
- **Confirmation flow**: `run_script` tool sends Run/Cancel buttons. ⚠️ **The Run button currently has no handler** — `confirm_run_script` is emitted but never dispatched, so the script never starts (todo.vu #32818). Allow-listed in `tests/test_action_coverage.py::KNOWN_ORPHANS`.

**To add a new script:** Add an entry to `SCRIPT_REGISTRY` in `config/scripts.py`. That's it.

## Testing
- Run tests: `uv run pytest tests/ -v`
- Tests use **mocks and in-memory stores** — no database required.
- `pytest-asyncio` with `asyncio_mode = "auto"` (no manual `@pytest.mark.asyncio` needed for async tests).
- 30-second timeout per test.
- Test structure mirrors source: `tests/agents/`, `tests/tools/`.
- When patching config in tests, use `@patch('includes.agents.general_agent.config')` (patch where it's imported).
- When patching chat modules, use `@patch('includes.chat.actions.config')`.
- When patching dashboard modules, use `@patch('includes.dashboard.routes.config')`.
- See `docs/TESTING.md` for full guide — including **Manual End-to-End Testing
  (Local)**, which covers `scripts/test_rfq_creation.py` (replays the Gmail
  add-on "Create RFQ" flow locally, since the add-on itself posts to prod).
  NetSuite writes are intercepted by default there; `--reset` is standalone and
  stops without creating an RFQ.

## Error Handling & Logging
- Use Python `logging` (not `print`).
- Fail fast on config issues at startup.
- User-facing errors: catch in the handler, send a friendly message through `ctx`, log the technical details.

## Style & Quality
- PEP 8 style, PEP 484 type hints.
- Small composable functions over large monoliths.
- Descriptive names (no single-letter variables except trivial loops).

## Development Prompts & Plans

When asked to create a prompt, plan, or task list, always:

- **Store in** `.github/prompts/` using the naming convention `plan-<descriptiveName>.prompt.md` (camelCase for the descriptive part).
- **Use the standard plan format:**
  - `#` heading with the plan title.
  - Grouped sections by phase (e.g. `## Phase 1 — Core Infrastructure`, `## Phase 2 — Integration`, `## Phase 3 — Polish`).
  - Numbered tasks as `###` subheadings within each phase. Numbering is sequential across phases (not reset per phase).
  - Bullet points under each task describing what needs to be done.

### Marking tasks as complete

**CRITICAL: Never delete content from plan files.** All original bullet points, descriptions, and task details must be preserved. The plan is a living record of what was planned and what was done.

- **Mark the task heading** with strikethrough and a ✅:
  ```
  ### ~~1. Task description~~ ✅
  ```
- **Mark each original bullet point** with strikethrough:
  ```
  - ~~Define a registry of available actions with metadata.~~
  - ~~Each action maps to an async handler function.~~
  ```
- **Add implementation notes** as new (non-struck-through) bullets below the original ones to record what was actually built:
  ```
  - ~~Original planned bullet point.~~
  - ~~Another planned bullet point.~~
  - Implementation note: what was actually done or any deviations from the plan.
  ```
- **Mark a phase heading** as complete when all its tasks are done:
  ```
  ## Phase 1 — Core Migration ✅
  ```
- **Leave incomplete tasks** as plain numbered headings with no strikethrough:
  ```
  ### 14. Task description
  ```
- **Discarded tasks** (decided not to implement) should be marked differently — strikethrough with `DISCARDED` and a brief reason:
  ```
  ### ~~12. Task description~~ DISCARDED
  - ~~Original bullet points struck through.~~
  - Reason: superseded by a simpler approach in task #5.
  ```

## Task Management (todo.vu MCP)

Project tasks live in todo.vu, accessed via the `todo-vu-mcp` MCP server. When asked to find, create, or update a task for this project, use these defaults without asking:

- **Workspace ID:** `mooball`
- **Client ID:** `116` — "Eagle Exports Operations Trust", referred to as **Eagle Exports**
- **Logged-in user ID:** `7`

**Every task for this project belongs to client `116` (Eagle Exports).** Never create a task against a different client. Within that client, default to project `1038` unless the work clearly belongs to a specific feature project.

### Projects under client 116

| ID | Project |
|----|---------|
| 1011 | EagleAgent (main) |
| 1038 | **EagleAgent: Architecture** — default for most new tasks |
| 1025 | EagleAgent: NetSuite |
| 1026 | EagleAgent: RFQ Email |
| 1027 | EagleAgent: Admin |
| 1028 | EagleAgent: RFQ |
| 1030 | EagleAgent: Supplier Research |
| 1031 | EagleAgent: Feedback |
| 1032 | EagleAgent: Quotation |
| 1037 | EagleAgent: Gmail Plugin |

Older/non-EagleAgent projects for the same client: 254 (Google Workspace support), 261 (Support), 279 (solutrans.com.au), 553 (eaglexp.com.au), 680 (Workshop360).

**Default to project `1038` (Architecture)** for new tasks — refactors, technical debt, concurrency, structural bugs, and anything cross-cutting. Use a more specific project only when the work clearly belongs to one feature area.

### Usage notes
- Key tools: `list_tasks`, `create_task`, `change_tasks`, `task_add_comment`, `list_comments_attachments_time_entries`, `list_projects`, `list_clients`, `list_labels`, `list_users`.
- `list_tasks` defaults to `user_mode="assigned"`. Pass `only="active"`/`"completed"`/`"overdue"` to scope by dashboard section, and `search` for free-text lookup.
- `list_projects` with `client_id` is not filtered strictly server-side — verify `client_id` on each returned project.
- Task names come back HTML-escaped (`&amp;`, `&#x27;`).
- **Creating or modifying tasks counts as a write action** — follow the same rule as code changes: propose first, wait for explicit approval.

### Creating a task

1. Use client `116` (Eagle Exports) and project `1038` (EagleAgent: Architecture) unless the work clearly belongs to a specific feature project.
2. Pass the body at creation time via `details_markdown` — there is no need to create the task and patch it afterwards:

   ```python
   create_task(
       workspace_id="mooball",
       name="Task title",
       client_id=116,
       project_id=1038,
       details_markdown="**Why**\n\n...",
   )
   ```

3. Follow the write-action rule in **Usage notes** — propose the task first and wait for approval.

### Task bodies and comments are always Markdown

**Write in Markdown, and read from the `*_markdown` fields.**

| Purpose | Tool | Parameter |
|---|---|---|
| Task description, at creation | `create_task` | `details_markdown` |
| Task description, existing task | `change_tasks` | `details_markdown` (one task id at a time) |
| Comment | `task_add_comment` | `content_markdown` |

**⚠️ The tool schema shown to the client lags the live server.** todo.vu has moved from HTML body parameters to Markdown ones. The schema still advertises the old HTML names (`details`, `description`, `comment`), and all three are now **rejected** by the live server with a pydantic `Unexpected keyword argument` error. Use the `*_markdown` names above. Verified 2026-09-20.

Earlier guidance in this file said to send real HTML (verified 2026-08-17) — that was correct at the time. The `*_markdown` parameters are newer.

**Reading:** `list_tasks` returns `details` (server-rendered HTML — do **not** write to it) alongside `details_markdown`. Comments come back in `content_markdown`. Always read the `*_markdown` field.

GitHub-flavoured Markdown (headings, `**bold**`, bullet and numbered lists, backticks) round-trips cleanly — the server converts it to HTML for display. Two quirks: it splits Markdown into separate blocks at blank lines, so a bold line followed by a list may be stored as two blocks; and `~` may come back escaped as `\~`.

There is **no delete or edit tool for comments** — a posted comment can only be removed by hand in the todo.vu UI. Never post throwaway test comments.

If a `*_markdown` parameter is ever rejected, the schema shown to the client is stale. Validation errors are **non-destructive** (nothing is written), so probe candidate names freely: omit the parameter and the server names the required field, or try a candidate name and see whether the error changes.

## Git & Repository
- Do not commit `.env`, `.venv`, secrets, or `__pycache__/`.
- `pyproject.toml` is the single source of truth for dependencies.
- Shell scripts: `run.sh` (start dev server), `kill-8000.sh` (clear stuck port), `start.sh` (production entry).

## AI Assistant Rules

### ⛔ FILE EDIT AUTHORIZATION — HARD CONSTRAINT
**You MUST NOT create, edit, or delete any file unless the user has given explicit authorization using one of these exact phrases:**
- "go ahead" / "please implement" / "yes do it" / "please proceed" / "go for it"
- "please commit" / "commit this"
- A direct imperative: "change X to Y", "add Z", "create X", "delete Y"

**These are NOT authorization to edit files:**
- "Can you look at X?" — means INVESTIGATE only
- "Is it possible to...?" — means ANSWER the question, do not implement
- "Can we...?" / "Should we...?" — means DISCUSS, do not implement
- "Propose a solution" — means PROPOSE only, do not implement
- "I need X" — describes a need; ask if they want you to implement it
- "Look at this" — means READ/INVESTIGATE only

**If unsure: ASK. Never assume. Interpret every request literally.**

### Production Safety
- **Never modify production data** without explicit approval. Do not run UPDATE/INSERT/DELETE on the production database or production server unless the user explicitly says to proceed. Always stop at diagnosis and ask for permission.
- Production database connection details are in `.env` as `PROD_DATABASE_URL` (Railway proxy: `shortline.proxy.rlwy.net`). See "Connecting to Databases" above for the connection pattern.

### Change Workflow
- **Diagnose first, propose second, implement only after approval.** When the user reports an issue: (1) investigate and explain the root cause, (2) propose a fix with specific files and changes, (3) wait for explicit authorization before making any code changes.
- **Ask before committing.** Present the diff and ask before `git commit` + `git push`. The user wants the opportunity to test changes first.
