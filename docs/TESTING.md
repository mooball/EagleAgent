# Testing Guide for EagleAgent

This guide explains how to run tests for EagleAgent.

## Overview

EagleAgent uses **PostgreSQL** for all persistence (checkpoints, cross-thread store, data layer). Most tests use mocks and in-memory stores so they run fast without external services. A subset of tests (e.g., `test_database_matching.py`, `test_dashboard_routes.py`) require a running PostgreSQL instance — start it with `./start_postgres.sh` before running the full suite.

This approach ensures:
- ✅ **Zero production impact** — Tests never touch live data
- ✅ **Fast execution** — Most tests use in-memory mocks
- ✅ **Offline capability** — Mock-based tests run without internet
- ✅ **No cost** — Completely free

---

## Quick Start

```bash
# Start PostgreSQL (required for full test suite)
./start_postgres.sh

# Install dev dependencies
uv sync --group dev

# Run all tests
uv run pytest

# Common options
uv run pytest -v --tb=short      # Verbose with short tracebacks
uv run pytest -x                 # Stop on first failure
uv run pytest -k test_prompts    # Run only matching tests
uv run pytest -m "not slow"      # Skip slow tests
uv run pytest -m "not slow"      # Skip slow tests
```

---

## Test Organization

```
tests/
├── conftest.py                    # Shared fixtures (stores, storage, user data, postgres pool)
├── test_smoke.py                  # Basic sanity tests
├── test_graph_wiring.py           # LangGraph wiring with stub model
├── test_prompts.py                # System prompt building
├── test_roles.py                  # Role-based access control
├── test_settings.py               # Config/settings validation
├── test_actions.py                # Action button registry/dispatcher
├── test_file_attachments.py       # File upload/processing
├── test_mcp_integration.py        # MCP tool integration
├── test_integration.py            # End-to-end scenarios
├── test_job_runner.py             # Background job runner
├── test_job_tools.py              # Job management LangGraph tools
├── test_humanize_timestamp.py     # Timestamp formatting for dashboard
├── test_dashboard_context.py      # Dashboard context store
├── test_dashboard_routes.py       # Dashboard route handlers
├── test_main_auth.py              # FastAPI OAuth & session tests
├── agents/
│   ├── test_browser_agent.py      # BrowserAgent + browser tools
│   ├── test_general_agent.py      # GeneralAgent (tools, prompts, execution)
│   ├── test_procurement_agent.py  # ProcurementAgent tools and routing
│   └── test_supervisor.py         # Supervisor routing logic
└── tools/
    ├── test_user_profile.py       # User profile management tools
    ├── test_product_tools.py      # Product/supplier search tools
    └── test_quote_tools.py        # RFQ/quote workflow tools
```

### Test Categories

**Unit Tests** (fast, isolated):
- `test_prompts.py` — Prompt building and templating
- `test_roles.py` — Admin/staff role logic
- `test_settings.py` — Config/settings validation
- `test_actions.py` — Action button registry and dispatcher
- `test_humanize_timestamp.py` — Dashboard timestamp formatting
- `test_dashboard_context.py` — Dashboard context store
- `agents/test_general_agent.py` — GeneralAgent tools, prompts, execution
- `agents/test_browser_agent.py` — BrowserAgent and browser tool mocking
- `agents/test_procurement_agent.py` — ProcurementAgent tools
- `agents/test_supervisor.py` — Routing decisions
- `tools/test_user_profile.py` — Profile tool behavior
- `tools/test_product_tools.py` — Product/supplier search tools
- `tools/test_quote_tools.py` — RFQ/quote workflow tools

**Integration Tests** (slower, cross-component):
- `test_integration.py` — Complete graph workflows
- `test_graph_wiring.py` — LangGraph compilation and wiring
- `test_mcp_integration.py` — MCP server tool loading
- `test_dashboard_routes.py` — Dashboard route handlers (requires test app)
- `test_main_auth.py` — FastAPI OAuth and session middleware
- `test_job_runner.py` — Background job subprocess management
- `test_job_tools.py` — Job management tools
- Marked with `@pytest.mark.integration`

**Slow Tests** (performance/stress):
- Marked with `@pytest.mark.slow`
- Run with: `uv run pytest -m slow`

---

## Test Fixtures

Located in `tests/conftest.py`:

### Database Fixtures

#### `test_store`
- In-memory store (no PostgreSQL needed)
- Used for user profile and cross-thread memory tests

```python
async def test_example(test_store):
    await test_store.aput(("users",), "user@test.com", {"name": "Tom"})
    result = await test_store.aget(("users",), "user@test.com")
    assert result.value["name"] == "Tom"
```

#### `test_postgres_pool` / `test_checkpointer`
- Requires a running PostgreSQL instance (local or via `./start_postgres.sh`)
- Used by most tests via `conftest.py`
- Pool connects to `localhost:5432` by default

### Storage Fixtures

#### `temp_storage_dir`
- Temporary directory for file attachment tests
- Auto-cleanup after test

### Data Fixtures

#### `test_user_id`
- Returns: `"test-user@example.com"`

---

## Running Tests

### All Tests

```bash
uv run pytest tests/ -v
```

### Specific Test File

```bash
uv run pytest tests/agents/test_general_agent.py -v
```

### Specific Test Class

```bash
uv run pytest tests/agents/test_general_agent.py::TestGetToolsAsync -v
```

### Specific Test Function

```bash
uv run pytest tests/agents/test_general_agent.py::TestGetToolsAsync::test_includes_mcp_tools -v
```

### With Coverage

```bash
uv run pytest tests/ --cov=. --cov-report=html
```

### Show Print Statements

```bash
uv run pytest tests/ -v -s
```

---

## Manual End-to-End Testing (Local)

Automated tests cover the units; some flows also need a real run against your
local database. The hard one is the **Gmail add-on**, because the add-on posts to
production (`BACKEND_URL = https://agent.eaglexp.com.au` in `addon/Code.gs`) and
`/api/addon/*` is gated behind a Google OIDC token that only Apps Script can
mint. Clicking the add-on therefore can never reach your local server.

`scripts/test_rfq_creation.py` solves this by replaying the **"Create RFQ + OP"**
flow in-process: it calls the real `addon.create_rfq()` route function with the
same body the add-on sends (`{gmail_message_id, gmail_thread_id}`), so every
guard and write runs exactly as it does in production — minus HTTP and auth.

### 🛡 NetSuite writes are blocked by default

There is no NetSuite sandbox in this codebase, and `Config.NETSUITE_ACCOUNT_ID`
defaults to `794882` — **production**. Both the add-on route and the create-RFQ
pipeline call `create_and_link_opportunity()`, which writes a real Opportunity.

So the script **intercepts that call by default** and reports it as
`🛡 intercepted`. Nothing reaches NetSuite unless you explicitly pass
`--allow-netsuite`, which you should not need for local testing.

> **Why it works this way.** An earlier version made blocking opt-in via
> `--no-netsuite`, and combined with `--reset` falling through to a trigger that
> was a real hazard: a command that read like "just clean up after the last run"
> silently created live opportunities in production NetSuite. Fail closed.

`--no-netsuite` is still accepted as a no-op for compatibility with older notes,
but it is no longer needed.

### Getting test data in

Either pull real mail into the local database:

```bash
# From your own mailbox (recommended — attachments resolve from Gmail on demand)
uv run python -m scripts.sync_gmail_mailboxes --user you@eagle-exports.com

# Or copy emails + RFQs from production
uv run python -m scripts.sync_prod_mail_data --limit 50
```

### The workflow

Both `uv run python -m scripts.test_rfq_creation` and
`uv run python scripts/test_rfq_creation.py` work — the script bootstraps the
repo root onto `sys.path`, so running it as a plain file is fine too.

```bash
# 1. List recent emails and how ready each is (read-only)
uv run python -m scripts.test_rfq_creation --recent 10
uv run python -m scripts.test_rfq_creation --recent 20 --search RFQ

# 2. Inspect one email: customer linked? already processed?
uv run python -m scripts.test_rfq_creation --email-id 42844

# 3. Link a customer if the route's guard complains (id or name fragment)
uv run python -m scripts.test_rfq_creation --email-id 42844 --link-customer "Ranger"

# 4. Preview the whole thing without writing anything
uv run python -m scripts.test_rfq_creation --email-id 42844 --dry-run

# 5. Run it — creates the RFQ, watches the pipeline. NetSuite writes stay blocked.
uv run python -m scripts.test_rfq_creation --email-id 42844 --yes

# 6. Iterate. --reset is STANDALONE: it stops after cleaning up, and does NOT
#    create an RFQ. Run step 5 again separately to replay.
uv run python -m scripts.test_rfq_creation --email-id 42844 --reset --yes
```

**Actions that stop rather than run:** `--recent`, `--dry-run`, `--reset`. Only a
plain "inspect or run" invocation reaches the trigger.

The script refuses to run against a non-local `DATABASE_URL` unless you pass
`--yes`, because it **writes** (creates RFQs, links emails, resets guards).

### Watching the agent-working lock

Step 4 prints the RFQ number and its dashboard URL as soon as the RFQ exists.
Open that URL, then watch the run in the terminal:

```
  → RFQ created: RFQ-2026-1234
    Open http://localhost:8000/rfqs/RFQ-2026-1234 now to watch the banner.

  [09:32:23] step=extracting_items  status=processing
  [09:33:23] step=updating_details  status=processing
  [09:33:25] pipeline finished — status=complete, items=2
  [09:33:25] lock: cleared   items on RFQ: 2
```

In the browser you should see: the blue banner at the top of the RFQ Items tab,
no Add/Edit/Delete controls, then — once the run finishes — the banner vanishing
on its own and the extracted lines appearing (the page polls itself).

To confirm the server-side guard rather than just the hidden buttons, run this in
the browser console while the banner is up:

```javascript
htmx.ajax('POST', '/partial/rfqs/RFQ-2026-1234/add-item',
          {target: '#main-content', values: {input_description: 'should fail'}})
```

It should toast a 409 rather than adding a line. See
`.github/prompts/plan-rfqAgentWorkingLock.prompt.md` for how the lock works.

### Reproducing extraction failures on demand

Some failures only happen upstream at random. The original motivating case was
Gemini returning `500 INTERNAL` for one PDF in a 10-attachment email, which
silently produced an RFQ one line short — no error in the UI, the pipeline notes
or the RFQ. Retrying cannot reproduce it, because the failure is transient.

`--inject-attachment-failure` makes a named attachment report as unreadable so
the failure path can be exercised deterministically. Everything downstream is the
real code path: the bundle report, `rfq_creation_result["input"]`, the human
warning line, and the "Attachments Read" block in the comms modal.

```bash
# 1. Reset first (standalone) — 49663 is the 10-attachment email carrying
#    `estimate QBRI1207.pdf`, which is the attachment that originally 500'd.
uv run python -m scripts.test_rfq_creation --email-id 49663 --reset --yes

# 2. Replay the flow with that one attachment forced to fail.
uv run python -m scripts.test_rfq_creation --email-id 49663 \
    --inject-attachment-failure "estimate QBRI1207.pdf:model_error" --yes
```

Expected terminal output at the end of the run:

```
  [09:41:12] pipeline finished — status=complete, items=1
             warning: 1 attachment(s) could not be read: estimate QBRI1207.pdf
             input:       9 of 10 attachments read (9 skipped as signature)
             unreadable:  estimate QBRI1207.pdf [model_error] injected by ...
```

In the comms modal the **Attachments Read** row goes amber and reads
`9 of 10 (9 skipped as signature)`, with the warning listed underneath.

Syntax is `FILENAME[:CODE]`, repeatable. `CODE` defaults to `model_error`; use
`*:CODE` to fail every attachment. Valid codes:

| Code | Meaning |
|---|---|
| `model_error` | upstream call failed (transient — the original bug) |
| `parse_error` | returned content that could not be parsed |
| `empty` | read successfully but yielded nothing |
| `fetch_failed` | could not retrieve the attachment bytes |
| `unsupported` | file type never handled (a deterministic gap) |

The script refuses a filename the email does not have (and lists the real ones),
and refuses an unknown code — a typo should not "pass" while testing nothing.

**Where this lives:** it is a test-only monkeypatch *inside*
`scripts/test_rfq_creation.py`. No production module gains an `if TESTING` branch
and no environment variable can enable it in the deployed app. It patches the
extractors **as bound in `supplier_quote_pipeline`** (the caller's references),
not the definitions in `email_pipeline` — patching the definitions would replace
a name nobody looks up and silently do nothing.

Not covered: `bundle_failure` codes (`no_content`, `email_not_found`) fail before
the attachment loop and are not reachable this way.

For a faster, non-destructive check of just the extraction step (creates no RFQ,
resets nothing), `_probe_bundle.py` in the repo root drives the same patcher:

```bash
uv run python -m scripts.probe_content_bundle 49663
uv run python -m scripts.probe_content_bundle 49663 --inject "estimate QBRI1207.pdf:model_error"
```

### Notes and gotchas

- **The pipeline runs in a daemon thread.** If the script exits immediately, the
  run is killed mid-flight, leaving a half-populated RFQ and a stuck lock. The
  script therefore polls until the run reaches a terminal state. `--no-watch`
  disables that — only use it if you know why.
- **Re-runs are blocked by design.** `rfq_creation_result` / `rfq_token` are
  idempotency guards; `--reset` deletes the previous RFQ and clears them for the
  whole email *thread*.
- **`--reset` reuses the same RFQ number.** Numbering is `max+1`, so deleting the
  RFQ makes the next run take the *same* number. A browser tab still showing the
  old RFQ will therefore share a URL with the new run — close or reload it, or
  you'll be looking at a stale page and concluding the lock didn't engage.
- **`--reset` does not touch NetSuite.** If a previous run created a real
  opportunity, delete it in NetSuite by hand.
- **Attachments** are fetched from Gmail on demand by the extraction step, so a
  message must still exist in a mailbox your local Gmail credentials can read.
  Prod-synced rows may not resolve attachments.
- **`--direct`** skips the add-on route and lets the pipeline create the RFQ
  itself, exercising the other lock path (and not checking the customer guard).

---

## Writing New Tests

### Test a Store Component

```python
async def test_my_feature(test_store, test_user_id):
    """Test description."""
    data = {"key": "value"}
    await test_store.aput(("namespace",), test_user_id, data)

    result = await test_store.aget(("namespace",), test_user_id)
    assert result.value == data
```

### Test with Mocked LLM

```python
from unittest.mock import Mock, AsyncMock
from langchain_core.messages import AIMessage

def test_my_feature():
    mock_model = Mock()
    mock_model.ainvoke = AsyncMock(return_value=AIMessage(content="Hello"))
    mock_model.bind_tools = Mock(return_value=mock_model)

    # Use mock_model in place of ChatGoogleGenerativeAI
    ...
```

### Test an Agent

```python
from includes.agents import GeneralAgent

@pytest.mark.asyncio
async def test_agent_behavior():
    mock_model = Mock()
    mock_model.ainvoke = AsyncMock(return_value=AIMessage(content="Done"))
    mock_model.bind_tools = Mock(return_value=mock_model)

    agent = GeneralAgent(model=mock_model, store=None)
    state = {"messages": [HumanMessage(content="Hello")], "user_id": ""}
    result = await agent(state)
    assert "messages" in result
```

### Mark as Slow or Integration

```python
@pytest.mark.slow
async def test_heavy_operation():
    """This test takes a while."""
    pass

@pytest.mark.integration
async def test_end_to_end_flow():
    """Tests multiple components together."""
    pass
```

---

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - name: Install Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install uv
        run: curl -LsSf https://astral.sh/uv/install.sh | sh

      - name: Install dependencies
        run: uv sync --group dev

      - name: Run Tests
        run: uv run pytest tests/ -v
```

---

## Performance Tips

```bash
# Only re-run tests that failed last time
uv run pytest tests/ --lf

# Run failed tests first, then others
uv run pytest tests/ --ff

# Show 10 slowest tests
uv run pytest tests/ --durations=10
```

---

## Troubleshooting

### Tests Hang or Timeout

Check timeout setting in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
timeout = 30  # Increase if needed
```

Or run with more time:
```bash
uv run pytest tests/ --timeout=60
```

### Async Fixture Warnings

Already configured in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

---

## Best Practices

✅ **DO:**
- Use fixtures for test data
- Clean up after each test (handled by fixtures)
- Write descriptive test names
- Use `@pytest.mark` for organization
- Test both success and error cases
- Use mocks/stubs for LLM calls and external services

❌ **DON'T:**
- Access production databases in tests
- Hardcode credentials
- Leave tests hanging (use timeouts)
- Test external APIs without mocking
- Share state between tests

---

## Additional Resources

- [pytest Documentation](https://docs.pytest.org/)
- [pytest-asyncio](https://pytest-asyncio.readthedocs.io/)
- [LangGraph Testing](https://langchain-ai.github.io/langgraph/how-tos/testing/)
