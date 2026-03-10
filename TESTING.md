# Testing Guide for EagleAgent

This guide explains how to run tests for EagleAgent using isolated test environments.

## Overview

EagleAgent uses a **dual-database testing strategy**:

1. **PostgreSQL**: In-memory databases (automatic, no setup needed)
2. **PostgreSQL**: Local emulator (requires one-time setup)

This approach ensures:
- ✅ **Zero production impact** - Tests never touch live data
- ✅ **Fast execution** - No network calls to GCP
- ✅ **Offline capability** - Run tests without internet
- ✅ **No cost** - Completely free

---

## Quick Start

### Easiest Way: Use the Test Runner Script

The simplest way to run tests is using the automated test runner:

```bash
# One command - handles everything automatically
uv run pytest
```

That's it! The script will:
- ✅ Check if PostgreSQL emulator is running
- ✅ Start it automatically if needed
- ✅ Set required environment variables
- ✅ Run all tests with proper configuration

You can also pass pytest options:

```bash
uv run pytest --maxfail=1        # Stop after first failure
uv run pytest -k test_profile    # Run only matching tests
uv run pytest -v --tb=short      # Verbose with short tracebacks
```

### Manual Setup (Alternative)

If you prefer to run tests manually or need more control:

#### 1. Install Test Dependencies

```bash
uv sync --group dev
```

This installs:
- `pytest` (9.0.2) - Test framework
- `pytest-asyncio` (1.3.0) - Async test support
- `pytest-timeout` (2.4.0) - Prevent hanging tests

#### 2. Install PostgreSQL Emulator

**Option A: Via gcloud CLI (Recommended)**

```bash
# Install gcloud CLI if needed
# Visit: https://cloud.google.com/sdk/docs/install

# Install the emulator component
gcloud components install cloud-firestore-emulator
```

**Option B: Standalone JAR**

Download from: https://firebase.google.com/docs/emulator-suite/install_and_configure

#### 3. Start the PostgreSQL Emulator

```bash
# Start emulator (leave this running in a terminal)
gcloud emulators firestore start --host-port=localhost:8686
```

You should see:
```
[firestore] To use the emulator, set environment variable:
  export FIRESTORE_EMULATOR_HOST=localhost:8686
```

#### 4. Run Tests Manually

In a **new terminal**:

```bash
# Set the emulator environment variable
export FIRESTORE_EMULATOR_HOST=localhost:8686

# Run all tests
uv run pytest tests/ -v

# Or run specific test files
uv run pytest tests/test_firestore_store.py -v
uv run pytest tests/test_user_profile_tools.py -v
```

**Expected output:**
```
======================= test session starts =======================
... [all tests passed] ...
======================= passed in ~2s =======================
```

---

## Test Organization

```
tests/
├── conftest.py                    # Shared fixtures
├── test_smoke.py                  # Basic sanity test
├── test_graph_wiring.py          # LangGraph wiring with stub
├── test_sqlite_data_layer.py     # PostgreSQL CRUD operations
├── test_firestore_store.py       # User profile storage
├── test_checkpoint_saver.py      # Conversation checkpoints
├── test_user_profile_tools.py    # Agent tools for memory
└── test_integration.py           # End-to-end scenarios
```

**Comprehensive test coverage across the codebase**

### Project Structure

```
EagleAgent/
├── app.py                        # Main application
├── run.sh                        # Start the app
├── kill.sh                       # Stop the app
├── pytest                  # Run tests (automated)
├── includes/                     # Core modules
│   ├── firestore_store.py       # PostgreSQL store implementation
│   ├── timestamped_firestore_saver.py  # Checkpoint saver
│   └── user_profile_tools.py    # User profile tools
├── scripts/                      # Utility scripts
│   ├── init_sqlite_db.py        # Initialize PostgreSQL database
│   ├── clear_checkpoints.py     # Clear checkpoints
│   ├── list_checkpoints.py      # List checkpoints
│   ├── manage_user_profile.py   # Manage user profiles
│   └── ...                      # Other utilities
└── tests/                        # All test files
    ├── conftest.py              # Shared fixtures
    ├── test_*.py                # Individual test modules
    └── ...
```

### Test Categories

**Unit Tests** (fast, isolated):
- `test_firestore_store.py` - PostgreSQLStore operations
- `test_sqlite_data_layer.py` - PostgreSQL schema and CRUD
- `test_checkpoint_saver.py` - Checkpoint save/load
- `test_user_profile_tools.py` - Tool behavior

**Integration Tests** (slower, cross-component):
- `test_integration.py` - Complete workflows
- Marked with `@pytest.mark.integration`

**Slow Tests** (performance/stress tests):
- Marked with `@pytest.mark.slow`
- Run with: `pytest -m slow`

---

## Running Tests

### Using the Test Runner (Recommended)

The `pytest` script handles all setup automatically:

```bash
# Run all tests
uv run pytest

# Run with pytest options
uv run pytest -x                 # Stop on first failure
uv run pytest -v --tb=short      # Verbose with short tracebacks
uv run pytest -k test_firestore  # Run only matching tests
uv run pytest --maxfail=3        # Stop after 3 failures
uv run pytest -m integration     # Run only integration tests
uv run pytest -m "not slow"      # Skip slow tests
```

**What the script does:**
1. Checks if PostgreSQL emulator is running
2. Starts it automatically if not found
3. Waits for emulator to be ready
4. Sets `FIRESTORE_EMULATOR_HOST` environment variable
5. Runs pytest with your options
6. Reports success/failure with clear output

### Manual Test Execution

If you prefer to run tests manually:

#### Run All Tests

```bash
export FIRESTORE_EMULATOR_HOST=localhost:8686
uv run pytest tests/ -v
```

### Run Specific Test File

```bash
uv run pytest tests/test_firestore_store.py -v
```

### Run Specific Test Class

```bash
uv run pytest tests/test_firestore_store.py::TestPostgreSQLStoreBasics -v
```

### Run Specific Test Function

```bash
uv run pytest tests/test_firestore_store.py::TestPostgreSQLStoreBasics::test_put_and_get_simple_value -v
```

### Skip Slow Tests

```bash
uv run pytest tests/ -v -m "not slow"
```

### Run Only Integration Tests

```bash
uv run pytest tests/ -v -m integration
```

### Run with Coverage

```bash
uv run pytest tests/ --cov=. --cov-report=html
```

### Stop on First Failure

```bash
uv run pytest tests/ -x
```

### Show Print Statements

```bash
uv run pytest tests/ -v -s
```

---

## Test Fixtures

### Database Fixtures

Located in `tests/conftest.py`:

#### `temp_sqlite_db`
- Creates a temporary PostgreSQL database
- Automatically initializes schema
- Auto-cleanup after test

```python
async def test_example(temp_sqlite_db):
    # Use temp_sqlite_db path
    pass
```

#### `test_firestore_client`
- Connects to PostgreSQL emulator
- Auto-cleanup all collections after test

```python
def test_example(test_firestore_client):
    # Use client
    pass
```

#### `test_store`
- Pre-configured PostgreSQLStore instance
- Uses test collection (`test_user_memory`)

```python
async def test_example(test_store):
    await test_store.aput(("users",), "user@test.com", {...})
```

#### `test_checkpointer`
- Pre-configured TimestampedPostgreSQLSaver
- Uses test collection (`test_checkpoints`)

```python
async def test_example(test_checkpointer):
    await test_checkpointer.aput(config, checkpoint, metadata)
```

### Data Fixtures

#### `test_user_id`
- Returns: `"test-user@example.com"`

#### `test_user_profile`
- Sample complete user profile

#### `test_thread_id`
- Returns: Unique thread ID per test (e.g., `"test-thread-<uuid>"`)
- Ensures complete test isolation

#### `stub_chat_model`
- Stubbed LLM for testing without API calls
- Returns deterministic responses
- Includes `bind_tools()` method for compatibility

---

## Troubleshooting

### Error: "PostgreSQL emulator not detected"

**Quick Fix**: Use the automated test runner which handles this automatically:

```bash
uv run pytest
```

**Manual Fix**: Make sure the emulator is running and the environment variable is set:

```bash
# Terminal 1: Start emulator
gcloud emulators firestore start --host-port=localhost:8686

# Terminal 2: Set env var and run tests
export FIRESTORE_EMULATOR_HOST=localhost:8686
uv run pytest tests/ -v
```

### Error: "Port already in use"

The emulator is already running. Either:
1. Use the existing instance
2. Stop it: `pkill -f firestore`
3. Use a different port: `--host-port=localhost:8687`

### Tests Hang or Timeout

**Solution**: Check timeout setting in `pyproject.toml`:

```toml
[tool.pytest.init_options]
timeout = 30  # Increase if needed
```

Or run with more time:
```bash
pytest tests/ --timeout=60
```

### Async Fixture Warnings

**Solution**: Already configured in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

### Emulator Data Persists Between Runs

This is intentional for debugging. To clear:

```bash
# Stop emulator
pkill -f firestore

# Start fresh
gcloud emulators firestore start --host-port=localhost:8686
```

---

## Writing New Tests

### Test a PostgreSQL Component

```python
async def test_my_feature(test_store, test_user_id):
    """Test description."""
    # Arrange
    data = {"key": "value"}
    
    # Act
    await test_store.aput(("namespace",), test_user_id, data)
    
    # Assert
    result = await test_store.aget(("namespace",), test_user_id)
    assert result.value == data
```

### Test a PostgreSQL Component

```python
async def test_my_sqlite_feature(temp_sqlite_db):
    """Test description."""
    import aiosqlite
    
    async with aiosqlite.connect(temp_sqlite_db) as db:
        await db.execute("INSERT INTO users ...")
        await db.commit()
        
        cursor = await db.execute("SELECT ...")
        result = await cursor.fetchone()
        assert result is not None
```

### Test with Stubbed LLM

```python
def test_my_graph_feature(stub_chat_model, monkeypatch):
    """Test description."""
    import langchain_google_genai
    
    monkeypatch.setattr(
        langchain_google_genai,
        "ChatGoogleGenerativeAI",
        stub_chat_model,
        raising=True
    )
    
    # Now your code will use the stub instead of real API
    ...
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
      - uses: actions/checkout@v3
      
      - name: Install Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.12'
      
      - name: Install uv
        run: curl -LsSf https://astral.sh/uv/install.sh | sh
      
      - name: Install dependencies
        run: uv sync --group dev
      
      - name: Install gcloud SDK
        uses: google-github-actions/setup-gcloud@v1
      
      - name: Install PostgreSQL Emulator
        run: gcloud components install cloud-firestore-emulator --quiet
      
      - name: Run Tests
        run: uv run pytest
```

**Note**: The `pytest` script handles starting the emulator and setting environment variables automatically, simplifying your CI/CD pipeline.

### Manual CI/CD Setup (Alternative)

If you prefer manual control in CI/CD:

```yaml
      - name: Start PostgreSQL Emulator
        run: |
          gcloud emulators firestore start --host-port=localhost:8686 > /tmp/emulator.log 2>&1 &
          sleep 5
      
      - name: Run Tests
        env:
          FIRESTORE_EMULATOR_HOST: localhost:8686
        run: uv run pytest tests/ -v --cov
```

---

## Performance Tips

### Run Tests in Parallel

```bash
# Install plugin
uv add pytest-xdist --group dev

# Run with 4 workers
uv run pytest tests/ -n 4
```

### Cache Test Results

```bash
# Only run tests that failed last time
uv run pytest tests/ --lf

# Run failed tests first, then others
uv run pytest tests/ --ff
```

### Profile Slow Tests

```bash
# Show 10 slowest tests
uv run pytest tests/ --durations=10
```

---

## Best Practices

✅ **DO:**
- Use fixtures for test data
- Clean up after each test (handled by fixtures)
- Write descriptive test names
- Use `@pytest.mark` for organization
- Test both success and error cases
- Use provided fixtures like `test_thread_id` for proper test isolation

❌ **DON'T:**
- Access production databases in tests
- Hardcode credentials
- Leave tests hanging (use timeouts)
- Test external APIs (use stubs/mocks)
- Share state between tests (always use unique IDs from fixtures)

---

## FAQ

**Q: What's the easiest way to run tests?**  
A: Use `uv run pytest` - it handles everything automatically including starting the PostgreSQL emulator if needed.

**Q: Do I need to restart the emulator between test runs?**  
A: No, the test fixtures handle cleanup automatically. The `pytest` script will reuse an existing emulator instance.

**Q: Can I run tests without the emulator?**  
A: No, PostgreSQL tests require the emulator. PostgreSQL tests will still run.

**Q: How do I debug a failing test?**  
A: Use `uv run pytest -v -s` to see print statements, or add `pytest.set_trace()` for breakpoints.

**Q: Can I use the same emulator for development and testing?**  
A: Yes, but be careful - tests clean up all data after running.

**Q: How do I test against real PostgreSQL?**  
A: Don't. Use the emulator for tests. For E2E testing, use a separate staging environment.

**Q: Why do tests use unique thread IDs?**  
A: Each test gets a unique thread ID (via `test_thread_id` fixture) to ensure complete isolation. This prevents data contamination between tests running in the same session.

**Q: Can I pass pytest options to the test runner?**  
A: Yes! Use `uv run pytest <pytest-options>`. Examples: `uv run pytest -x`, `uv run pytest -k test_name`, `uv run pytest -m integration`.

---

## Additional Resources

- [pytest Documentation](https://docs.pytest.org/)
- [PostgreSQL Emulator Guide](https://firebase.google.com/docs/emulator-suite/connect_firestore)
- [pytest-asyncio](https://pytest-asyncio.readthedocs.io/)
- [LangGraph Testing](https://langchain-ai.github.io/langgraph/how-tos/testing/)
