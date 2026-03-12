# Testing Guide for EagleAgent

This guide explains how to run tests for EagleAgent.

## Overview

EagleAgent uses **PostgreSQL** for all persistence (checkpoints, cross-thread store, data layer). Tests use mocks and in-memory stores by default, so **no running database is required** for the standard test suite.

This approach ensures:
- ✅ **Zero production impact** — Tests never touch live data
- ✅ **Fast execution** — No network calls or external services
- ✅ **Offline capability** — Run tests without internet
- ✅ **No cost** — Completely free

---

## Quick Start

```bash
# Install dev dependencies
uv sync --group dev

# Run all tests
uv run pytest

# Common options
uv run pytest -v --tb=short      # Verbose with short tracebacks
uv run pytest -x                 # Stop on first failure
uv run pytest -k test_prompts    # Run only matching tests
uv run pytest -m integration     # Run only integration tests
uv run pytest -m "not slow"      # Skip slow tests
```

---

## Test Organization

```
tests/
├── conftest.py                    # Shared fixtures (stores, storage, user data)
├── test_smoke.py                  # Basic sanity tests
├── test_graph_wiring.py           # LangGraph wiring with stub model
├── test_prompts.py                # System prompt building
├── test_roles.py                  # Role-based access control
├── test_file_attachments.py       # File upload/processing
├── test_mcp_integration.py        # MCP tool integration
├── test_integration.py            # End-to-end scenarios
├── agents/
│   ├── test_browser_agent.py      # BrowserAgent + browser tools
│   ├── test_general_agent.py      # GeneralAgent (tools, prompts, execution)
│   └── test_supervisor.py         # Supervisor routing logic
└── tools/
    └── test_user_profile.py       # User profile management tools
```

### Test Categories

**Unit Tests** (fast, isolated):
- `test_prompts.py` — Prompt building and templating
- `test_roles.py` — Admin/staff role logic
- `agents/test_general_agent.py` — GeneralAgent tools, prompts, execution
- `agents/test_browser_agent.py` — BrowserAgent and browser tool mocking
- `agents/test_supervisor.py` — Routing decisions
- `tools/test_user_profile.py` — Profile tool behavior

**Integration Tests** (slower, cross-component):
- `test_integration.py` — Complete graph workflows
- `test_graph_wiring.py` — LangGraph compilation and wiring
- `test_mcp_integration.py` — MCP server tool loading
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
- Requires a running PostgreSQL instance (`POSTGRES_DB_URI` env var)
- Only used for database-specific integration tests

### Storage Fixtures

#### `temp_storage_dir` / `local_storage_client`
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
from includes.agents.general_agent import GeneralAgent

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
