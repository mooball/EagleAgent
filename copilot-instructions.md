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

---

# Copilot Instructions for EagleAgent

## Language & Tooling
- Python 3.12, managed with `uv` (no `pip` or `venv` commands).
- Use `~=` (compatible-release) pinning for all dependencies in `pyproject.toml`.
- Use type hints on functions, class attributes, and public APIs.
- Prefer standard library over extra deps when reasonable.

## Project Structure

```
main.py                    # FastAPI ASGI entry point — Google OAuth, session middleware, mounts Chainlit at /chat
app.py                     # Chainlit entry point — graph construction, handlers, streaming
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
  chat/                    # Chat-transport modules
    context.py              # ChatContext protocol + ContextVar — the Chainlit boundary
    context_chainlit.py     # ChainlitChatContext — adapter, may import chainlit
    runner.py               # run_turn() — owns the agent turn and the per-thread run lock
    actions.py              # Action registry and dispatcher
    rfq_actions.py          # RFQ_ACTIONS — (payload, ctx) handlers for dashboard buttons
    streaming_logic.py      # Pure stream-decision helpers (checkpoint repair, repetition guard)
    document_processing.py  # PDF/image/text/audio processing for file attachments
    local_storage_client.py # LocalStorageClient — file attachments on local disk
    job_progress.py         # Progress messages for background jobs
  dashboard/               # FastAPI dashboard modules
    routes.py               # Full-page & HTMX partial routes (Suppliers, Products, RFQs, Users, Home)
    context.py              # In-memory store for current dashboard view per user
    database.py             # SQLAlchemy sync session for dashboard read queries
    models.py               # SQLAlchemy ORM models (Supplier, Product, Brand, etc.)
  agent_bridge.py           # Bidirectional dashboard↔Chainlit communication
  prompts.py                # System prompt builder — dynamic, role-aware, profile-aware
  job_runner.py             # Async background job runner — subprocess management, reaper, signal handling
  mcp_config.py             # MCP server configuration loader
templates/                  # Jinja2 dashboard templates (base.html, suppliers.html, products.html, etc.)
public/
  elements/                 # Custom Chainlit React components (RFQSummary.jsx)
  embedded.js               # Chainlit iframe integration — theme sync, dashboard context push
  stylesheet.css            # Chainlit UI CSS overrides
scripts/                    # Admin scripts (import_products, import_suppliers, etc.)
docs/                       # All documentation except README.md and chainlit.md
tests/                      # All tests (pytest, pytest-asyncio)
  agents/                   # Agent-specific tests
  tools/                    # Tool-specific tests
```

**Conventions:**
- Import agents via the package: `from includes.agents import GeneralAgent, Supervisor`
- Import chat modules: `from includes.chat.actions import dispatch_action`
- Import dashboard modules: `from includes.dashboard.models import Product, Supplier`
- Intra-package imports use direct paths to avoid circular imports: `from includes.agents.base import BaseSubAgent`
- `chainlit.md` must stay in the project root (Chainlit expects it there).

## Dual-App Architecture

EagleAgent runs as two apps in one process:

1. **FastAPI** (`main.py`) — The ASGI entry point. Handles Google OAuth (via `fastapi-sso`), session middleware, dashboard HTML routes, dashboard context API, and mounts Chainlit at `/chat`.
2. **Chainlit** (`app.py`) — The chat UI. Builds LangGraph graphs, defines message/action handlers, streams responses.

The user authenticates via FastAPI, then the session is injected into Chainlit via HTTP headers. The dashboard and chat communicate bidirectionally through `includes/agent_bridge.py`.

### Chat Profiles

The app offers multiple chat profiles via `@cl.set_chat_profiles`:
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
4. Register it as a node in `app.py`'s `setup_globals()` function.
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
- **Data layer**: `SQLAlchemyDataLayer` (Chainlit conversation history).
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
- Stored on local disk via `LocalStorageClient` at `DATA_DIR/attachments/`.
- Served to browser via Starlette `StaticFiles` mount at `/files`.
- No cloud storage — files stay on the application host.

## ⚠️ Chat Architecture — the Chainlit boundary

**Chainlit is being removed.** Phase 1 of the chat migration decoupled all business logic from it. Two CI tests enforce the boundary (`tests/test_no_chainlit_imports.py`, `tests/test_action_coverage.py`), but they cannot catch every way of re-coupling. Follow these rules on any chat-related work.

### 1. Never touch `cl.*` outside the adapter layer

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

**The adapter layer is the only place allowed to import `chainlit`:**
`app.py`, `main.py`, `includes/chat/context_chainlit.py`, `includes/chat/data_layer.py`, `includes/chat/local_storage_client.py`, `includes/agent_bridge.py`.

> **Adding a file to that allowlist is a deliberate architectural decision, not a quick fix.** If you are tempted, the answer is almost always a new method on `ChatContext` instead.

### 2. `includes/` must never import `app`

`app.py` depends on `includes/`, never the reverse. A `from app import main` cycle used to exist and blocked the whole refactor. Enforced by CI.

### 3. Every agent turn goes through `run_turn()`

`includes/chat/runner.py` owns the turn: the stream loop, checkpoint repair, token footer, resilient persistence, and the **per-`thread_id` run lock**. Never call `graph.astream_events(...)` directly — you would bypass all of it and risk corrupting the checkpoint.

- `on_busy="reject"` — user-typed messages.
- `on_busy="wait"` — dashboard-initiated work that should queue.

### 4. Action handlers are `(payload, ctx)` and live in a registry

```python
async def on_my_action(payload: dict, ctx: ChatContext) -> None: ...
RFQ_ACTIONS = {"my_action": on_my_action, ...}   # includes/chat/rfq_actions.py
```

`app.py` adapts them onto `@cl.action_callback` in one loop. **Every button you emit must have a handler** — `tests/test_action_coverage.py` fails otherwise.

### 5. Agents are defined once, in `includes/agents/registry.py`

Adding or renaming an agent means editing `AGENTS` only — not chat profiles, graph selection, or resume handling.

### 6. `ctx.get/set` is per-*session*, not per-*run*

Two runs can be active on one thread (a dashboard button plus a typed message). Anything belonging to a single run belongs on the **context object** — as `active_message` does — or a concurrent run will clobber it. This has already caused one lost-output bug.

## Chainlit (`app.py`)

`app.py` is a **thin adapter**. It builds a `ChainlitChatContext`, calls into `includes/`, and owns the Chainlit lifecycle hooks. Business logic does not belong here.

- `@cl.set_chat_profiles`: Built from `includes/agents/registry.py`.
- `@cl.on_chat_start` / `@cl.on_chat_resume`: Thread ID, user profile via `_ensure_user_profile()`, graph selection via `resolve_agent(...)`.
- `@cl.action_callback`: Lifecycle actions only (`new_conversation`, `cancel_job`, `stop_agent`, `cancel_run_script`). RFQ actions are registered by adapting `RFQ_ACTIONS`.
- `@cl.on_message` (`main()`): Normalises the message, processes attachments, resolves intent, then delegates to `run_turn()`.
- `setup_globals()`: Builds the LangGraph `StateGraph` (Supervisor + agent nodes), initializes PostgreSQL connections.


## Dashboard (`main.py`, `includes/dashboard/`)

The FastAPI dashboard serves HTML pages for managing suppliers, products, RFQs, and users.

- **Routes** (`includes/dashboard/routes.py`): Full-page renders and HTMX partial responses. Uses Jinja2 templates from `templates/`.
- **Context** (`includes/dashboard/context.py`): In-memory store keyed by user email — tracks which dashboard page/entity the user is viewing. Injected into the agent's system prompt so it knows the user's current context.
- **Database** (`includes/dashboard/database.py`): `get_session()` provides SQLAlchemy sync sessions for read queries.
- **Models** (`includes/dashboard/models.py`): SQLAlchemy ORM models — `Supplier`, `Product`, `Brand`, `SupplierBrand`, `Transaction`, etc.
- **Agent Bridge** (`includes/agent_bridge.py`): Bidirectional communication — dashboard can dispatch messages to the agent, agent can notify dashboard to refresh via `cl.send_window_message`.

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
2. In `app.py`, add a `@cl.action_callback("your_action_name")` that calls `dispatch_action("your_action_name", ChainlitChatContext.from_session())`.
3. Optionally add a LangGraph tool wrapper in `includes/tools/action_tools.py`.
4. If admin-only, add the tool name to `ADMIN_ONLY_TOOLS` in `includes/graph.py`.

> **RFQ buttons are different** — they go in `RFQ_ACTIONS` in `includes/chat/rfq_actions.py` as `(payload, ctx)` handlers. `app.py` registers them in one loop; do not add a decorator per button.

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
- **Progress** (`includes/chat/job_progress.py`): Posts Chainlit messages on start (with Cancel button), every 30s, and on completion/failure.
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
- See `docs/TESTING.md` for full guide.

## Error Handling & Logging
- Use Python `logging` (not `print`).
- Fail fast on config issues at startup.
- User-facing errors: catch in Chainlit handlers, send friendly message, log technical details.

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
- **Client ID:** `116` (Eagle Exports Operations Trust)
- **Logged-in user ID:** `7`

### Projects under client 116

| ID | Project |
|----|---------|
| 1011 | EagleAgent (main) |
| 1025 | EagleAgent: NetSuite |
| 1026 | EagleAgent: RFQ Email |
| 1027 | EagleAgent: Admin |
| 1028 | EagleAgent: RFQ |
| 1030 | EagleAgent: Supplier Research |
| 1031 | EagleAgent: Feedback |
| 1032 | EagleAgent: Quotation |
| 1037 | EagleAgent: Gmail Plugin |

Older/non-EagleAgent projects for the same client: 254 (Google Workspace support), 261 (Support), 279 (solutrans.com.au), 553 (eaglexp.com.au), 680 (Workshop360).

Default to project `1011` for new tasks unless the work clearly belongs to one of the more specific projects above.

### Usage notes
- Key tools: `list_tasks`, `create_task`, `change_tasks`, `task_add_comment`, `list_comments_attachments_time_entries`, `list_projects`, `list_clients`, `list_labels`, `list_users`.
- **Task `details` must be markdown or plain text — HTML does not render.** Do not send `<p>` / `<strong>` tags.
- `list_tasks` defaults to `user_mode="assigned"`. Pass `only="active"`/`"completed"`/`"overdue"` to scope by dashboard section, and `search` for free-text lookup.
- `list_projects` with `client_id` is not filtered strictly server-side — verify `client_id` on each returned project.
- Task names come back HTML-escaped (`&amp;`, `&#x27;`); `details` is HTML.
- **Creating or modifying tasks counts as a write action** — follow the same rule as code changes: propose first, wait for explicit approval.

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
