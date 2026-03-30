"""Pytest configuration and shared fixtures for EagleAgent tests.

This module provides fixtures for isolated testing with:
- Async PostgreSQL databases
- Local storage paths
- Test stores and checkpointers
- Automatic cleanup
"""

import os
import sys
import tempfile
import pytest

# Add parent directory to path to import project modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from includes.local_storage_client import LocalStorageClient

# ============================================================================
# Environment Detection
# ============================================================================

def is_postgres_running():
    """Check if PostgreSQL is available for testing."""
    return os.environ.get("POSTGRES_DB_URI") is not None

# ============================================================================
# PostgreSQL Fixtures
# ============================================================================

@pytest.fixture
def postgres_connection_string():
    """Get connection string for test database.
    
    Returns:
        str: PostgreSQL connection string
    """
    return os.environ.get("POSTGRES_DB_URI", "postgresql://postgres:postgres@localhost:5432/postgres")


@pytest.fixture
async def test_postgres_pool(postgres_connection_string):
    """Create a temporary connection pool to PostgreSQL test db."""
    from psycopg_pool import AsyncConnectionPool
    
    pool = AsyncConnectionPool(
        conninfo=postgres_connection_string,
        max_size=5,
        kwargs={"autocommit": True},
    )
    # Wait for the pool to be ready
    await pool.wait()
    yield pool
    await pool.close()

# ============================================================================
# Local Storage Fixtures
# ============================================================================

@pytest.fixture
def temp_storage_dir():
    """Provide a temporary directory for local file attachments during testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir

@pytest.fixture
def local_storage_client(temp_storage_dir):
    """Create a LocalStorageClient instance pointed at the temporary directory."""
    return LocalStorageClient(base_dir=temp_storage_dir)

# ============================================================================
# Checkpointer & Store Fixtures
# ============================================================================

@pytest.fixture
async def test_checkpointer(test_postgres_pool):
    """Create an AsyncPostgresSaver checkpointer for testing."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    
    checkpointer = AsyncPostgresSaver(test_postgres_pool)
    # setup the checkpointer tables (assumes standard setup method)
    # await checkpointer.setup() -- in tests you might need async setup if not done globally
    yield checkpointer

@pytest.fixture
def test_store():
    """Create a MemoryStore or BaseStore for testing if a local Postgres store isn't available."""
    from langgraph.store.memory import InMemoryStore
    store = InMemoryStore()
    yield store

# ============================================================================
# Test Data Fixtures
# ============================================================================

@pytest.fixture
def test_user_id():
    """Provide a consistent test user ID."""
    return "test-user@example.com"

@pytest.fixture
def test_user_profile():
    """Provide sample user profile data."""
    return {
        "name": "Test User",
        "preferred_name": "Tester",
        "preferences": ["Python", "Testing"],
        "facts": ["loves automated tests"],
        "job": "QA Engineer",
    }

@pytest.fixture
def test_thread_id():
    """Generate a unique thread ID for each test to ensure test isolation."""
    import uuid
    return f"test-thread-{uuid.uuid4()}"

# ============================================================================
# Utility Fixtures
# ============================================================================

@pytest.fixture
def stub_chat_model():
    """Provide a stubbed chat model for testing without API calls.
    
    Returns:
        class: StubChatModel that returns deterministic responses
    """
    from langchain_core.messages import AIMessage
    
    class StubChatModel:
        """Stub replacement for ChatGoogleGenerativeAI."""
        
        def __init__(self, *args, **kwargs):
            pass
        
        async def ainvoke(self, messages):
            last = messages[-1]
            content = getattr(last, "content", "")
            return AIMessage(content=f"stub-response: {content}")
        
        def bind_tools(self, tools, **kwargs):
            """Support tool binding for compatibility."""
            return self
    
    return StubChatModel

@pytest.fixture(autouse=True)
async def setup_checkpointer(test_checkpointer):
    try: await test_checkpointer.setup()
    except Exception: pass
