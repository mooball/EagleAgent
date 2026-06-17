# Full Codebase Quality Review

A periodic, comprehensive review of the EagleAgent codebase covering code versions, structure, documentation, testing, and security. Run before major releases or at minimum quarterly.

**This document is the primary process guide.** It defines *what* to review and *how*. Update it only when the review process itself needs refinement — e.g., adding a new phase, adjusting scope, or changing the execution order.

**Findings from each review session go in a separate document** named `codebaseReview-YYYY-MM-DD.md` (also stored in `.github/prompts/`). That findings document captures what was discovered, what passed, and what needs action. Each findings document becomes the basis for prioritised work items. See the [Output Format](#output-format) section below for the template.

---

## Phase 1 — Dependency & Version Health Audit

### 1. Audit Python dependencies

- Verify all dependencies in `pyproject.toml` use `~=` (compatible-release) pinning — no bare `>=` without upper bounds.
- Confirm dev dependencies are in the `[dependency-groups].dev` group, not mixed with production deps.
- Run `uv lock --check` to ensure lockfile is consistent with `pyproject.toml`.
- Run `pip-audit` or `safety check` against the resolved dependency tree for known CVEs.
- Check for deprecated API usage in fast-moving frameworks: `langchain`, `langgraph`, `chainlit`, `fastapi`, `sqlalchemy`.

### 2. Audit container & infra versions

- Verify `Dockerfile` base image (`python:3.12-slim`) is the latest patch release.
- Verify Node.js version in Dockerfile (`20.x LTS`) is still supported and receiving security patches.
- Verify Playwright browser versions in Dockerfile match the installed `playwright` Python package.
- Verify `docker-compose.yml` PostgreSQL + pgvector image (`pgvector/pgvector:0.8.0-pg17`) is the latest stable release.
- Verify `.python-version` matches `pyproject.toml`'s `requires-python` and the Dockerfile base image.

### 3. Audit database migrations

- Run `alembic check` to verify the migration head matches the current ORM models in `includes/dashboard/models.py`.
- Confirm no manual schema changes exist outside of Alembic migrations.
- Verify `alembic/env.py` imports and configuration are correct for both sync and async engine setups.

---

## Phase 2 — Code Structure & Architecture Review

### 4. Review top-level entry points

- Verify `main.py` (FastAPI ASGI entry point) delegates all logic to `includes/` modules — no business logic in the entry point.
- Verify `app.py` (Chainlit entry point) delegates to `includes/` modules — handlers should be thin wrappers.
- Verify `chainlit.md` is present at the project root (required by Chainlit).
- Check for orphaned files at the root level: `input.css`, `tailwindcss` binary — are these still needed or are they artifacts?

### 5. Review `includes/graph.py` — LangGraph construction

- Verify `setup_globals()` initializes shared resources (pool, store, checkpointer, MCP client, job runner) exactly once.
- Verify the StateGraph wiring: all agent nodes are registered, all conditional edges are correct, Supervisor → agent → Supervisor loop is intact.
- Verify `RouteDecision` literal type in `includes/agents/supervisor.py` includes all active agents.
- Verify `ADMIN_ONLY_TOOLS` set is accurate and enforced.
- Verify model factory (`create_model`) uses correct configuration for each chat profile.

### 6. Review agent implementations — `includes/agents/`

- Verify every sub-agent extends `BaseSubAgent` (`includes/agents/base.py`) and implements required hooks.
- Verify `GeneralAgent` (`general_agent.py`) loads MCP tools correctly and handles errors gracefully.
- Verify `ProcurementAgent` (`procurement_agent.py`) database lookups are efficient — no N+1 query patterns.
- Verify `ResearchAgent` (`research_agent.py`) Google Search grounding is configured correctly with rate-limit handling.
- Verify `SysAdminAgent` (`sysadmin_agent.py`) enforces admin-only access and sandboxes script execution.
- Verify `BrowserAgent` (`browser_agent.py`) is properly disabled in the main graph if not intended for production use.
- Verify `Supervisor` (`supervisor.py`) routing: rule-based keyword matching is correct, LLM fallback returns valid agent names, and FINISH termination works.

### 7. Review Chainlit modules — `includes/chat/`

- Verify `actions.py` action registry: all actions have unique names, handlers are async, and `dispatch_action` handles unknown actions gracefully.
- Verify `commands.py` legacy handlers are either properly deprecated or still needed — no dead code.
- Verify `document_processing.py`: all supported file types (PDF, image, text, audio) are handled, file size limits are enforced, and user errors are surfaced clearly.
- Verify `local_storage_client.py`: storage path is configurable, files are served correctly, and cleanup/pruning is in place.
- Verify `data_layer.py` custom `SQLAlchemyDataLayer` is compatible with the current Chainlit version.
- Verify `middleware.py`: OAuth error redirect is correct, retry notification logic is sound, no middleware leaks.
- Verify `rfq_actions.py`: all RFQ callbacks are idempotent, state consistency is maintained, and error paths are handled.
- Verify `job_progress.py`: progress messages appear in the correct thread, update frequency is reasonable, and Cancel handling works.

### 8. Review FastAPI dashboard — `includes/dashboard/`

- Verify `models.py` ORM models match the actual database schema — all tables, columns, relationships, and indexes are correct.
- Verify `database.py` session factories manage connections properly — no connection leaks, sync and async sessions are correctly scoped.
- Verify `context.py` in-memory context is isolated per user — no cross-user data leakage, and memory is bounded (no unbounded growth).
- Verify all route modules under `routes/`:
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
- Verify `rfq_crud.py`: all CRUD operations are transactional where needed.
- Verify `rfq_render.py`: rendering is correct for all RFQ states (draft, sent, received, etc.).
- Verify `user_profile.py`: remember/get/forget operations are atomic and cross-thread persistence works.
- Verify `browser_tools.py`: tool schemas are accurate and browser lifecycle is handled correctly.

### 10. Review prompt management — `includes/prompts/`

- Verify `config.py` loads prompt configuration correctly — overrides from YAML work, defaults are sensible.
- Verify `builder.py` assembles system prompts correctly for all user roles and chat profiles.
- Verify `intents.py` intent classification is accurate — no misrouted intents.

### 11. Review integrations — `includes/gmail/`, `includes/netsuite/`, `includes/hubspot/`

- Verify `gmail/matching.py`: mailbox matching is performant, domain indexing is correct.
- Verify `gmail/draft_service.py`: draft creation works, custom headers are set, errors are surfaced.
- Verify `netsuite/auth.py`: OAuth token refresh works, credentials are stored securely (not in source).
- Verify `netsuite/client.py`: REST client handles rate limits and retries, error responses are parsed correctly.
- Verify `netsuite/queries.py`: all queries are parameterized (no SQL/query injection), results are paginated where needed.
- Verify `netsuite/constants.py`: enums and constants are complete and match the NetSuite API documentation.
- Verify `netsuite/sync_utils.py`: dedup and matching algorithms are correct and tested.
- Verify `hubspot/`: if the package is empty, confirm whether the integration is intentionally incomplete or abandoned.

### 12. Review static assets — `public/`, `templates/`

- Verify `public/embedded.js`: no unused functions, minified for production if appropriate.
- Verify `public/stylesheet.css`: styles are consistent, no orphaned classes.
- Verify `public/tailwind.min.css`: matches the current `tailwind.config.js` content configuration — regenerate if needed.
- Verify `public/elements/`: all custom React/JSX components are compatible with the current Chainlit element API.
- Verify `public/avatars/`: all avatar assets are referenced — no orphaned files.
- Verify all Jinja2 templates in `templates/` are referenced by at least one dashboard route — no orphaned templates.
- Verify template variables are properly escaped (XSS prevention).
- Verify UI consistency across all dashboard pages — same layout, styling, and interaction patterns.

---

## Phase 3 — Documentation Audit

### 13. Audit project-level documentation

- Verify `README.md`: project description is accurate, setup instructions work from scratch, architecture overview is current.
- Verify `copilot-instructions.md`: reflects the current codebase structure, all agent names are correct, all conventions are accurate.
- Verify `chainlit.md`: welcome/system message is appropriate and up to date.

### 14. Audit architecture documentation — `docs/`

- Verify `AGENT_GRAPH_ARCHITECTURE.md`: graph structure matches the actual implementation in `includes/graph.py`, all agents are documented, state schema is accurate.
- Verify `AGENT_BRIDGE.md`: bridge mechanism is correctly described and matches `includes/agent_bridge.py`.
- Verify `CONTEXT_ARCHITECTURE.md`: in-memory context design is accurately documented.
- Verify `CROSS_THREAD_MEMORY.md`: PostgreSQL store schema and API are correctly documented.
- Verify `DEVELOPMENT_WORKFLOW.md`: dev setup steps work, linting/formatting instructions are current, test commands are correct.
- Verify `FILE_ATTACHMENTS.md`: supported file types, size limits, and storage paths are accurate.
- Verify `FUTURE_AGENT_PLANNING.md`: roadmap is current — completed items are marked, new plans are added.
- Verify `GMAIL_GETTING_STARTED.md`: user-facing OAuth steps are accurate and testable.
- Verify `GMAIL_SETUP.md`: technical Gmail integration steps are current.
- Verify `GOOGLE_OAUTH_SETUP.md`: Google Cloud Console steps are accurate and screenshots/links work.
- Verify `MCP_INTEGRATION.md`: MCP server setup instructions are current and match `config/mcp_servers.yaml.example`.
- Verify `SERVER_SCRIPTS.md`: all registered scripts in `config/scripts.py` are listed and described.
- Verify `TESTING.md`: test commands, markers, and fixture descriptions are current.
- Verify `TEST_AUTO_MEMORY.md`: memory testing strategy is accurate.

### 15. Audit configuration documentation

- Verify `config/mcp_servers.yaml.example` is a valid template with all required fields — no missing or stale fields.
- Verify `config/prompts.yaml.example` matches the current prompt structure in `includes/prompts/`.
- Verify `.env.example` lists all required environment variables — any new secrets since last review must be added.

### 16. Audit inline documentation

- Scan all source files in `includes/` for public functions, classes, and methods without docstrings — flag gaps.
- Check for complex algorithms without explanatory comments.
- Check for magic numbers that should be named constants.
- Catalog all TODO/FIXME/HACK comments and assess each for priority.

---

## Phase 4 — Testing & Coverage Audit

### 17. Audit test infrastructure

- Verify `tests/conftest.py` fixtures are comprehensive and realistic — mocks should not be so thin that they hide real bugs.
- Verify PostgreSQL pool configuration in tests is correct — tests should use the same database version as production.
- Verify `pytest` configuration in `pyproject.toml`: markers (`slow`, `integration`) are defined and used consistently.
- Verify `pytest-asyncio` mode is `"auto"` and all async tests work without manual decorators.
- Verify `pytest-timeout` setting (60s) is reasonable — no test should legitimately need more than 60s.
- Run the full test suite: `uv run pytest tests/ -x --timeout=60 -q --no-header` — every test must pass.

### 18. Audit agent test coverage — `tests/agents/`

- Verify `test_supervisor.py`: all routing paths are tested (each agent, FINISH), fallback routing is tested, edge cases (empty input, malformed input) are covered.
- Verify `test_general_agent.py`: MCP tool calls are tested, error handling is tested, tool loading failures are handled.
- Verify `test_procurement_agent.py`: all lookup query variations are tested with different inputs.
- Verify `test_browser_agent.py`: browser lifecycle and error paths are tested, disabled state is tested.

### 19. Audit tool test coverage — `tests/tools/`

- Verify `test_user_profile.py`: remember/get/forget operations are tested, cross-thread persistence is verified.
- Verify `test_supplier_sourcing.py`: supplier search scenarios cover edge cases (no results, partial matches).
- Verify `test_product_tools.py`: product search and filtering are tested with varied inputs.
- Verify `test_quote_tools.py`: RFQ workflow state transitions are tested, error states are covered.

### 20. Audit integration & system test coverage

- Verify `test_integration.py`: end-to-end scenarios cover the full agent pipeline, both happy paths and error paths.
- Verify `test_graph_wiring.py`: full graph structure is validated — no missing or extra nodes/edges.
- Verify `test_mcp_integration.py`: MCP server connections are tested, disconnection and error handling are covered.
- Verify `test_main_auth.py`: OAuth flow is tested, unauthorized requests are rejected, session middleware works.
- Verify `test_job_runner.py`: subprocess lifecycle is tested, timeouts and errors are handled, reaper works.
- Verify `test_job_tools.py`: admin restrictions are tested, confirmation flow is tested.

### 21. Audit dashboard & data test coverage

- Verify `test_dashboard_routes.py`: all routes are tested, auth checks are verified per route, HTMX partials are tested.
- Verify `test_dashboard_context.py`: context isolation per user is tested, concurrent access is safe.
- Verify `test_database_matching.py`: matching algorithms are tested with real and edge-case data, test isolation from production data is confirmed.
- Verify `test_supplier_categorization.py`: all categories are tested, edge cases are covered.
- Verify `test_currency.py`: currency conversions are tested with various rate scenarios and error cases.
- Verify `test_document_processing.py`: all supported file types are tested, size limits are enforced, errors are handled.
- Verify `test_netsuite.py` and `test_netsuite_expanded.py`: NetSuite integration is comprehensively tested.
- Verify `test_rfq_enrichment.py`: RFQ enrichment workflow is tested end to end.

### 22. Identify test coverage gaps

- Scan for source modules with no corresponding test file:
  - `includes/gmail/matching.py` and `includes/gmail/draft_service.py` — no dedicated tests.
  - `includes/hubspot/` — no tests (confirm if integration is inactive).
  - `includes/chat/data_layer.py` — no dedicated test for custom SQLAlchemy data layer.
  - `includes/chat/middleware.py` — no dedicated test for ASGI middleware.
  - `includes/chat/local_storage_client.py` — tested indirectly only.
  - `includes/chat/rfq_actions.py` — no dedicated test for RFQ action callbacks.
  - `includes/chat/job_progress.py` — no dedicated test for progress messages.
  - `includes/agent_bridge.py` — no dedicated test for bidirectional bridge.
  - `includes/mcp_config.py` — tested indirectly via MCP integration tests only.
  - `includes/prompts/intents.py` — no dedicated test for intent classification.
  - `includes/netsuite/queries.py` — no dedicated test for query builders.
  - `includes/netsuite/sync_utils.py` — no dedicated test for sync helpers.
  - `includes/supplier_categorization.py` — no dedicated test.
  - `config/scripts.py` — no dedicated test for script registry.
  - `includes/chat/commands.py` — no dedicated test for legacy command handlers.
- Flag the highest-risk gaps: modules handling I/O, authentication, or data mutation.

### 23. Audit test quality

- Verify tests are isolated — no shared mutable state between tests that could cause ordering-dependent failures.
- Verify async tests use `pytest-asyncio` correctly — no `asyncio.run()` inside test functions.
- Verify `@pytest.mark.slow` is applied to all tests that legitimately take longer than a few seconds.
- Verify `@pytest.mark.integration` is applied to tests requiring external services.
- Spot-check test assertions — are they specific and meaningful, or just `assert True` / `assert response`?
- Verify edge cases are tested: empty inputs, None values, very large inputs, concurrent access.
- Verify mock objects are realistic — over-mocking can hide real integration bugs.

---

## Phase 5 — Security Review

### 24. Secrets & credentials audit

- Scan all source files for hardcoded secrets (API keys, passwords, tokens) — none should exist outside of `.env` or environment variables.
- Verify `service-account-key.json` and any `.json` key files are in `.gitignore` and not committed.
- Verify `.env.example` has placeholder values only — no real credentials.
- Check `.env` (if accessible) does not contain secrets that should be in a secrets manager.

### 25. Injection & input validation audit

- Verify all database queries use parameterized statements — no raw SQL with string interpolation or f-strings.
- Verify all NetSuite queries in `includes/netsuite/queries.py` are parameterized.
- Verify Jinja2 templates use auto-escaping — user-supplied data is not rendered raw.
- Verify Chainlit message rendering does not allow HTML/JS injection from user or agent output.

### 26. Authentication & authorization audit

- Verify Google OAuth flow in `main.py` is complete — token validation, session creation, user identity extraction.
- Verify admin routes in `includes/dashboard/routes/admin.py` and `includes/chat/actions.py` enforce `require_role("admin")` or equivalent.
- Verify `SysAdminAgent` and `includes/tools/job_tools.py` enforce admin-only access at both the tool and execution levels.
- Verify role-based access controls are consistent across the dashboard, chat actions, and LangGraph tools.

### 27. File upload security audit

- Verify `includes/chat/document_processing.py` validates file types by content (MIME type detection), not just file extension.
- Verify file size limits are enforced before processing.
- Verify upload paths cannot be manipulated to write outside the designated `DATA_DIR/attachments/` directory (path traversal prevention).
- Verify uploaded files are served via Starlette `StaticFiles` with correct content-type headers.

### 28. Subprocess execution audit

- Verify `includes/job_runner.py` does not allow arbitrary command injection — only registered scripts from `config/scripts.py` can be executed.
- Verify script arguments are validated against the allowed args defined in the registry.
- Verify subprocesses run with the same permissions as the application (non-root `eagleagent` user in Docker).

---

## Phase 6 — Performance Review

### 29. Database performance

- Scan all dashboard routes and agent tools for N+1 query patterns — check joins and eager loading.
- Verify PostgreSQL connection pool sizes are appropriate for expected concurrency in `config/settings.py`.
- Verify indexes exist on frequently queried columns in `includes/dashboard/models.py` — check supplier name, product SKU, RFQ status, email tracking message IDs.
- Check for missing foreign key indexes on join columns.

### 30. Memory & resource usage

- Verify `includes/dashboard/context.py` in-memory context is bounded — is there a TTL, max size, or eviction policy?
- Verify `includes/job_runner.py` ring buffers (200-line output capture) don't grow unbounded for long-running jobs.
- Verify LangGraph checkpointer is not storing excessive state — old checkpoints should be pruned if retention is not needed.
- Verify `public/avatars/` and `data/attachments/` directories have cleanup/pruning — no unbounded disk growth.

### 31. Async patterns & blocking calls

- Scan for sync calls in async contexts — accidental blocking of the event loop.
- Verify all database operations in agent code paths use async sessions where available.
- Verify file I/O in `document_processing.py` and `local_storage_client.py` does not block the event loop for large files.

### 32. Caching opportunities

- Identify expensive operations that are recomputed frequently: currency conversion, supplier matching, domain extraction.
- Verify whether caching is appropriate and, if implemented, whether cache invalidation is correct.

---

## Phase 7 — Code Quality & Consistency

### 33. Type hints audit

- Spot-check public functions across all `includes/` modules for complete type hints on parameters and return values.
- Verify class attributes have type annotations where applicable.

### 34. Error handling audit

- Verify exceptions are caught at appropriate levels — not silently swallowed at the top level, not leaking sensitive internal details to users.
- Verify user-facing error messages are helpful and actionable.
- Verify retry logic (Gemini API, MCP connections, NetSuite API) has reasonable backoff and max retries.

### 35. Logging audit

- Verify Python `logging` is used consistently — no `print()` statements in production code paths.
- Verify log levels are appropriate: `DEBUG` for development, `INFO` for key events, `WARNING` for recoverable issues, `ERROR` for failures.
- Verify sensitive data (emails, tokens, PII) is not logged at `INFO` or `DEBUG` levels.

### 36. Code cleanliness

- Scan for unused imports across the codebase.
- Scan for commented-out code blocks — either remove or add a comment explaining why they're preserved.
- Verify import ordering: stdlib → third-party → local, per PEP 8.
- Verify naming consistency: no single-letter variables except trivial loop indices, descriptive function and class names.
- Verify magic numbers and strings are extracted to named constants — especially in `config/settings.py`.

### 37. Dead code & orphaned files

- Identify any source files not imported by any other file in the codebase.
- Identify any scripts in `scripts/` not registered in `config/scripts.py` — are they intentionally standalone?
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
