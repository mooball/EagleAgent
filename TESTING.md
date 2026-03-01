# Testing Guide for EagleAgent

This guide explains how to run tests for EagleAgent using isolated test environments.

## Overview

EagleAgent uses a **dual-database testing strategy**:

1. **SQLite**: In-memory databases (automatic, no setup needed)
2. **Firestore**: Local emulator (requires one-time setup)

This approach ensures:
- ✅ **Zero production impact** - Tests never touch live data
- ✅ **Fast execution** - No network calls to GCP
- ✅ **Offline capability** - Run tests without internet
- ✅ **No cost** - Completely free

---

## Quick Start

### 1. Install Test Dependencies

```bash
uv sync --group dev
```

This installs:
- `pytest` - Test framework
- `pytest-asyncio` - Async test support
- `pytest-timeout` - Prevent hanging tests

### 2. Install Firestore Emulator

#### Option A: Via gcloud CLI (Recommended)

```bash
# Install gcloud CLI if needed
# Visit: https://cloud.google.com/sdk/docs/install

# Install the emulator component
gcloud components install cloud-firestore-emulator
```

#### Option B: Standalone JAR

Download from: https://firebase.google.com/docs/emulator-suite/install_and_configure

### 3. Start the Firestore Emulator

```bash
# Start emulator (leave this running in a terminal)
gcloud emulators firestore start --host-port=localhost:8686
```

You should see:
```
[firestore] To use the emulator, set environment variable:
  export FIRESTORE_EMULATOR_HOST=localhost:8686
```

### 4. Run Tests

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

---

## Test Organization

```
tests/
├── conftest.py                    # Shared fixtures
├── test_smoke.py                  # Basic sanity test
├── test_graph_wiring.py          # LangGraph wiring with stub
├── test_sqlite_data_layer.py     # SQLite CRUD operations
├── test_firestore_store.py       # User profile storage
├── test_checkpoint_saver.py      # Conversation checkpoints
├── test_user_profile_tools.py    # Agent tools for memory
└── test_integration.py           # End-to-end scenarios
```

### Test Categories

**Unit Tests** (fast, isolated):
- `test_firestore_store.py` - FirestoreStore operations
- `test_sqlite_data_layer.py` - SQLite schema and CRUD
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

### Run All Tests

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
uv run pytest tests/test_firestore_store.py::TestFirestoreStoreBasics -v
```

### Run Specific Test Function

```bash
uv run pytest tests/test_firestore_store.py::TestFirestoreStoreBasics::test_put_and_get_simple_value -v
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
- Creates a temporary SQLite database
- Automatically initializes schema
- Auto-cleanup after test

```python
async def test_example(temp_sqlite_db):
    # Use temp_sqlite_db path
    pass
```

#### `test_firestore_client`
- Connects to Firestore emulator
- Auto-cleanup all collections after test

```python
def test_example(test_firestore_client):
    # Use client
    pass
```

#### `test_store`
- Pre-configured FirestoreStore instance
- Uses test collection (`test_user_memory`)

```python
async def test_example(test_store):
    await test_store.aput(("users",), "user@test.com", {...})
```

#### `test_checkpointer`
- Pre-configured TimestampedFirestoreSaver
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
- Returns: `"test-thread-12345"`

#### `stub_chat_model`
- Stubbed LLM for testing without API calls
- Returns deterministic responses

---

## Troubleshooting

### Error: "Firestore emulator not detected"

**Solution**: Make sure the emulator is running and the environment variable is set:

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

### Test a Firestore Component

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

### Test a SQLite Component

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
      
      - name: Install Firestore Emulator
        run: |
          wget -q https://storage.googleapis.com/firebase-preview-drop/emulator/cloud-firestore-emulator-*.jar
          java -version
      
      - name: Start Firestore Emulator
        run: |
          gcloud emulators firestore start --host-port=localhost:8686 &
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

❌ **DON'T:**
- Access production databases in tests
- Hardcode credentials
- Leave tests hanging (use timeouts)
- Test external APIs (use stubs/mocks)
- Share state between tests

---

## FAQ

**Q: Do I need to restart the emulator between test runs?**  
A: No, the test fixtures handle cleanup automatically.

**Q: Can I run tests without the emulator?**  
A: No, Firestore tests require the emulator. SQLite tests will still run.

**Q: How do I debug a failing test?**  
A: Use `pytest -v -s` to see print statements, or add `pytest.set_trace()` for breakpoints.

**Q: Can I use the same emulator for development and testing?**  
A: Yes, but be careful - tests clean up all data after running.

**Q: How do I test against real Firestore?**  
A: Don't. Use the emulator for tests. For E2E testing, use a separate staging environment.

---

## Additional Resources

- [pytest Documentation](https://docs.pytest.org/)
- [Firestore Emulator Guide](https://firebase.google.com/docs/emulator-suite/connect_firestore)
- [pytest-asyncio](https://pytest-asyncio.readthedocs.io/)
- [LangGraph Testing](https://langchain-ai.github.io/langgraph/how-tos/testing/)
