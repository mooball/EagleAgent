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
  tools/                   # Tool definitions
    browser_tools.py        # Headless browser automation
    user_profile.py         # User profile management tools (remember/get/forget)
    action_tools.py         # Action button tools for LangGraph
    job_tools.py            # Script execution tools (admin-only)
    product_tools.py        # Product/supplier database search tools
    quote_tools.py          # RFQ/quote workflow tools
  chat/                    # Chainlit-specific modules
    actions.py              # Action registry and dispatcher — replaces slash commands
    commands.py             # Legacy command handlers (deleteall wipe logic)
    document_processing.py  # PDF/image/text/audio processing for file attachments
    local_storage_client.py # LocalStorageClient — file attachments on local disk
    job_progress.py         # Chainlit progress messages for background jobs
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
- **Research Agent** — Standalone research graph (Google Search grounding)
- **Internal Agent** (admin-only) — Same as Eagle Agent, for internal use
- **System Admin** (admin-only) — Single-agent SysAdminAgent graph for script management

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

### File Attachments
- Stored on local disk via `LocalStorageClient` at `DATA_DIR/attachments/`.
- Served to browser via Starlette `StaticFiles` mount at `/files`.
- No cloud storage — files stay on the application host.

## Chainlit (`app.py`)

- `@cl.set_chat_profiles`: Defines available profiles (Eagle Agent, Research Agent, Internal Agent, System Admin).
- `@cl.on_chat_start` / `@cl.on_chat_resume`: Set up thread ID, ensure user profile via `_ensure_user_profile()`, attach action buttons to welcome message.
- `@cl.action_callback`: Handles action button clicks (`new_conversation`, `delete_all_data`, `confirm_delete_all`, `cancel_delete_all`).
- `@cl.on_message` (`main()`): Intercepts help keywords via `is_help_request()`, processes file attachments, invokes graph with streaming.
- `setup_globals()`: Builds the LangGraph `StateGraph` (Supervisor + agent nodes), initializes PostgreSQL connections.
- Keep handlers thin — delegate to agents, prompts module, and document processing.

## Dashboard (`main.py`, `includes/dashboard/`)

The FastAPI dashboard serves HTML pages for managing suppliers, products, RFQs, and users.

- **Routes** (`includes/dashboard/routes.py`): Full-page renders and HTMX partial responses. Uses Jinja2 templates from `templates/`.
- **Context** (`includes/dashboard/context.py`): In-memory store keyed by user email — tracks which dashboard page/entity the user is viewing. Injected into the agent's system prompt so it knows the user's current context.
- **Database** (`includes/dashboard/database.py`): `get_session()` provides SQLAlchemy sync sessions for read queries.
- **Models** (`includes/dashboard/models.py`): SQLAlchemy ORM models — `Supplier`, `Product`, `Brand`, `SupplierBrand`, `ProductSupplier`, etc.
- **Agent Bridge** (`includes/agent_bridge.py`): Bidirectional communication — dashboard can dispatch messages to the agent, agent can notify dashboard to refresh via `cl.send_window_message`.

## Action Buttons (`includes/chat/actions.py`)

Actions replace the old `/` slash commands with Chainlit-native action buttons and LangGraph tools.

- **Registry**: `@register_action(name, label, description, icon, admin_only)` decorator registers a handler.
- **Dispatcher**: `dispatch_action(name)` checks the user's role before executing admin-only actions.
- **Filtering**: `get_actions_for_user(user_id)` returns actions visible to the given user's role.
- **Discovery**: Users can type `help`, `actions`, `menu`, `commands`, or `show actions` to see buttons mid-conversation.
- **LangGraph tools**: `includes/tools/action_tools.py` exposes `list_available_actions`, `start_new_conversation`, and `delete_all_user_data` so the agent can invoke them via natural language.
- **System prompt**: `build_system_prompt()` dynamically includes a list of available actions based on the user's role.

**To add a new action:**
1. In `includes/chat/actions.py`, add a `@register_action(...)` decorated async handler.
2. In `app.py`, add a `@cl.action_callback("your_action_name")` that calls `dispatch_action("your_action_name")`.
3. Optionally add a LangGraph tool wrapper in `includes/tools/action_tools.py`.
4. If admin-only, add the tool name to `ADMIN_ONLY_TOOLS` in `app.py`.

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
- **Confirmation flow**: `run_script` tool sends Run/Cancel buttons. Actual execution happens in `@cl.action_callback("confirm_run_script")` in `app.py`.

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

## Git & Repository
- Do not commit `.env`, `.venv`, secrets, or `__pycache__/`.
- `pyproject.toml` is the single source of truth for dependencies.
- Shell scripts: `run.sh` (start dev server), `kill-8000.sh` (clear stuck port), `start.sh` (production entry).
