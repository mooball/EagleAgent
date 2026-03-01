"""Tests for SQLite data layer (Chainlit conversation history).

Tests the SQLite database that stores UI conversation history.
"""

import pytest
import aiosqlite
import uuid
from datetime import datetime


class TestSQLiteSchema:
    """Test SQLite database schema and setup."""
    
    async def test_database_created(self, temp_sqlite_db):
        """Test that database file is created."""
        import os
        assert os.path.exists(temp_sqlite_db)
    
    async def test_users_table_exists(self, temp_sqlite_db):
        """Test that users table exists with correct schema."""
        async with aiosqlite.connect(temp_sqlite_db) as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
            )
            result = await cursor.fetchone()
            assert result is not None
            assert result[0] == "users"
    
    async def test_threads_table_exists(self, temp_sqlite_db):
        """Test that threads table exists."""
        async with aiosqlite.connect(temp_sqlite_db) as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='threads'"
            )
            result = await cursor.fetchone()
            assert result is not None
    
    async def test_steps_table_exists(self, temp_sqlite_db):
        """Test that steps table exists."""
        async with aiosqlite.connect(temp_sqlite_db) as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='steps'"
            )
            result = await cursor.fetchone()
            assert result is not None
    
    async def test_steps_has_defaultopen_column(self, temp_sqlite_db):
        """Test that steps table has the defaultOpen column."""
        async with aiosqlite.connect(temp_sqlite_db) as db:
            cursor = await db.execute("PRAGMA table_info(steps)")
            columns = await cursor.fetchall()
            column_names = [col[1] for col in columns]
            assert "defaultOpen" in column_names


class TestUserOperations:
    """Test CRUD operations on users table."""
    
    async def test_insert_user(self, temp_sqlite_db):
        """Test inserting a user."""
        user_id = str(uuid.uuid4())
        identifier = "test@example.com"
        
        async with aiosqlite.connect(temp_sqlite_db) as db:
            await db.execute(
                "INSERT INTO users (id, identifier, metadata) VALUES (?, ?, ?)",
                (user_id, identifier, '{}')
            )
            await db.commit()
            
            cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            result = await cursor.fetchone()
            
            assert result is not None
            assert result[1] == identifier
    
    async def test_unique_identifier_constraint(self, temp_sqlite_db):
        """Test that user identifiers must be unique."""
        identifier = "unique@example.com"
        
        async with aiosqlite.connect(temp_sqlite_db) as db:
            # Insert first user
            await db.execute(
                "INSERT INTO users (id, identifier, metadata) VALUES (?, ?, ?)",
                (str(uuid.uuid4()), identifier, '{}')
            )
            await db.commit()
            
            # Try to insert duplicate identifier
            with pytest.raises(aiosqlite.IntegrityError):
                await db.execute(
                    "INSERT INTO users (id, identifier, metadata) VALUES (?, ?, ?)",
                    (str(uuid.uuid4()), identifier, '{}')
                )
                await db.commit()


class TestThreadOperations:
    """Test CRUD operations on threads table."""
    
    async def test_insert_thread(self, temp_sqlite_db):
        """Test inserting a conversation thread."""
        # First create a user
        user_id = str(uuid.uuid4())
        async with aiosqlite.connect(temp_sqlite_db) as db:
            await db.execute(
                "INSERT INTO users (id, identifier, metadata) VALUES (?, ?, ?)",
                (user_id, "user@example.com", '{}')
            )
            await db.commit()
            
            # Then create a thread
            thread_id = str(uuid.uuid4())
            await db.execute(
                "INSERT INTO threads (id, name, userId) VALUES (?, ?, ?)",
                (thread_id, "Test Thread", user_id)
            )
            await db.commit()
            
            cursor = await db.execute("SELECT * FROM threads WHERE id = ?", (thread_id,))
            result = await cursor.fetchone()
            
            assert result is not None
            assert result[2] == "Test Thread"  # name column
    
    async def test_cascade_delete_threads(self, temp_sqlite_db):
        """Test that deleting a user cascades to threads."""
        user_id = str(uuid.uuid4())
        thread_id = str(uuid.uuid4())
        
        async with aiosqlite.connect(temp_sqlite_db) as db:
            # Enable foreign keys for this connection
            await db.execute("PRAGMA foreign_keys = ON;")
            
            # Create user and thread
            await db.execute(
                "INSERT INTO users (id, identifier, metadata) VALUES (?, ?, ?)",
                (user_id, "cascade@example.com", '{}')
            )
            await db.execute(
                "INSERT INTO threads (id, name, userId) VALUES (?, ?, ?)",
                (thread_id, "Thread", user_id)
            )
            await db.commit()
            
            # Delete user
            await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
            await db.commit()
            
            # Thread should be deleted
            cursor = await db.execute("SELECT * FROM threads WHERE id = ?", (thread_id,))
            result = await cursor.fetchone()
            assert result is None


class TestStepOperations:
    """Test CRUD operations on steps table."""
    
    async def test_insert_step(self, temp_sqlite_db):
        """Test inserting a conversation step (message)."""
        # Create user and thread first
        user_id = str(uuid.uuid4())
        thread_id = str(uuid.uuid4())
        step_id = str(uuid.uuid4())
        
        async with aiosqlite.connect(temp_sqlite_db) as db:
            await db.execute(
                "INSERT INTO users (id, identifier, metadata) VALUES (?, ?, ?)",
                (user_id, "step@example.com", '{}')
            )
            await db.execute(
                "INSERT INTO threads (id, name, userId) VALUES (?, ?, ?)",
                (thread_id, "Thread", user_id)
            )
            await db.commit()
            
            # Insert step
            await db.execute(
                """INSERT INTO steps (id, name, type, threadId, streaming, input, output)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (step_id, "on_message", "run", thread_id, 0, "Hello", "Hi there!")
            )
            await db.commit()
            
            cursor = await db.execute("SELECT * FROM steps WHERE id = ?", (step_id,))
            result = await cursor.fetchone()
            
            assert result is not None
            assert result[1] == "on_message"  # name
            assert result[10] == "Hello"  # input (column index 10)
            assert result[11] == "Hi there!"  # output (column index 11)


class TestConnectionString:
    """Test SQLAlchemy connection string format."""
    
    def test_connection_string_format(self, sqlite_connection_string):
        """Test that connection string is properly formatted."""
        assert sqlite_connection_string.startswith("sqlite+aiosqlite:///")
        assert ".db" in sqlite_connection_string
