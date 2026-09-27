# EagleAgent Codebase Review — 2026-09-27

> Review conducted against the process defined in `plan-fullCodebaseReview.prompt.md` (updated this session to drop the removed Chainlit architecture).
> Previous review: `codebaseReview-2026-07-25.md`. All 7 phases complete.

## Phase 1 — Dependency & Version Health Audit

### Task 1: Audit Python dependencies

**Lockfile consistency**: `uv lock --check` — ✅ passed (241 packages resolved, no drift).

**Pinning style**: 1 violation — `google-auth>=2.0` (unbounded). All other production deps use `~=`. ⚠️

**Dev dependencies**: `[dependency-groups].dev` correct (`pytest`, `pytest-asyncio`, `pytest-cov`, `pytest-timeout`, `pip-audit`). ✅

**Security scanning** (`pip-audit`): **32 known vulnerabilities in 12 packages**.

| Package | Version | CVEs | Fix | Type |
|---|---|---|---|---|
| aiohttp | 3.14.1 | 4 | 3.14.3 | transitive |
| anyio | 4.12.1 | 2 | 4.14.2 | transitive |
| pyasn1 | 0.6.3 | 6 | 0.6.4 | transitive |
| python-engineio | 4.13.1 | 4 | 4.13.2 | **via chainlit** |
| python-socketio | 5.16.1 | 2 | 5.16.2 | **via chainlit** |
| httplib2 | 0.31.2 | 2 | 0.32.0 | transitive |
| cryptography | 49.0.0 | 1 | 50.0.0 | transitive |
| click | 8.3.1 | 1 | 8.3.3 | transitive |
| msgpack | 1.2.0 | 1 | 1.2.1 | transitive |
| pydantic-settings | 2.13.1 | 1 | 2.14.2 | transitive |
| pytest | 8.4.2 | 1 | 9.0.3 | dev-only |
| pip | 26.1.2 | 1 | 26.2 | tooling, not app |

**⚠️ `chainlit` is still a declared dependency** (`chainlit~=2.11.1` in `pyproject.toml`) even though the chat UI is in-house. Only `tests/chat/conftest.py` touches it (`import chainlit.data as cl_data`). It is dead weight and the sole source of the 6 `python-engineio`/`python-socketio` CVEs above. Removing it (and the stale test support) is the single highest-value dependency change.

**Deprecated API usage**:
- `create_react_agent` → `langchain.agents.create_agent` — `includes/agents/base.py:408,412` (LangGraphDeprecatedSinceV10).
- SQLAlchemy legacy `Query.get()` — 10+ sites (see Phase 7).

**Direct dependencies behind current** (selected): `fastapi` 0.136.1→0.141.1, `alembic` 1.18.4→1.20.0, `fastapi-sso` 0.21.0→0.23.0, `langchain` 1.3.14→1.4.2, `langgraph` 1.2.9→1.2.12, `pgvector` 0.4.2→0.5.0, `pypdfium2` 5.5.0→5.13.0, `uvicorn` 0.41.0→0.54.0, `google-api-python-client` 2.197→2.200. Major jumps needing care: `google-genai` 1.68→**2.25**, `mcp` 1.28.1→**2.2.0**, `sqlalchemy` 2.0.49→**2.1.1**, `pytest-asyncio` 0.26→**1.4.0**.

### Task 2: Audit container & infra versions

| Component | Current | Status |
|---|---|---|
| Python base image | `python:3.12-slim` | ✅ floating tag |
| Node.js | `22.x LTS` (Dockerfile) | ✅ |
| agent-browser | `@0.16.3` (pinned) | ✅ |
| Tailwind CLI | v4.3.3 standalone (CSS-first) | ✅ |
| pgvector image | `pgvector/pgvector:0.8.0-pg17` | ⚠️ still 0.8.0 — the July review recorded a bump to 0.9.2 that is **not** in `docker-compose.yml` (or the running container) |
| `.python-version` | `3.12` | ✅ matches pyproject + Dockerfile |

### Task 3: Audit database migrations

**`alembic check` — ❌ FAILED**, and the database **is** at head (`d1e2f3a4b5c6`), so this is genuine model↔migration drift, not a stale DB. Detected operations:

- remove table `chat_ui_current_threads` (in DB/migration `a7f3c9d2e1b4`, absent from models)
- remove index `ix_llm_call_log_ts_model_status` (present in DB, absent from models)
- remove indexes `idx_products_part_number_norm`, `idx_products_part_number_norm_ci`, `idx_products_supplier_code_norm`, `idx_products_supplier_code_norm_ci`
- remove index `ix_sdc_status` (supplier_duplicate_candidates)
- remove indexes `ix_smk_name_trgm`, `ix_smk_supplier_id`, `ix_smk_type_value` (supplier_match_keys)
- add FK `rfqs.quote_brand_id → brands.id`
- add index `ix_supplier_match_keys_supplier_id`

**Risk**: a future `alembic revision --autogenerate` would emit destructive `DROP TABLE`/`DROP INDEX` operations. Several indexes are created in migrations via raw SQL and not declared on the ORM models (and vice versa). Recommendation: declare indexes on the models, or add an `include_object`/`compare_index` filter in `alembic/env.py`, so autogenerate is trustworthy.

---

## Phase 2 — Code Structure & Architecture Review

### Task 4: Review top-level entry points

- `main.py` (558 lines) delegates correctly (OAuth, session, static mounts, background loops, chat router include). ✅
- **Stale docstring** (`main.py:1-6`): still says *"mounts Chainlit at /chat. Dashboard UI will be added in Phase 2."*
- **Root clutter — 25 files**: 24 scratch scripts (`_diag_*` ×8, `_probe_*` ×8, `_test_*` ×3, `_verify_*` ×2, `_find_*`, `_retest_*`) plus `docker-compose.yml.bak-20260927-102135`. Should move to a gitignored `temp_files/` (already in `.gitignore`) or be deleted.
- `public/tailwind.min.css` is generated and untracked ✅.

### Task 5: Review `includes/graph.py`

- `setup_globals()` idempotent; three graphs compiled; wiring correct. ✅
- **Silently swallowed startup failure** (`graph.py:121-125`):
  ```python
  try:
      await pg_pool.open()
  except Exception:
      pass
  ```
  The next call (`store.setup()`) would fail anyway, so swallowing only hides the real error. Fail loudly instead.
- `ADMIN_ONLY_TOOLS` is empty (kept as a registration point) — fine, but verify it stays documented.

### Task 6: Review agents — `includes/agents/`

- All sub-agents extend `BaseSubAgent` (`base.py`, 506 lines). ✅
- `create_react_agent` used at `base.py:408,412` (deprecated — see Phase 1).
- `BrowserAgent` module present but excluded from the main graph (intentional).
- `registry.py` remains the single definition point. ✅
- ⚠️ `research_agent.py` and `sysadmin_agent.py` still have **no dedicated tests** (open since June).

### Task 7: Review chat modules — `includes/chat/`

Transport-neutral design intact: business logic depends on `ChatContext` (`context.py`); `context_sse.py` is the only implementation. ✅

| Module | Lines | Notes |
|---|---|---|
| `rfq_actions.py` | 977 | largest chat module |
| `transcript.py` | 647 | app-owned async engine |
| `runner.py` | 478 | `run_turn()` + per-thread locks |
| `widgets.py` | — | no `innerHTML`/`| safe`; rendered via templates ✅ |

- **`astream_events` only in `runner.py`** ✅ (no bypass).
- `document_processing.py` enforces `MAX_FILE_SIZE_MB`. ✅
- `middleware.py`, `job_progress.py`, `supplier_search_gate.py`, `streaming_logic.py` all present and cohesive.

### Task 8: Review dashboard — `includes/dashboard/`

- **Auth coverage**: 158 route decorators vs 171 `require_user`/`require_role` usages — every route appears guarded. ✅
- `context.py` has a 30-minute TTL with lazy + periodic eviction. ✅
- New supplier domain modules (`supplier_contacts.py`, `supplier_create.py`, `supplier_dedup.py` 606, `supplier_matching.py`, `supplier_widget.py` 641) and `email_uploads.py` are cohesive.
- **Monolith routes**: `routes/rfqs.py` **4,221 lines**, `routes/admin.py` **1,777**, `routes/chat_ui.py` **1,437**. These are the hardest files to review, edit, and test safely.

### Task 9: Review tools — `includes/tools/`

- `@tool` decoration and admin filtering intact.
- **Monolith tools**: `rfq_crud.py` **3,799**, `product_tools.py` **1,725**, `quote_tools.py` **1,321**, `supplier_quote_pipeline.py` **1,103**, `rfq_item_import.py` **827**.
- New since July: `rfq_creation_pipeline.py` (602), `rfq_item_import.py`, `supplier_search_tools.py` (527), `comms_summary.py`.

### Task 10: Review prompts — `includes/prompts/`

- `config.py`, `builder.py` (471), `intents.py` present and cohesive. ✅

### Task 11: Review the LLM layer — `includes/llm/` (new since July)

- `telemetry.py` (663) writes `llm_call_log`; token reconciliation and model-ladder fallback recorded. ✅
- `context.py` provides the interactive-vs-background workload context. ✅
- Tests disable telemetry by default (`tests/conftest.py`). ✅

### Task 12: Review integrations

- `gmail/matching.py` (529), `gmail/draft_service.py` (639), `netsuite/*` (incl. `records/item.py` 503) — covered by tests. ✅
- `hubspot/__init__.py` now has a holding-pattern docstring + skeleton; still not wired in — decide to implement or drop the dependency. ⚠️

### Task 13: Review static assets

- 144 template files; only `templates/login.html` looked orphaned but is rendered by `main.py:327`. ✅
- **Orphan assets**: `public/favicon.png`, `public/logo_dark.png`, `public/logo_light.png` have no references (only `eagle-icon.png` is used, in `base.html:83`). ⚠️
- Preline vendored `4.2.0` (2026-08-19). ✅
- Only one `| safe` in templates — see Phase 5.

---

## Phase 3 — Documentation Audit

### Task 14: Project-level documentation

- `README.md`: accurate; **all 20 docs linked**. ✅
- `AGENTS.md`: added this cycle (OpenCode instructions). ✅
- `copilot-instructions.md`: now a near-duplicate of `AGENTS.md` — **drift risk**. Recommend making one canonical and pointing the other at it.
- `main.py` docstring stale (Phase 2).

### Task 15: Architecture documentation — `docs/` (20 files)

- 19 of 20 accurate. Remaining Chainlit mentions in `CHAT_UI.md`, `AGENT_BRIDGE.md`, `RFQ_WORKFLOW.md`, `GOOGLE_OAUTH_SETUP.md` are **intentional history notes** ("this replaced Chainlit") — acceptable. ✅
- ⚠️ `FILE_ATTACHMENTS.md` (lines 66-67, 200) still claims *"No server-side accept-list or size cap enforced today"*, but `document_processing.py:332` enforces `MAX_FILE_SIZE_MB`. Stale/misleading.
- ⚠️ `FUTURE_AGENT_PLANNING.md` still headed *"Current State (May 2025)"* — flagged in June, still unfixed.

### Task 16: Configuration documentation

- **⚠️ `.env.example` is missing 23 of the 61 variables `config/settings.py` reads**, including: `ADMIN_EMAILS`, `DEBUG`, `LOG_LEVEL`, `TIMEZONE`, `DEFAULT_MAX_TOKENS`, `DEFAULT_TEMPERATURE`, `MAX_HISTORY_TOKENS`, `FALLBACK_MODEL`, `FALLBACK_CHAIN`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GMAIL_ALLOW_DOMAINS`, `EMAIL_ATTACHMENT_MAX_MB`, `EMAIL_ATTACHMENT_TOTAL_MB`, `EMAIL_UPLOAD_TTL_HOURS`, `HUBSPOT_ACCESS_TOKEN`, and all five `NETSUITE_*` (`ACCOUNT_ID`, `CLIENT_ID`, `CERTIFICATE_ID`, `PRIVATE_KEY_B64`, `SYNC_BATCH_SIZE`, `SYNC_ENABLED`, `SYNC_INTERVAL`).
- `config/mcp_servers.yaml.example` present and valid.

### Task 17: Inline documentation

- **TODO/FIXME/HACK in app code: 0** ✅ (excellent discipline maintained).

---

## Phase 4 — Testing & Coverage Audit

### Task 18: Test infrastructure

```
1848 passed, 19 skipped, 7 deselected, 1866 warnings in 84.07s
```

- **0 failures.** ✅
- 85 test files, 1,764 test functions, 28,236 test LOC vs 38,612 source LOC → **test:source ratio 0.73** (up from 0.42 in July).
- pytest config: `asyncio_mode = "auto"`, `timeout = 30`, markers `slow`/`integration`. ✅
- ⚠️ **No CI** (`.github/workflows/` absent), no pre-commit hook — this suite runs only when someone remembers.

### Task 18b: Deprecation warnings

| Warning | Location | Action |
|---|---|---|
| `create_react_agent` moved to `langchain.agents` | `includes/agents/base.py:408,412` | ⚠️ open since June — migrate to `create_agent` |
| SQLAlchemy `Query.get()` legacy | `rfq_creation_pipeline.py:291`, `netsuite/records/opportunity.py:139`, `addon.py:790`, + others | switch to `Session.get()` |
| `AsyncConnectionPool` constructor deprecated | `tests/conftest.py` | use `open=False` + `await open()` |

Total warnings: **1,866**.

### Task 19: Skipped tests

19 skips; the recurring reason is `"app.py removed with Chainlit; retarget to includes.graph"`. These are **dead tests** referencing the removed entry point — retarget or delete.

### Task 20: Test coverage gaps

**Only 4 source modules have no test references** (down from 13 in July):

| Module | Risk |
|---|---|
| `includes/agents/research_agent.py` | Medium — open since June |
| `includes/agents/sysadmin_agent.py` | Medium — admin/script surface, open since June |
| `includes/netsuite/countries.py` | Low |
| `includes/system_settings.py` | Low |

Also: **no linter, formatter, or type-checker** is configured (only `[tool.pytest.ini_options]`), and `ruff` is not installed.

---

## Phase 5 — Security Review

### Task 21: Secrets & credentials

- No hardcoded secrets in `includes/`. ✅
- `.env`, `*.pem`, `service-account-key*.json`, `config/mcp_servers.yaml` all gitignored and untracked. ✅
- `.env.example` placeholders only. ✅

### Task 22: Injection & input validation — ⚠️ **stored XSS confirmed**

`templates/partials/_rfq_email_suppliers.html:317` renders:
```jinja
{{ body_html | safe }}
```
`body_html` is populated from **inbound email HTML**, not server-generated text:
- `includes/tools/supplier_quote_pipeline.py:111` → `tracking.body_html = content["body_html"]` (from `fetch_message_content(service, gmail_message_id)`)
- `includes/dashboard/routes/rfqs.py:3408` → `"body_html": tracking.body_html`

The July 2026 review closed this as a **false positive**; that determination is incorrect. Unsanitized external HTML is rendered with `| safe` in the RFQ email-suppliers partial. Blast radius is admin-only, but it is a genuine stored-XSS vector. Recommend sanitizing (`nh3`/`bleach`) or rendering as text.

Everything else: dashboard SQL is parameterized (SQLAlchemy ORM) ✅; SuiteQL injection fixed in July (numeric validation) ✅.

### Task 23: Authentication & authorization

- Google OAuth via `fastapi-sso`; session `max_age` 15 days, `same_site="lax"`, `https_only=not DEBUG`. ✅
- Role checks consistent across routes, chat actions, and tools. ✅

### Task 24: File upload security

- Size enforced in `document_processing.py:332` (`MAX_FILE_SIZE_MB`, default 50). ✅
- Upload route (`chat_ui.py:984-993`) sanitizes with `os.path.basename(...)` and prefixes `{user_id}/{element_id}/` — traversal mitigated. ✅
- Note: the upload route writes to disk **before** `document_processing` size-checks; a large non-processed upload is only bounded by the web server. Minor.

### Task 25: Subprocess execution

- `job_runner.py` uses `asyncio.create_subprocess_exec` (list form), `get_script()` allowlist, `validate_args()`. ✅ Non-root in Docker. ✅

---

## Phase 6 — Performance Review

### Task 26: Database performance

- **N+1 still present** in `includes/dashboard/routes/rfqs.py`: `session.query(Contact)` runs inside loops over items/suppliers (around lines 172 and 243), plus extra `Transaction` queries per supplier. 20–50+ queries per RFQ detail render. ⚠️ (open since July; still "acceptable at current scale," but the file has grown since).
- Connection pool `min_size=1, max_size=10` with keepalives. ✅

### Task 27: Memory & resource usage

- Dashboard context: 30-min TTL ✅.
- Maintenance loop prunes checkpoints (`CHECKPOINT_RETENTION_DAYS=90`) and attachments (`ATTACHMENT_RETENTION_DAYS=90`) every 24h. ✅
- Job runner: 200-line ring buffer. ✅
- ⚠️ **Gmail credentials cache still has no TTL** (`includes/gmail/__init__.py:47`) — open since June; only refreshes when `creds.expired`. Revoked access persists until restart.

### Task 28: Async patterns

- No `time.sleep()` in async paths; slow work is wrapped in `asyncio.to_thread`. ✅
- `print()` count in `includes/`: **4** (small, down from 18 in July).

### Task 29: Caching

- Currency: 24h TTL ✅. Taxonomy, prompts, CSS hash cached. ✅
- Supplier/brand lists and the Gmail domain index remain uncached (low priority at current scale).

---

## Phase 7 — Code Quality & Consistency

### Task 30: Type hints

- Broad annotations present, but **no type-checker enforces them**; a heuristic scan found ~239 public functions without an inline return annotation (inflated by multi-line signatures). Add `mypy`/`pyright` (or `ruff` rules) to make this enforceable rather than aspirational.

### Task 31: Error handling

- `except Exception:` — **80 occurrences** in `includes/`. Most return error dicts, but several **silently swallow** with `pass`:
  - `includes/graph.py:125` (pool open — see Phase 2)
  - `includes/dashboard/routes/addon.py:247,261,278,300,324`
  - `includes/dashboard/routes/chat_ui.py:128`
  - `includes/email_pipeline.py:656`
  - `includes/chat/context_sse.py:351`
  - `includes/tools/rfq_creation_pipeline.py:201`
  (`includes/llm/telemetry.py:327,342` are intentional and `# noqa`-annotated.)
- Retry logic present in `BaseSubAgent` and LLM layer. ✅

### Task 32: Logging

- `logging` used consistently; only 4 `print()` remain in `includes/`.

### Task 33: Code cleanliness

- 0 TODO/FIXME/HACK in app code. ✅
- SQLAlchemy legacy `Query.get()` at 10+ sites (see deprecations).
- Naming/imports broadly consistent.

### Task 34: Dead code & orphaned files

- 24 root scratch scripts + 1 `.bak` file (Phase 2).
- 3 orphan public assets (Phase 2).
- 5 skipped tests referencing the removed `app.py` (Phase 4).
- `chainlit` dependency + `tests/chat/conftest.py` support (Phase 1).
- 61 of 74 `scripts/` files are intentionally standalone (not in the 13-entry `SCRIPT_REGISTRY`). ✅

---

## Critical Issues (must fix before next release)
- [ ] **Stored XSS**: inbound email HTML rendered with `| safe` — `templates/partials/_rfq_email_suppliers.html:317` (source: `includes/tools/supplier_quote_pipeline.py:111`, `includes/dashboard/routes/rfqs.py:3408`). Sanitize or render as text.

## Warnings (should fix soon)
- [ ] **`chainlit` still a declared dependency** — dead weight; sole source of 6 CVEs (`python-engineio`, `python-socketio`). Remove it and the stale `tests/chat/conftest.py` support.
- [ ] **32 CVEs in 12 packages** — bump `aiohttp`→3.14.3, `anyio`→4.14.2, `cryptography`→50.0.0, `pyasn1`→0.6.4, `httplib2`→0.32.0, `click`→8.3.3, `msgpack`→1.2.1, `pydantic-settings`→2.14.2 (and remove the chainlit transitive CVEs).
- [ ] **`alembic check` fails** — model/migration drift (DB at head). Reconcile models vs migrations so autogenerate can't emit destructive drops.
- [ ] **`.env.example` missing 23 of 61 settings** — add NetSuite, LLM fallback, admin/logging, attachment, and HubSpot vars.
- [ ] **Root clutter** — 24 `_diag_*`/`_probe_*`/`_test_*`/`_verify_*` scratch scripts + `docker-compose.yml.bak-*` at repo root.
- [ ] **`graph.py` swallows pool-open failure** (`graph.py:121-125`) — fail loudly.
- [ ] **No CI / no linter / no type-checker** — add a workflow running the suite + `ruff`; consider pre-commit.
- [ ] **Monolith files** — `routes/rfqs.py` (4,221), `tools/rfq_crud.py` (3,799), `routes/admin.py` (1,777).
- [ ] **`create_react_agent` deprecation** (`agents/base.py:408,412`) — migrate to `create_agent`.
- [ ] **Gmail credentials cache has no TTL** (`gmail/__init__.py:47`) — open since June.
- [ ] **N+1 in RFQ detail view** (`routes/rfqs.py` ~172, ~243) — open since July.
- [ ] **`research_agent.py` / `sysadmin_agent.py` have no tests** — open since June.
- [ ] **5 skipped tests reference removed `app.py`** — retarget or delete.
- [ ] **pgvector image still 0.8.0** — July recorded a 0.9.2 bump not present in `docker-compose.yml`.

## Suggestions (nice to have)
- [ ] Pin `google-auth~=2.x` (only remaining unbounded pin).
- [ ] Sanitize the `| safe` email HTML with `nh3` (Rust, fast) and note it in `FILE_ATTACHMENTS.md`.
- [ ] Update `FILE_ATTACHMENTS.md` (size-cap claim) and `FUTURE_AGENT_PLANNING.md` (May 2025 date).
- [ ] Resolve `AGENTS.md` vs `copilot-instructions.md` duplication (make one canonical).
- [ ] Replace SQLAlchemy legacy `Query.get()` with `Session.get()`.
- [ ] Delete orphan assets (`favicon.png`, `logo_dark.png`, `logo_light.png`) or reference them.
- [ ] Migrate `tests/conftest.py` off the deprecated `AsyncConnectionPool` constructor.
- [ ] Decide HubSpot: implement or drop `hubspot-api-client`.
- [ ] Review the 80 `except Exception:` handlers; narrow the silent `pass` ones.
- [ ] Plan major dep upgrades carefully: `google-genai` 1→2, `mcp` 1→2, `sqlalchemy` 2.0→2.1, `pytest-asyncio` 0→1.

## Test Coverage Summary
- Test files: **85** (1,764 test functions, 1,874 collected)
- Tests passing: **1,848** (0 failing)
- Tests skipped: 19 (5 reference removed `app.py`)
- Source modules with no direct test coverage: **4**
- Test:source ratio: **0.73** (July: 0.42)
- Highest-risk gaps: `agents/research_agent.py`, `agents/sysadmin_agent.py`

## Documentation Status
- Docs reviewed: 20 (`docs/`) + README + AGENTS.md + copilot-instructions.md
- Docs needing updates: 3 (`FILE_ATTACHMENTS.md`, `FUTURE_AGENT_PLANNING.md`, `main.py` docstring)
- Missing documentation topics: none major; `.env.example` coverage is the gap

## Codebase Metrics
| Metric | June 2026 | July 2026 | Sept 2026 | Change |
|---|---|---|---|---|
| Source LOC (`includes/`) | ~18,000 | 21,989 | 38,612 | +76% |
| Test LOC (`tests/`) | ~6,500 | 9,135 | 28,236 | +209% |
| Test files | ~30 | 39 | 85 | +46 |
| Tests | 511 | 715 | 1,874 | +1,159 |
| Tests failing | 5 | 1 | **0** | ✅ |
| Test:source ratio | 0.36 | 0.42 | **0.73** | ✅ |
| CVEs found | 8 | 40 | 32 | ⚠️ 6 from chainlit |
| Broad `except Exception:` | ~20 | ~131 | 80 | ↓ |
| Root scratch files | 0 | 0 | **25** | ⚠️ new |
| Docs | 14 | 19 | 20 | +1 |
| Lint/type/CI | none | none | **none** | ⚠️ |

## Action Items (priority order)
1. [ ] **CRITICAL**: Sanitize/escape `body_html` (stored XSS) — `_rfq_email_suppliers.html` + source.
2. [ ] **HIGH**: Remove the `chainlit` dependency and stale test support.
3. [ ] **HIGH**: Resolve `pip-audit` CVEs (bump aiohttp/anyio/cryptography/pyasn1/httplib2/click/msgpack/pydantic-settings).
4. [ ] **HIGH**: Reconcile Alembic models/migrations so `alembic check` passes.
5. [ ] **HIGH**: Add CI (pytest + ruff) — the suite is strong but unenforced.
6. [ ] **MEDIUM**: Clean up root scratch/backup files.
7. [ ] **MEDIUM**: Fix `graph.py` silent pool-open swallow.
8. [ ] **MEDIUM**: Complete `.env.example` (23 missing vars).
9. [ ] **MEDIUM**: Decompose `routes/rfqs.py` and `tools/rfq_crud.py`.
10. [ ] **MEDIUM**: Migrate `create_react_agent` → `create_agent`.
11. [ ] **MEDIUM**: Add TTL/invalidation to the Gmail credentials cache.
12. [ ] **MEDIUM**: Write tests for `research_agent.py` and `sysadmin_agent.py`.
13. [ ] **LOW**: Remove/retarget the 5 `app.py` skipped tests.
14. [ ] **LOW**: Fix stale docs (`FILE_ATTACHMENTS.md`, `FUTURE_AGENT_PLANNING.md`, `main.py` docstring).
15. [ ] **LOW**: Resolve `AGENTS.md`/`copilot-instructions.md` duplication.
16. [ ] **LOW**: Pin `google-auth`; bump pgvector image; tidy orphan assets.
