# Full Codebase Quality Review

A periodic, comprehensive review of the EagleAgent codebase covering code versions, structure, documentation, testing, and security. Run before major releases or at minimum quarterly.

**This document is the primary process guide.** It defines *what* to review and *how*. Update it only when the review process itself needs refinement — e.g., adding a new phase, adjusting scope, or changing the execution order.

**Findings from each review session go in a separate document** named `codebaseReview-YYYY-MM-DD.md` (also stored in `.github/prompts/`). That findings document captures what was discovered, what passed, and what needs action. Each findings document becomes the basis for prioritised work items. See the [Output Format](#output-format) section below for the template.

> **Architecture note (kept current):** Chainlit was removed. The chat UI is in-house and serves over Server-Sent Events from the same FastAPI app (`/chat-ui`). There is no `app.py`, no `chainlit.md`, and no `includes/chat/data_layer.py`, `local_storage_client.py`, or `commands.py`. Business logic talks to `ChatContext` (`includes/chat/context.py`); `SseChatContext` (`includes/chat/context_sse.py`) is the only implementation. Frontend is Jinja2 + HTMX + Alpine + **Tailwind CSS v4** (CSS-first in `input.css`, standalone CLI, no Node config file) + vendored Preline UI.

---

## Phase 1 — Dependency & Version Health Audit

### 1. Audit Python dependencies

- Verify all dependencies in `pyproject.toml` use `~=` (compatible-release) pinning — no bare `>=` without upper bounds.
- Confirm dev dependencies are in the `[dependency-groups].dev` group, not mixed with production deps.
- Run `uv lock --check` to ensure lockfile is consistent with `pyproject.toml`.
- Run `pip-audit` against the resolved dependency tree for known CVEs.
- Check for deprecated API usage in fast-moving frameworks: `langchain`, `langgraph`, `fastapi`, `sqlalchemy`, `psycopg`, `pydantic`, `google-genai`.
  - In particular, track the `create_react_agent` → `langchain.agents.create_agent` migration (`includes/agents/base.py`) and SQLAlchemy `Query.get()` legacy usage.

### 2. Audit container & infra versions

- Verify `Dockerfile` base image (`python:3.12-slim`) is the latest patch release.
- Verify Node.js version in Dockerfile (currently `22.x LTS`) is still supported and receiving security patches.
- Verify Playwright browser versions in Dockerfile match the installed `playwright` Python package.
- Verify the vendored Tailwind standalone CLI version in `Dockerfile` matches what `input.css` expects (Tailwind **v4**, CSS-first).
- Verify `docker-compose.yml` PostgreSQL + pgvector image is the latest stable release.
- Verify `.python-version` matches `pyproject.toml`'s `requires-python` and the Dockerfile base image.

### 3. Audit database migrations

- Run `alembic check` (or `alembic upgrade head` against a scratch DB) to verify the migration head matches the current ORM models in `includes/dashboard/models.py`.
- Confirm no manual schema changes exist outside of Alembic migrations.
- Verify `alembic/env.py` imports and configuration are correct for both sync and async engine setups.

---

## Phase 2 — Code Structure & Architecture Review

### 4. Review top-level entry points

- Verify `main.py` (FastAPI ASGI entry point) delegates all logic to `includes/` modules — no business logic in the entry point.
- Verify `includes/dashboard/routes/chat_ui.py` (the in-house chat UI router) delegates to `includes/chat/` — route handlers should be thin wrappers over `run_turn()` and the action dispatcher.
- Check for stray root artifacts: scratch scripts (`_diag_*`, `_probe_*`, `_test_*`, `_verify_*`), backup files (`*.bak-*`), committed binaries, and `__pycache__`.
- Verify `input.css` is the Tailwind v4 source and `public/tailwind.min.css` is generated (untracked), not committed.
- Verify module docstrings reflect reality (e.g. `main.py` no longer mentions Chainlit).

### 5. Review `includes/graph.py` — LangGraph construction

- Verify `setup_globals()` initializes shared resources (pool, store, checkpointer, MCP client, job runner) exactly once.
- Verify startup failures fail loudly — in particular that opening the Postgres pool does not swallow errors with a bare `except Exception: pass`.
- Verify the StateGraph wiring: all agent nodes are registered, all conditional edges are correct, Supervisor → agent → Supervisor loop is intact.
- Verify `RouteDecision` literal type in `includes/agents/supervisor.py` includes all active agents and matches the graph router.
- Verify `ADMIN_ONLY_TOOLS` is accurate and enforced.
- Verify model factory (`create_model`) uses correct configuration for each chat profile.

### 6. Review agent implementations — `includes/agents/`

- Verify every sub-agent extends `BaseSubAgent` (`includes/agents/base.py`) and implements required hooks.
- Verify `GeneralAgent` (`general_agent.py`) loads MCP tools correctly and handles errors gracefully.
- Verify `ProcurementAgent` (`procurement_agent.py`) database lookups are efficient — no N+1 query patterns.
- Verify `ResearchAgent` (`research_agent.py`) Google Search grounding is configured correctly with rate-limit handling.
- Verify `SysAdminAgent` (`sysadmin_agent.py`) enforces admin-only access and sandboxes script execution.
- Verify `BrowserAgent` (`browser_agent.py`) is properly excluded from the main graph if not intended for production use, and that its module is not dead weight.
- Verify `Supervisor` (`supervisor.py`) routing: rule-based keyword matching is correct, LLM fallback returns valid agent names, and FINISH termination works.
- Verify `registry.py` remains the single definition of agents and that adding/removing an agent touches only the documented places.

### 7. Review chat modules — `includes/chat/`

- Verify `context.py` `ChatContext` protocol is the only thing business logic depends on, and that `context_sse.py` is the only implementation.
- Verify `runner.py` `run_turn()` is the single path for agent turns — no direct `graph.astream_events(...)` calls elsewhere.
- Verify `actions.py` action registry: all actions have unique names, handlers are async, and `dispatch_action` handles unknown actions gracefully.
- Verify `rfq_actions.py`: all RFQ callbacks are idempotent, state consistency is maintained, and error paths are handled.
- Verify `transcript.py`: thread/step/element persistence is resilient and the per-thread run lock is respected.
- Verify `streaming_logic.py`: checkpoint repair and repetition guard behave correctly.
- Verify `document_processing.py`: all supported file types (PDF, image, text, audio) are handled, size limits are enforced, and user errors are surfaced clearly.
- Verify `middleware.py`: OAuth error redirect is correct, retry notification logic is sound, no middleware leaks.
- Verify `job_progress.py`: progress messages appear in the correct thread, update frequency is reasonable, and Cancel handling works.
- Verify `widgets.py`: in-chat widget rendering is transport-neutral and free of XSS.
- Verify `supplier_search_gate.py`: post-classification search prompt logic is correct.

### 8. Review FastAPI dashboard — `includes/dashboard/`

- Verify `models.py` ORM models match the actual database schema — all tables, columns, relationships, and indexes are correct.
- Verify `database.py` session factories manage connections properly — no connection leaks, sync and async sessions are correctly scoped.
- Verify `context.py` in-memory context is isolated per user — no cross-user data leakage, and memory is bounded (TTL/eviction).
- Verify the supplier domain modules (`supplier_contacts.py`, `supplier_create.py`, `supplier_dedup.py`, `supplier_matching.py`, `supplier_widget.py`) and `email_uploads.py` have clear responsibilities and no duplicated logic.
- Verify all route modules under `routes/` (`admin`, `addon`, `api`, `chat_ui`, `contacts`, `customers`, `opportunities`, `products`, `rfqs`, `suppliers`, `transactions`):
  - Every route has proper authentication (`require_user`) and authorization (`require_role` where needed).
  - Input validation uses Pydantic models or explicit parameter checking.
  - Error responses are consistent (status codes, JSON structure).
  - HTMX partials and full-page renders are correctly distinguished.
- Verify `_helpers.py` utilities are DRY — no duplicated helper logic across routes.

### 9. Review tool definitions — `includes/tools/`

- Verify `action_tools.py`: all LangGraph tool wrappers have correct schemas and descriptions.
- Verify `job_tools.py`: admin-only restrictions are enforced at the tool level, confirmation flow works.
- Verify `product_tools.py`: search queries are efficient and parameterized.
- Verify `quote_tools.py`: RFQ workflow state transitions are correct and error paths are handled.
- Verify `rfq_crud.py`, `rfq_creation_pipeline.py`, `rfq_item_import.py`, and `rfq_render.py`: CRUD is transactional where needed, the creation pipeline guards are sound, and rendering is correct for all RFQ states (draft, sent, received, etc.).
- Verify `supplier_quote_pipeline.py` and `supplier_search_tools.py`: inbound-quote processing and supplier search are correct and tested.
- Verify `user_profile.py`: remember/get/forget operations are atomic and cross-thread persistence works.
- Verify `browser_tools.py`: tool schemas are accurate and browser lifecycle is handled correctly.
- Verify `comms_summary.py`: communications summary is correct.

### 10. Review prompt management — `includes/prompts/`

- Verify `config.py` loads prompt configuration correctly — overrides from YAML work, defaults are sensible.
- Verify `builder.py` assembles system prompts correctly for all user roles and chat profiles.
- Verify `intents.py` intent classification is accurate — no misrouted intents.

### 11. Review the LLM layer — `includes/llm/`

- Verify `context.py`: the workload context (interactive vs background/`SYNC`) is applied on every model path, and background work cannot starve live chat.
- Verify `telemetry.py`: `llm_call_log` writes are correct, tokens/cost reconcile on both the raw-SDK and LangChain paths, the model-ladder fallback is recorded, and telemetry failures never break a turn.
- Verify tests do not write to the telemetry table (see `tests/conftest.py`).

### 12. Review integrations — `includes/gmail/`, `includes/netsuite/`, `includes/hubspot/`

- Verify `gmail/matching.py`: mailbox matching is performant, domain indexing is correct.
- Verify `gmail/draft_service.py`: draft creation works, custom headers are set, errors are surfaced.
- Verify `netsuite/auth.py`: OAuth token refresh works, credentials are stored securely (not in source).
- Verify `netsuite/client.py`: REST client handles rate limits and retries, error responses are parsed correctly.
- Verify `netsuite/queries.py`: all queries are parameterized (no SuiteQL injection), results are paginated where needed.
- Verify `netsuite/constants.py`, `netsuite/departments.py`, `netsuite/countries.py`, `netsuite/records/`: enums and constants are complete and match the NetSuite API documentation.
- Verify `netsuite/sync_utils.py`: dedup and matching algorithms are correct and tested.
- Verify `hubspot/`: if the package is still empty, confirm whether the integration is intentionally incomplete or should be removed along with its dependency.

### 13. Review static assets — `public/`, `templates/`

- Verify `public/tailwind.min.css` is generated from the current `input.css` (Tailwind v4) — regenerate if needed; it should be untracked.
- Verify `public/vendor/preline/`: the vendored version is recorded and referenced consistently.
- Verify `public/avatars/` and other assets are referenced — no orphaned files.
- Verify all Jinja2 templates in `templates/`, `templates/chat_ui/`, and `templates/partials/` are referenced by at least one route — no orphaned templates.
- Verify template variables are properly escaped (XSS prevention) — flag every `| safe` usage and justify it.
- Verify UI consistency across all dashboard pages — same layout, styling, and interaction patterns.

---

## Phase 3 — Documentation Audit

### 14. Audit project-level documentation

- Verify `README.md`: project description is accurate, setup instructions work from scratch, architecture overview is current, and all doc links resolve.
- Verify `AGENTS.md`: reflects the current codebase structure, all agent names are correct, all conventions are accurate. This is the primary instruction file for OpenCode.
- Verify `copilot-instructions.md`: still accurate for Copilot, or note it as a stale duplicate of `AGENTS.md` (drift risk).

### 15. Audit architecture documentation — `docs/`

- Verify `AGENT_GRAPH_ARCHITECTURE.md`: graph structure matches the actual implementation in `includes/graph.py`, all agents are documented, state schema is accurate.
- Verify `AGENT_BRIDGE.md`: bridge mechanism is correctly described and matches `includes/agent_bridge.py`.
- Verify `CHAT_UI.md`: the SSE transport, threads, storage, and embed are described as implemented.
- Verify `CONTEXT_ARCHITECTURE.md`: context and message flow are accurately documented.
- Verify `CROSS_THREAD_MEMORY.md`: PostgreSQL store schema and API are correctly documented.
- Verify `DEVELOPMENT_WORKFLOW.md`: dev setup steps work, linting/formatting instructions are current, test commands are correct.
- Verify `FILE_ATTACHMENTS.md`: supported file types, size limits, and storage paths are accurate.
- Verify `FUTURE_AGENT_PLANNING.md`: roadmap is current — completed items are marked, new plans are added.
- Verify `GMAIL_GETTING_STARTED.md` and `GMAIL_SETUP.md`: user-facing and technical Gmail steps are accurate.
- Verify `GOOGLE_OAUTH_SETUP.md`: Google Cloud Console steps are accurate and links work.
- Verify `MCP_INTEGRATION.md`: MCP server setup instructions are current and match `config/mcp_servers.yaml.example`.
- Verify `NETSUITE_INTEGRATION.md`, `RFQ_WORKFLOW.md`, `SUPPLIER_CATEGORIZATION.md`, `CURRENCY_CONVERSION.md`, `INTERNAL_AGENT.md`: accurate against the current code.
- Verify `SERVER_SCRIPTS.md`: all registered scripts in `config/scripts.py` are listed and described.
- Verify `TESTING.md` and `TEST_AUTO_MEMORY.md`: test commands, markers, and fixture descriptions are current.

### 16. Audit configuration documentation

- Verify `config/mcp_servers.yaml.example` is a valid template with all required fields — no missing or stale fields.
- Verify `config/prompts.yaml.example` matches the current prompt structure in `includes/prompts/`.
- Verify `.env.example` lists all required environment variables — any new secrets since last review must be added.

### 17. Audit inline documentation

- Scan all source files in `includes/` for public functions, classes, and methods without docstrings — flag gaps.
- Check for complex algorithms without explanatory comments.
- Check for magic numbers that should be named constants.
- Catalog all TODO/FIXME/HACK comments and assess each for priority.

---

## Phase 4 — Testing & Coverage Audit

### 18. Audit test infrastructure

- Verify `tests/conftest.py` fixtures are comprehensive and realistic — mocks should not be so thin that they hide real bugs.
- Verify tests that need a database are explicitly marked and skip cleanly when none is available — no test should silently depend on a live prod-adjacent DB.
- Verify `pytest` configuration in `pyproject.toml`: markers (`slow`, `integration`) are defined and used consistently.
- Verify `pytest-asyncio` mode is `"auto"` and all async tests work without manual decorators; flag redundant `@pytest.mark.asyncio`.
- Verify `pytest-timeout` setting (30s) is reasonable — no test should legitimately need more than 30s.
- Run the full suite: `uv run pytest tests/ -q --timeout=30` — every non-skipped test must pass.

### 19. Audit agent test coverage — `tests/agents/`

- Verify `test_supervisor.py`: all routing paths are tested (each agent, FINISH), fallback routing is tested, edge cases (empty input, malformed input) are covered.
- Verify `test_general_agent.py`: MCP tool calls are tested, error handling is tested, tool loading failures are handled.
- Verify `test_procurement_agent.py`: all lookup query variations are tested with different inputs.
- Verify `test_research_agent.py` and `test_sysadmin_agent.py` exist and cover routing, tools, and admin-only enforcement.
- Verify `test_browser_agent.py`: browser lifecycle and error paths are tested, excluded-from-graph state is tested.

### 20. Audit tool test coverage — `tests/tools/`

- Verify `test_user_profile.py`: remember/get/forget operations are tested, cross-thread persistence is verified.
- Verify `test_product_tools.py`: product search and filtering are tested with varied inputs.
- Verify `test_quote_tools.py`: RFQ workflow state transitions are tested, error states are covered.
- Verify the RFQ pipeline files (`test_rfq_crud.py`, `test_rfq_creation_pipeline.py`, `test_rfq_item_import.py`, `test_rfq_pipeline_lock.py`) and supplier files (`test_supplier_sourcing.py`, `test_quote_tools.py`) cover the RFQ/supplier workflow end to end.

### 21. Audit integration & system test coverage

- Verify `test_integration.py`: end-to-end scenarios cover the full agent pipeline, both happy paths and error paths.
- Verify `test_graph_wiring.py`: full graph structure is validated — no missing or extra nodes/edges.
- Verify `test_mcp_integration.py`: MCP server connections are tested, disconnection and error handling are covered.
- Verify `test_main_auth.py`: OAuth flow is tested, unauthorized requests are rejected, session middleware works.
- Verify `test_job_runner.py` and `test_job_tools.py`: subprocess lifecycle is tested, timeouts and errors are handled, reaper works, admin restrictions and confirmation flow are tested.
- Verify `test_chat_ui_routes.py`, `test_bridge_dispatch_sse.py`, and `tests/client/` cover the in-house chat transport.

### 22. Audit dashboard & data test coverage

- Verify `test_dashboard_routes.py`: all routes are tested, auth checks are verified per route, HTMX partials are tested.
- Verify `test_dashboard_context.py`: context isolation per user is tested, concurrent access is safe.
- Verify `test_database_matching.py`: matching algorithms are tested with real and edge-case data, test isolation from production data is confirmed.
- Verify `test_supplier_*.py`: supplier contacts, creation, dedup, widget, and matching are all tested.
- Verify `test_supplier_categorization.py`, `test_currency.py`, `test_document_processing.py`: all covered.
- Verify `test_netsuite.py` and `test_netsuite_expanded.py`: NetSuite integration is comprehensively tested.
- Verify `test_rfq_*.py`: the RFQ workflow (enrichment, link targets, supplier email/contact/edit, bulk retry, pipeline lock) is tested end to end.

### 23. Identify test coverage gaps

- Scan for source modules with no corresponding test file.
- Pay particular attention to modules handling I/O, authentication, data mutation, and new code added since the last review (`includes/llm/`, `includes/chat/widgets.py`, `includes/dashboard/supplier_*.py`, `includes/dashboard/email_uploads.py`, `includes/tools/rfq_creation_pipeline.py`).
- Flag the highest-risk gaps explicitly.

### 24. Audit test quality

- Verify tests are isolated — no shared mutable state between tests that could cause ordering-dependent failures.
- Verify async tests use `pytest-asyncio` correctly — no `asyncio.run()` inside test functions.
- Verify `@pytest.mark.slow` is applied to all tests that legitimately take longer than a few seconds.
- Verify `@pytest.mark.integration` is applied to tests requiring external services.
- Spot-check test assertions — are they specific and meaningful, or just `assert True` / `assert response`?
- Verify edge cases are tested: empty inputs, None values, very large inputs, concurrent access.
- Verify mock objects are realistic — over-mocking can hide real integration bugs.

---

## Phase 5 — Security Review

### 25. Secrets & credentials audit

- Scan all source files for hardcoded secrets (API keys, passwords, tokens) — none should exist outside of `.env` or environment variables.
- Verify `service-account-key.json` and any `.json`/`.pem` key files are in `.gitignore` and not committed.
- Verify `.env.example` has placeholder values only — no real credentials.
- Check `.env` (if accessible) does not contain secrets that should be in a secrets manager.

### 26. Injection & input validation audit

- Verify all database queries use parameterized statements — no raw SQL with string interpolation or f-strings.
- Verify all NetSuite queries in `includes/netsuite/queries.py` are parameterized.
- Verify Jinja2 templates use auto-escaping — user-supplied data is not rendered raw; justify every `| safe`.
- Verify the chat UI and in-chat widgets do not allow HTML/JS injection from user or agent output.

### 27. Authentication & authorization audit

- Verify Google OAuth flow in `main.py` is complete — token validation, session creation, user identity extraction.
- Verify admin routes in `includes/dashboard/routes/admin.py` and admin actions in `includes/chat/actions.py` enforce role checks.
- Verify `SysAdminAgent` and `includes/tools/job_tools.py` enforce admin-only access at both the tool and execution levels.
- Verify role-based access controls are consistent across the dashboard, chat actions, and LangGraph tools.

### 28. File upload security audit

- Verify `includes/chat/document_processing.py` validates file types by content (MIME type detection), not just file extension.
- Verify file size limits are enforced before processing.
- Verify upload paths cannot be manipulated to write outside the designated `DATA_DIR/attachments/` directory (path traversal prevention).
- Verify uploaded files are served via Starlette `StaticFiles` with correct content-type headers.

### 29. Subprocess execution audit

- Verify `includes/job_runner.py` does not allow arbitrary command injection — only registered scripts from `config/scripts.py` can be executed.
- Verify script arguments are validated against the allowed args defined in the registry.
- Verify subprocesses run with the same permissions as the application (non-root `eagleagent` user in Docker).

---

## Phase 6 — Performance Review

### 30. Database performance

- Scan all dashboard routes and agent tools for N+1 query patterns — check joins and eager loading. Re-check the RFQ detail view supplier-contact loop flagged previously.
- Verify PostgreSQL connection pool sizes are appropriate for expected concurrency in `config/settings.py`.
- Verify indexes exist on frequently queried columns in `includes/dashboard/models.py` — check supplier name, product SKU, RFQ status, email tracking message IDs.
- Check for missing foreign key indexes on join columns.

### 31. Memory & resource usage

- Verify `includes/dashboard/context.py` in-memory context is bounded — TTL, max size, or eviction policy.
- Verify `includes/job_runner.py` ring buffers (200-line output capture) don't grow unbounded for long-running jobs.
- Verify LangGraph checkpointer is pruned by the maintenance loop — retention is bounded.
- Verify `data/attachments/` and `public/avatars/` have cleanup/pruning — no unbounded disk growth.

### 32. Async patterns & blocking calls

- Scan for sync calls in async contexts — accidental blocking of the event loop.
- Verify all database operations in agent code paths use async sessions where available.
- Verify file I/O in `document_processing.py` does not block the event loop for large files.

### 33. Caching opportunities

- Identify expensive operations that are recomputed frequently: currency conversion, supplier matching, domain extraction.
- Verify whether caching is appropriate and, if implemented, whether cache invalidation is correct (Gmail credentials cache TTL remains an open item).

---

## Phase 7 — Code Quality & Consistency

### 34. Type hints audit

- Spot-check public functions across all `includes/` modules for complete type hints on parameters and return values.
- Verify class attributes have type annotations where applicable.

### 35. Error handling audit

- Verify exceptions are caught at appropriate levels — not silently swallowed at the top level, not leaking sensitive internal details to users. Flag bare `except Exception:` and `except Exception: pass`.
- Verify user-facing error messages are helpful and actionable.
- Verify retry logic (Gemini API, MCP connections, NetSuite API) has reasonable backoff and max retries.

### 36. Logging audit

- Verify Python `logging` is used consistently — no `print()` statements in production code paths.
- Verify log levels are appropriate: `DEBUG` for development, `INFO` for key events, `WARNING` for recoverable issues, `ERROR` for failures.
- Verify sensitive data (emails, tokens, PII) is not logged at `INFO` or `DEBUG` levels.

### 37. Code cleanliness

- Scan for unused imports across the codebase.
- Scan for commented-out code blocks — either remove or add a comment explaining why they're preserved.
- Verify import ordering: stdlib → third-party → local, per PEP 8.
- Verify naming consistency: no single-letter variables except trivial loop indices, descriptive function and class names.
- Verify magic numbers and strings are extracted to named constants — especially in `config/settings.py`.

### 38. Dead code & orphaned files

- Identify any source files not imported by any other file in the codebase.
- Identify any scripts in `scripts/` not registered in `config/scripts.py` — are they intentionally standalone?
- Identify any root-level scratch/backup files (`_diag_*`, `_probe_*`, `_test_*`, `_verify_*`, `*.bak-*`) and recommend consolidation or removal.
- Identify any templates in `templates/` not referenced by any route.
- Identify any static assets in `public/` not referenced by any template or component.

---

## Review Execution Order

Work through the phases in order for maximum efficiency:

1. **Phase 1** (Dependency Audit) — quick wins, automated checks.
2. **Phase 2** (Structure Review) — the largest phase, deep code reading.
3. **Phase 3** (Documentation Audit) — compare docs against the structure found in Phase 2.
4. **Phase 4** (Testing Audit) — run tests, identify gaps, assess quality.
5. **Phase 5** (Security Review) — secrets scan, injection scan, auth verification.
6. **Phase 6** (Performance Review) — query analysis, memory profiling.
7. **Phase 7** (Code Quality) — final cleanup pass.

## Output Format

After completing the review, create a findings document at `.github/prompts/codebaseReview-YYYY-MM-DD.md` using the template below. This findings document captures what was discovered and becomes the basis for prioritised action items. Do not update this primary guide with findings — keep it as the evergreen process reference.

```markdown
# EagleAgent Codebase Review — YYYY-MM-DD

> Review conducted against the process defined in `plan-fullCodebaseReview.prompt.md`.

### Critical Issues (must fix before next release)
- [ ] Issue description — file(s) affected

### Warnings (should fix soon)
- [ ] Issue description — file(s) affected

### Suggestions (nice to have)
- [ ] Issue description — file(s) affected

### Test Coverage Summary
- Total test files: N
- Tests passing: N
- Modules with no direct test coverage: N
- Highest-risk gaps: [list]

### Documentation Status
- Docs reviewed: N
- Docs needing updates: N
- Missing documentation topics: [list]

### Action Items
- [ ] [actionable item derived from findings above]
- [ ] …
```
