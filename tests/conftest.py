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

from includes.chat.local_storage_client import LocalStorageClient

# ============================================================================
# Chat Context Fixtures
# ============================================================================
# FakeChatContext lives here rather than in a tests/ module because a `tests`
# package in site-packages shadows any `tests.*` import.


class FakeMessageHandle:
    """Records streaming/update/remove instead of performing them."""

    def __init__(self, content="", *, actions=None, author=None, transient=False):
        import uuid

        self.id = str(uuid.uuid4())
        self.content = content
        self.actions = list(actions or [])
        self.author = author
        self.transient = transient
        self.tokens: list[str] = []
        self.updated = 0
        self.removed = False
        self.persisted = False
        # Set by tests to simulate a dead socket.
        self.fail_on_update = False

    async def stream(self, token: str) -> None:
        self.tokens.append(token)
        self.content += token

    async def update(self) -> None:
        if self.fail_on_update:
            raise RuntimeError("socket closed")
        self.updated += 1

    async def remove(self) -> None:
        self.removed = True

    async def save(self) -> None:
        try:
            await self.update()
        except RuntimeError:
            self.persisted = True


class FakeChatContext:
    """In-memory ChatContext. Everything it is asked to do is recorded."""

    def __init__(
        self,
        *,
        thread_id="thread-abc",
        user_email="tester@example.com",
        agent="eagle",
        state=None,
        cancelled=False,
    ):
        self.thread_id = thread_id
        self.user_email = user_email
        self.agent = agent
        self.active_message = None
        self._state = dict(state or {})
        self._cancelled = cancelled

        self.messages: list[FakeMessageHandle] = []
        self.images: list[tuple[str, str]] = []
        self.dashboard_calls: list[tuple[str, dict | None]] = []
        self.thread_names: list[str] = []

    async def say(self, text, *, actions=None, author=None, transient=False):
        handle = FakeMessageHandle(text, actions=actions, author=author, transient=transient)
        self.messages.append(handle)
        return handle

    async def image(self, path: str, *, name: str) -> None:
        self.images.append((path, name))

    async def notify_dashboard(self, command: str, payload: dict | None = None) -> None:
        self.dashboard_calls.append((command, payload))

    async def rename_thread(self, name: str) -> None:
        self.thread_names.append(name)

    def get(self, key, default=None):
        return self._state.get(key, default)

    def set(self, key, value) -> None:
        self._state[key] = value

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def request_stop(self) -> None:
        self._cancelled = True

    def reset_cancel(self) -> None:
        self._cancelled = False

    # -- assertion helpers --

    @property
    def texts(self) -> list[str]:
        return [m.content for m in self.messages]

    @property
    def action_names(self) -> list[str]:
        return [a.name for m in self.messages for a in m.actions]


@pytest.fixture
def make_chat_ctx():
    """Factory for FakeChatContext, so tests can vary thread_id/agent/etc."""
    return FakeChatContext


@pytest.fixture
def chat_ctx():
    """A transport-free ChatContext that records everything it is asked to do."""
    return FakeChatContext()


@pytest.fixture
def bound_chat_ctx(chat_ctx):
    """`chat_ctx`, also bound to the ContextVar for the duration of the test."""
    from includes.chat.context import chat_context

    with chat_context(chat_ctx):
        yield chat_ctx


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
