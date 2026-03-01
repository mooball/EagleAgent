"""Pytest configuration and shared fixtures for EagleAgent tests.

This module provides fixtures for isolated testing with:
- In-memory SQLite databases
- Firestore emulator client
- Test stores and checkpointers
- Automatic cleanup
"""

import os
import sys
import tempfile
import pytest
from google.cloud import firestore

# Add parent directory to path to import project modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from firestore_store import FirestoreStore
from timestamped_firestore_saver import TimestampedFirestoreSaver


# ============================================================================
# Environment Detection
# ============================================================================

def is_firestore_emulator_running():
    """Check if Firestore emulator is available."""
    emulator_host = os.environ.get("FIRESTORE_EMULATOR_HOST")
    return emulator_host is not None


@pytest.fixture(scope="session", autouse=True)
def check_firestore_emulator():
    """Verify Firestore emulator is running before tests start."""
    if not is_firestore_emulator_running():
        pytest.skip(
            "Firestore emulator not detected. Start it with:\n"
            "  export FIRESTORE_EMULATOR_HOST=localhost:8686\n"
            "  gcloud emulators firestore start --host-port=localhost:8686"
        )


# ============================================================================
# SQLite Fixtures
# ============================================================================

@pytest.fixture
async def temp_sqlite_db():
    """Create a temporary SQLite database for testing.
    
    Yields:
        str: Path to temporary database file
    """
    # Create a temporary file
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    
    # Initialize with schema
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        
        # Create minimal schema for testing
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                "id" TEXT PRIMARY KEY,
                "identifier" TEXT NOT NULL UNIQUE,
                "metadata" TEXT NOT NULL,
                "createdAt" TEXT
            );
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS threads (
                "id" TEXT PRIMARY KEY,
                "createdAt" TEXT,
                "name" TEXT,
                "userId" TEXT,
                "userIdentifier" TEXT,
                "tags" TEXT,
                "metadata" TEXT,
                FOREIGN KEY ("userId") REFERENCES users("id") ON DELETE CASCADE
            );
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS steps (
                "id" TEXT PRIMARY KEY,
                "name" TEXT NOT NULL,
                "type" TEXT NOT NULL,
                "threadId" TEXT NOT NULL,
                "parentId" TEXT,
                "streaming" INTEGER NOT NULL,
                "waitForAnswer" INTEGER,
                "isError" INTEGER,
                "metadata" TEXT,
                "tags" TEXT,
                "input" TEXT,
                "output" TEXT,
                "createdAt" TEXT,
                "start" TEXT,
                "end" TEXT,
                "generation" TEXT,
                "showInput" TEXT,
                "language" TEXT,
                "defaultOpen" INTEGER DEFAULT 0,
                FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
            );
        """)
        
        await db.commit()
    
    yield db_path
    
    # Cleanup
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture
def sqlite_connection_string(temp_sqlite_db):
    """Get SQLAlchemy connection string for test database.
    
    Args:
        temp_sqlite_db: Temporary database path
        
    Returns:
        str: SQLAlchemy connection string
    """
    return f"sqlite+aiosqlite:///{temp_sqlite_db}"


# ============================================================================
# Firestore Fixtures
# ============================================================================

@pytest.fixture
def test_firestore_client():
    """Create a Firestore client connected to the emulator.
    
    Yields:
        firestore.Client: Client connected to emulator
    """
    # Ensure we're using the emulator
    if not is_firestore_emulator_running():
        pytest.skip("Firestore emulator not running")
    
    # Create client (will automatically use FIRESTORE_EMULATOR_HOST)
    client = firestore.Client(project="test-project", database="(default)")
    
    yield client
    
    # Cleanup: Delete all test collections
    # Note: This is safe because we're in the emulator
    collections = client.collections()
    for collection in collections:
        delete_collection(collection, batch_size=100)


def delete_collection(collection_ref, batch_size):
    """Delete all documents in a collection (for emulator cleanup)."""
    docs = collection_ref.limit(batch_size).stream()
    deleted = 0
    
    for doc in docs:
        doc.reference.delete()
        deleted += 1
    
    if deleted >= batch_size:
        return delete_collection(collection_ref, batch_size)


@pytest.fixture
def test_store(test_firestore_client):
    """Create a FirestoreStore for testing.
    
    Args:
        test_firestore_client: Firestore client fixture
        
    Yields:
        FirestoreStore: Store instance for testing
    """
    # Create store with test collection
    store = FirestoreStore(project_id="test-project", collection="test_user_memory")
    
    yield store
    
    # Cleanup is handled by test_firestore_client fixture


@pytest.fixture
def test_checkpointer(test_firestore_client):
    """Create a TimestampedFirestoreSaver for testing.
    
    Args:
        test_firestore_client: Firestore client fixture
        
    Yields:
        TimestampedFirestoreSaver: Checkpointer for testing
    """
    checkpointer = TimestampedFirestoreSaver(
        project_id="test-project",
        checkpoints_collection="test_checkpoints"
    )
    
    yield checkpointer
    
    # Cleanup is handled by test_firestore_client fixture


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
    """Provide a consistent test thread ID."""
    return "test-thread-12345"


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
        
        def bind_tools(self, tools):
            """Support tool binding for compatibility."""
            return self
    
    return StubChatModel
