# May 2026 Code Review — Issues & Recommendations

## 1. Code Organisation

### 1.1 `app.py` is a 1,660-line monolith — HIGH ✅ DONE

`app.py` handles too many concerns in a single file:
- Data layer class + middleware classes (lines 55–295)
- LangGraph model/graph construction (lines 298–460)
- Auth callback, chat profiles, on_chat_start, on_chat_resume (lines 464–810)
- 7 action callbacks for RFQ operations: `rfq_refresh`, `rfq_update_supplier`, `rfq_identify_items`, `rfq_find_suppliers` (lines 887–1252) — these are substantial (150+ lines each for identify/find)
- Main message handler (lines 1253–end)

**Recommendation:** Extract to focused modules:
- `includes/chat/rfq_actions.py` — move the 4 RFQ action callbacks (`rfq_refresh`, `rfq_update_supplier`, `rfq_identify_items`, `rfq_find_suppliers`)
- `includes/chat/data_layer.py` — move `FixedSQLAlchemyDataLayer`
- `includes/chat/middleware.py` — move `OAuthErrorRedirectMiddleware`, `_GeminiRetryNotifier`
- `includes/graph.py` — move `create_model()`, `SupervisorState`, graph construction, `setup_globals()`

This would bring `app.py` down to ~400 lines: auth, lifecycle hooks, and message handler.

### 1.2 `includes/dashboard/routes.py` is 1,972 lines — HIGH ✅ DONE

Every dashboard route is in one file. The routes naturally group into:
- **Core/auth** (~100 lines): `require_user`, `require_role`, `_render`, `_is_htmx`
- **Supplier routes** (~500 lines): list, detail, partials, update, contacts, comments
- **Product routes** (~250 lines): list, detail, partials
- **Transaction routes** (~250 lines): list, partials, rows
- **RFQ routes** (~550 lines): list, detail, new, update, items, suppliers, price history
- **Admin routes** (~150 lines): users, jobs, scripts, netsuite status
- **API endpoints** (~150 lines): latest-thread, rfq-thread, dashboard-context

**Recommendation:** Split into route modules under `includes/dashboard/`:
- `routes/__init__.py` — router + shared helpers (`require_user`, `_render`, etc.)
- `routes/suppliers.py`
- `routes/products.py`
- `routes/transactions.py`
- `routes/rfqs.py`
- `routes/admin.py`
- `routes/api.py`

### 1.3 `includes/tools/quote_tools.py` is 1,499 lines — MEDIUM ✅ DONE

This file contains the `manage_rfq` / `get_rfq` tool functions, plus 15+ private `_sync` CRUD helpers, supplier matching/enrichment logic, and RFQ rendering. The tool factory is only ~200 lines — the rest is supporting code.

**Recommendation:** Extract:
- `includes/tools/rfq_crud.py` — all `_*_sync` CRUD functions (~700 lines)
- `includes/tools/rfq_render.py` — `_render_rfq_summary`, `_render_rfq_list` (~200 lines)
- Keep supplier-matching/enrichment in `quote_tools.py` (it's tightly coupled to the tool logic)

### 1.4 Minor naming issues — LOW

- `includes/prompts.py` contains intents, tool instructions, profile templates, AND system prompt builder. Consider renaming to `includes/agent_config.py` or splitting intents into their own module.
- `includes/chat/actions.py` vs the action callbacks in `app.py` — confusing. The module handles intent dispatch; the callbacks in `app.py` handle button clicks. Should be consolidated.
- `includes/agent_bridge.py` (singular) vs `includes/agents/` (plural folder) — minor inconsistency.

---

## 2. Test Coverage

### 2.1 Test counts: 412 passing, 1 failing

Overall health is good: 26 test files, 412 passing tests.

### 2.2 Failing test: `test_domain_match_different_name` — HIGH ✅ DONE

This test runs against the **real database** and finds a production "Repco Export & Wholesale" record (netsuite_id='6079') before the test fixture's record. The `db_session` fixture uses `begin_nested()` / SAVEPOINT to roll back, but doesn't isolate from existing production data.

**Root cause:** The domain-first scan returns the first matching supplier from the DB. Production data has a real Repco row, so the test assertion `result.id == sup.id` fails because it matched the production row instead.

**Fix:** The `db_session` fixture in `test_database_matching.py` needs to either:
1. Use a truly isolated test schema/DB, or
2. Filter the scan to only find test-inserted rows (harder), or
3. Use a unique test domain that doesn't exist in production (simplest fix)

### 2.3 Modules with no direct test file — MEDIUM ✅ DONE (partial)

| Source module | Test coverage |
|--------------|---------------|
| `includes/agent_bridge.py` | No dedicated test file |
| `includes/agents/research_agent.py` | No test file (others have tests) |
| `includes/agents/sysadmin_agent.py` | No test file |
| `includes/chat/commands.py` | No test file |
| `includes/chat/document_processing.py` | No test file |
| `includes/chat/job_progress.py` | No test file |
| `includes/chat/local_storage_client.py` | Tested indirectly via `test_file_attachments.py` |
| `includes/dashboard/database.py` | Tested via `test_database_matching.py` (partial — only match functions) |
| `includes/dashboard/models.py` | No dedicated test file (tested indirectly) |
| `includes/mcp_config.py` | Tested via `test_mcp_integration.py` |
| `includes/netsuite/*` | No test files for auth, client, queries, constants |
| `includes/supplier_categorization.py` | No test file |
| `includes/tools/action_tools.py` | No test file |
| `includes/tools/browser_tools.py` | No test file |
| `config/scripts.py` | No test file |

The biggest gaps are `agent_bridge.py`, `document_processing.py`, and `netsuite/*` — these handle real I/O so are harder to test but also higher risk.

### 2.4 No test isolation from production DB — MEDIUM

Several test files (especially `test_database_matching.py`, `test_dashboard_routes.py`, `test_rfq_enrichment.py`) use the real PostgreSQL database. While they use SAVEPOINT rollback to avoid persisting changes, they can see production data which causes the failing test above and could cause flaky tests in future.

**Recommendation:** Consider using a dedicated test database or schema, or at minimum, filter test queries to avoid collisions with production data.

---

## 3. LLM, Agent & Prompt Organisation

### 3.1 Agent architecture is clean — OK

The multi-agent structure under `includes/agents/` is well-organised:
- `base.py` (458 lines) — shared agent creation logic, tool binding
- `supervisor.py` — orchestrator
- `procurement_agent.py`, `general_agent.py`, `research_agent.py`, `browser_agent.py`, `sysadmin_agent.py` — specialist agents

Each agent is focused and reasonably sized. The README in the agents folder documents the architecture.

### 3.2 Prompt definitions are centralised but growing — MEDIUM ✅ DONE

`includes/prompts.py` (880 lines) contains:
- `AGENT_CONFIG` — identity/personality/company info
- `TOOL_INSTRUCTIONS` — per-tool usage guidance
- `PROFILE_TEMPLATES` — user profile context formatting
- `INTENTS` / `RESEARCH_INTENTS` — command button definitions with follow-up text + context prompts
- `build_system_prompt()` — assembles the full system prompt
- `format_profile_section()` — formats user profile for context

This is still manageable but getting large. The intent definitions (`new_rfq`, `find_supplier`, etc.) now contain substantial multi-paragraph prompt text that mixes UI concerns (follow_up messages) with agent instructions (context).

**Recommendation:** Consider splitting:
- `includes/prompts/config.py` — AGENT_CONFIG, TOOL_INSTRUCTIONS
- `includes/prompts/intents.py` — INTENTS, RESEARCH_INTENTS
- `includes/prompts/builder.py` — build_system_prompt(), format_profile_section()

### 3.3 Intent context is prompt-only with no validation — LOW

Intent contexts like `new_rfq` instruct the agent to use specific tool actions (`manage_rfq(action='update', ...)`) but there's no runtime validation that the agent follows these instructions. If the LLM ignores the prompt, the wrong action executes silently. This is inherent to prompt-based systems, but worth noting.

### 3.4 Model configuration is hardcoded — LOW ✅ DONE

`create_model()` in `includes/graph.py` now uses `Config.get_agent_model()`, `Config.DEFAULT_TEMPERATURE`, and `Config.DEFAULT_MAX_TOKENS`. Hardcoded model strings in `quote_tools.py`, `deduplicate_brands.py`, and `categorize_suppliers.py` replaced with `Config.DEFAULT_MODEL`.

---

## 4. Library Versions

### 4.1 Major updates available — HIGH ✅ DONE (except google-genai v2)

| Package | Current | Latest | Delta | Risk |
|---------|---------|--------|-------|------|
| `google-genai` | 1.68.0 | **2.2.0** | Major | High — v2 may have breaking API changes |
| `chainlit` | 2.9.6 | **2.11.1** | Minor | Medium — 2 minor versions behind; may include bug fixes for known issues |
| `langchain` | 1.2.10 | **1.3.0** | Minor | Medium — new features, possible deprecation changes |
| `langchain-core` | 1.2.16 | **1.4.0** | Minor | Medium — paired with langchain |
| `langgraph` | 1.0.9 | **1.2.0** | Minor | Medium — checkpoint format changes possible |
| `langgraph-checkpoint-postgres` | 3.0.4 | **3.1.0** | Minor | Low-Medium — checkpoint storage |

### 4.2 Safe minor updates — LOW risk ✅ DONE

| Package | Current | Latest | Notes |
|---------|---------|--------|-------|
| `langchain-google-genai` | 4.2.1 | 4.2.2 | Patch — should be safe |
| `langchain-mcp-adapters` | 0.2.1 | 0.2.2 | Patch |
| `fastapi` | 0.133.1 | 0.136.1 | 3 minor versions, generally safe |
| `sqlalchemy` | 2.0.47 | 2.0.49 | Patch |
| `psycopg` | 3.3.3 | 3.3.4 | Patch |
| `pillow` | 12.1.1 | 12.2.0 | Minor |
| `pandas` | 3.0.1 | 3.0.3 | Patch |
| `rapidfuzz` | 3.14.3 | 3.14.5 | Patch |
| `greenlet` | 3.3.2 | 3.5.0 | Minor — used by SQLAlchemy |

### 4.3 Recommendation

1. **Immediate:** Update all patch-level packages (4.2 table) — these are low-risk bug fixes.
2. **Test carefully:** Update `chainlit` 2.9.6 → 2.11.1, `fastapi` → 0.136.1, `langchain*` → 1.3.0 stack, `langgraph*` → 1.2.0 stack. Do this as a single coordinated update with full test run.
3. **Evaluate separately:** `google-genai` 1.x → 2.x is a major version bump. Review the changelog for breaking changes before attempting. This may also require `langchain-google-genai` adjustments.

---

## 5. Summary of Actionable Items

| # | Issue | Priority | Effort |
|---|-------|----------|--------|
| 1 | ✅ Split `app.py` — extract RFQ action callbacks, data layer, middleware, graph setup | High | Medium |
| 2 | ✅ Split `routes.py` into route modules | High | Medium |
| 3 | ✅ Fix failing `test_domain_match_different_name` (production data collision) | High | Low |
| 4 | ✅ Update patch-level dependencies | High | Low |
| 5 | ✅ Split `quote_tools.py` — extract CRUD helpers and renderers | Medium | Medium |
| 6 | ✅ Test coverage gaps — added tests for `scripts`, `netsuite`, `document_processing`, `supplier_categorization` | Medium | Medium |
| 7 | Test DB isolation — dedicated test schema or DB | Medium | Medium |
| 8 | ✅ Update `chainlit` 2.11.1 + `langchain` 1.2.18 + `langgraph` 1.1.10 + `fastapi` 0.136.1 | Medium | Medium |
| 9 | ✅ Split `prompts.py` into `prompts/` package (config, intents, builder) | Low | Low |
| 10 | ✅ Move model config to settings — all hardcoded model strings now use `Config.DEFAULT_MODEL` | Low | Low |
| 11 | Evaluate `google-genai` v2 migration | Low | High |
