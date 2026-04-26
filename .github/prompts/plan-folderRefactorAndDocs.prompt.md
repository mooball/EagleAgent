# Folder Restructure & Documentation Refresh

Reorganise `includes/` into clear sub-packages (chat, dashboard, agents, tools) so the codebase reflects its three main concerns: **Chainlit chat UI**, **FastAPI dashboard**, and **LangGraph agents**. Then update all documentation to match the new layout.

---

## Phase 1 — Folder Restructure ✅

### ~~1. Create `includes/chat/` package for Chainlit-specific modules~~ ✅

- ~~Move `includes/actions.py` → `includes/chat/actions.py`~~
- ~~Move `includes/commands.py` → `includes/chat/commands.py`~~
- ~~Move `includes/document_processing.py` → `includes/chat/document_processing.py`~~
- ~~Move `includes/local_storage_client.py` → `includes/chat/local_storage_client.py`~~
- ~~Move `includes/job_progress.py` → `includes/chat/job_progress.py`~~
- ~~Create `includes/chat/__init__.py` re-exporting key names for backward compat.~~
- Implementation note: Created `includes/chat/__init__.py` with docstring only (no re-exports needed — all consumers use direct imports).

### ~~2. Create `includes/dashboard/` package for FastAPI dashboard modules~~ ✅

- ~~Move `includes/dashboard_routes.py` → `includes/dashboard/routes.py`~~
- ~~Move `includes/dashboard_context.py` → `includes/dashboard/context.py`~~
- ~~Move `includes/database.py` → `includes/dashboard/database.py`~~
- ~~Move `includes/db_models.py` → `includes/dashboard/models.py`~~
- ~~Create `includes/dashboard/__init__.py` re-exporting key names.~~
- Implementation note: Created `includes/dashboard/__init__.py` with docstring only (no re-exports needed).

### ~~3. Update all imports across the codebase~~ ✅

- ~~Update `app.py` imports (Chainlit entry point — uses chat modules heavily).~~
- ~~Update `main.py` imports (FastAPI entry point — uses dashboard modules).~~
- ~~Update `includes/agent_bridge.py` (bridges both — imports from chat and dashboard).~~
- ~~Update `includes/prompts.py` (reads dashboard context).~~
- ~~Update `includes/agents/*.py` (any agent that references tools, job_progress, etc.).~~
- ~~Update `includes/tools/*.py` (any tool referencing moved modules).~~
- ~~Update `config/scripts.py` if it imports from moved modules.~~
- ~~Grep the entire codebase for old import paths and fix any stragglers.~~
- Implementation note: Updated imports in app.py, main.py, prompts.py, tools/action_tools.py, tools/product_tools.py, dashboard/routes.py, alembic/env.py, and all 9 scripts/ files. agent_bridge.py had no imports needing change.

### ~~4. Update all test imports~~ ✅

- ~~Update every `tests/*.py` and `tests/**/*.py` file that imports from moved modules.~~
- ~~Ensure `conftest.py` fixtures still resolve correctly.~~
- Implementation note: Updated imports in conftest.py, test_actions.py, test_dashboard_context.py, test_dashboard_routes.py, test_file_attachments.py, test_humanize_timestamp.py, test_main_auth.py, tools/test_product_tools.py. Also updated all `patch()` target strings via sed.

### ~~5. Run full test suite — must be green before proceeding~~ ✅

- ~~`uv run pytest tests/ -x --timeout=60 -q --no-header`~~
- ~~All 324+ tests must pass.~~
- ~~Fix any import errors or broken fixtures.~~
- Implementation note: 324 passed, 355 warnings in 40.47s. All green.

---

## Phase 2 — Update Documentation ✅

### ~~6. Rewrite `copilot-instructions.md` to reflect new structure~~ ✅

- ~~Add `main.py` as the ASGI entry point (FastAPI wrapping Chainlit).~~
- ~~Document the dual-app architecture: FastAPI dashboard + Chainlit chat.~~
- ~~Update project structure tree to show `includes/chat/`, `includes/dashboard/`, all agents, all tools.~~
- ~~Update graph routing to show actual agents: `[GeneralAgent | ProcurementAgent | ResearchAgent]`.~~
- ~~Add `agent_bridge.py` to the architecture description.~~
- ~~Document chat profiles (Eagle Agent, Research Agent, Internal Agent, System Admin).~~
- ~~Add `scripts/` directory to the structure listing.~~
- ~~Add `public/elements/` (custom Chainlit React components).~~

### ~~7. Update `README.md`~~ ✅

- ~~Update architecture section with all agents (not just GeneralAgent + BrowserAgent).~~
- ~~Fix "Running the App" to reference `main.py` as the entry point.~~
- ~~Add dashboard UI description (suppliers, products, RFQs, users pages).~~
- ~~Document the Google OAuth → FastAPI → Chainlit header injection flow.~~
- ~~Update doc links section to list all docs.~~

### ~~8. Fix `docs/CONTEXT_ARCHITECTURE.md`~~ ✅

- ~~Replace `call_model()` / `call_tools()` references with Supervisor → Agent → Supervisor loop.~~
- ~~Update the context construction flow diagram to reflect multi-agent routing.~~
- ~~Keep dual-memory explanation (still accurate).~~

### ~~9. Fix `docs/CROSS_THREAD_MEMORY.md`~~ ✅

- ~~Remove all references to deleted `manage_user_profile.py`.~~
- ~~Fix tool filename: `user_profile_tools.py` → `includes/tools/user_profile.py`.~~
- ~~Update "Future Enhancement" section — auto-save via tool calling is already implemented.~~
- ~~Fix `AgentState` → `SupervisorState` references.~~

### ~~10. Fix `docs/DEVELOPMENT_WORKFLOW.md`~~ ✅

- ~~Fix `./kill.sh` → `./kill-8000.sh`.~~
- ~~Add `main.py` as the entry point (not just `chainlit run app.py`).~~
- ~~Add dashboard development workflow (templates, HTMX partials, Tailwind).~~
- ~~Add test command: `uv run pytest tests/ -x --timeout=60`.~~

### ~~11. Fix `docs/GOOGLE_OAUTH_SETUP.md`~~ ✅

- ~~Fix redirect URI: `/auth/oauth/google/callback` → `/auth/google/callback`.~~
- ~~Remove references to non-existent `CLOUD_RUN_DEPLOYMENT.md` and `CHAINLIT_DATA_LAYER_SETUP.md`.~~
- ~~Replace Google Cloud Run commands with Railway deployment instructions.~~
- ~~Document that OAuth flows through FastAPI (`main.py`), not Chainlit directly.~~

### ~~12. Fix `docs/MCP_INTEGRATION.md`~~ ✅

- ~~Replace `call_model()` / `call_tools()` references with `GeneralAgent.get_tools_async()`.~~
- ~~Update data flow diagram to reflect multi-agent architecture.~~
- ~~Remove reference to non-existent `test_mcp_simple.py`.~~

### ~~13. Fix `docs/TESTING.md`~~ ✅

- ~~Add all missing test files to the listing (~12 files missing).~~
- ~~Add dashboard test files: `test_dashboard_context.py`, `test_dashboard_routes.py`, `test_main_auth.py`.~~
- ~~Add tool test files: `tools/test_product_tools.py`, `tools/test_quote_tools.py`, `tools/test_user_profile.py`.~~
- ~~Add agent test files: `agents/test_procurement_agent.py`, `agents/test_supervisor.py`.~~
- ~~Document the `test_postgres_pool` fixture and PostgreSQL requirement.~~

### ~~14. Fix `docs/TEST_AUTO_MEMORY.md`~~ ✅

- ~~Remove all `uv run manage_user_profile.py` commands (script deleted).~~
- ~~Replace manual verification steps with alternative approaches (e.g. psql queries, or note that auto-save is tested in `tests/tools/test_user_profile.py`).~~

### ~~15. Run full test suite — final validation~~ ✅

- ~~`uv run pytest tests/ -x --timeout=60 -q --no-header`~~
- ~~324 passed, 355 warnings in 40.44s.~~

---

## Phase 3 — Cleanup ✅

### ~~16. Remove stale files~~ ✅

- ~~Delete `update_app.py.save` (editor backup file).~~
- ~~Delete `includes/agents/code_agent.py` and `includes/agents/data_agent.py` — empty stubs with no imports.~~
- ~~Verify `service-account-key.json` and `service-account-key.mooball.json` are in `.gitignore`.~~ (confirmed)
- ~~Updated `BaseSubAgent` docstring in `includes/agents/base.py` to list real agents instead of deleted stubs.~~

### ~~17. Final review~~ ✅

- ~~Grep for any remaining references to old import paths (`from includes.actions`, `from includes.dashboard_routes`, etc.) — none found.~~
- ~~Grep docs for any remaining stale file references (`manage_user_profile.py`, `code_agent`, `data_agent`, `update_app.py`) — none found.~~
- ~~324 tests pass after all changes.~~
