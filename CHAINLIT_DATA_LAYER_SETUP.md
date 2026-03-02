# Chainlit Data Layer Setup

To enable conversation history and thread persistence in Chainlit, you need to set up a data layer. This project uses **SQLite** for local development and testing.

## Current Configuration

✅ **SQLite** is currently configured and ready to use.

## Why Do We Need This?

The data layer stores:
- **Threads**: Conversation metadata (id, user, timestamps, tags)
- **Steps**: Individual messages and their metadata
- **Elements**: Attachments, images, files
- **Feedback**: User ratings and feedback

**Note:** This is separate from LangGraph's Firestore checkpoints:
- **Chainlit data layer**: Stores conversation UI/metadata for the chat history sidebar
- **LangGraph checkpoints**: Stores conversation state for resuming the actual conversation

Both work together to provide seamless conversation persistence!

## How It Works: Dual Persistence Architecture

This app uses **two separate databases** for conversation persistence:

### 1. Chainlit Data Layer (SQLite) - "Cold Storage"
**Purpose:** UI metadata and conversation history for the sidebar

**Stores:**
- Thread metadata (name, timestamps, tags)
- Message history (for display in UI)
- User information
- Feedback and ratings
- File attachments

**Current Implementation:** SQLite database (`chainlit_datalayer.db`)

### 2. LangGraph Checkpoints (Firestore) - "Hot Storage"  
**Purpose:** Active conversation state for resuming agent workflows

**Stores:**
- Current conversation state
- Agent memory and context
- Intermediate execution states
- Message sequences

**Current Implementation:** Firestore with 7-day TTL (`TimestampedFirestoreSaver`)

### How They Work Together

1. **User sends a message** → LangGraph processes it and saves checkpoint to Firestore
2. **Chainlit records the step** → SQLite stores the message for UI display
3. **User clicks old conversation** → `@cl.on_chat_resume` fires
4. **Thread ID is restored** → LangGraph loads state from Firestore checkpoint
5. **Conversation continues** → User can seamlessly resume from where they left off

**Why two databases?**
- **Firestore:** Fast, scalable, already integrated, perfect for ephemeral state
- **SQLite:** Structured queries, chat history UI, user management

## SQLite Data Layer Setup

This project uses SQLite for local development and testing with the community SQLAlchemy data layer.

### Required Packages

Three Python packages are required for SQLite data persistence:

#### 1. **SQLAlchemy** (`sqlalchemy`)
```bash
uv add sqlalchemy
```
- **Purpose:** ORM (Object-Relational Mapping) framework
- **Why needed:** Chainlit's `SQLAlchemyDataLayer` uses SQLAlchemy to interact with the database
- **What it does:** Provides database abstraction, query building, and schema management

#### 2. **aiosqlite** (`aiosqlite`)
```bash
uv add aiosqlite
```
- **Purpose:** Async SQLite driver
- **Why needed:** Chainlit is built on async Python (asyncio) and requires async database operations
- **What it does:** Provides async/await support for SQLite operations
- **Critical:** Without this, you'll get "asyncio extension requires an async driver" error

#### 3. **greenlet** (`greenlet`)
```bash
uv add greenlet
```
- **Purpose:** Lightweight coroutine support
- **Why needed:** SQLAlchemy's async engine uses greenlet for context switching
- **What it does:** Enables SQLAlchemy to bridge sync and async code
- **Critical:** Without this, you'll get "No module named 'greenlet'" error

### Installation

Install all dependencies at once:
```bash
uv add sqlalchemy aiosqlite greenlet
```

### Database Initialization

After installing dependencies, initialize the database schema:

```bash
uv run init_sqlite_db.py
```

**What this script does:**
1. Creates `chainlit_datalayer.db` SQLite file in your project root
2. Enables foreign key constraints (`PRAGMA foreign_keys = ON`)
3. Creates five tables with proper relationships
4. Uses `CREATE TABLE IF NOT EXISTS` for safe re-runs

### Start the Application

```bash
./run.sh
```

### Configuration Files

#### 1. Environment Variable (`.env`)
```bash
DATABASE_URL=sqlite+aiosqlite:///./chainlit_datalayer.db
```

**Important:** The connection string must use `sqlite+aiosqlite://` (not just `sqlite://`) to enable async support.

#### 2. Data Layer Decorator (`app.py`)
```python
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer

@cl.data_layer
def get_data_layer():
    return SQLAlchemyDataLayer(conninfo=os.getenv("DATABASE_URL"))
```

This decorator tells Chainlit to use the SQLAlchemy data layer for conversation persistence.

### Database Schema Details

The `init_sqlite_db.py` script creates five tables:

#### 1. **users** Table
```sql
CREATE TABLE users (
    "id" TEXT PRIMARY KEY,              -- User UUID (stored as text)
    "identifier" TEXT NOT NULL UNIQUE,  -- Email or username
    "metadata" TEXT NOT NULL,           -- JSON metadata (image, provider)
    "createdAt" TEXT                    -- ISO timestamp
);
```
**Purpose:** Stores authenticated users and their metadata.

#### 2. **threads** Table
```sql
CREATE TABLE threads (
    "id" TEXT PRIMARY KEY,              -- Thread UUID
    "createdAt" TEXT,                   -- ISO timestamp
    "name" TEXT,                        -- Conversation title
    "userId" TEXT,                      -- Foreign key to users
    "userIdentifier" TEXT,              -- User email/username
    "tags" TEXT,                        -- Comma-separated tags
    "metadata" TEXT,                    -- JSON metadata
    FOREIGN KEY ("userId") REFERENCES users("id") ON DELETE CASCADE
);
```
**Purpose:** Stores conversation metadata for the history sidebar.

#### 3. **steps** Table
```sql
CREATE TABLE steps (
    "id" TEXT PRIMARY KEY,              -- Step UUID
    "name" TEXT NOT NULL,               -- Step name/type
    "type" TEXT NOT NULL,               -- "user_message", "assistant_message", etc.
    "threadId" TEXT NOT NULL,           -- Foreign key to threads
    "parentId" TEXT,                    -- Parent step (for nested steps)
    "streaming" INTEGER NOT NULL,       -- Boolean: 0 or 1
    "waitForAnswer" INTEGER,            -- Boolean: 0 or 1
    "isError" INTEGER,                  -- Boolean: 0 or 1
    "metadata" TEXT,                    -- JSON metadata
    "tags" TEXT,                        -- Comma-separated tags
    "input" TEXT,                       -- User input text
    "output" TEXT,                      -- AI response text
    "createdAt" TEXT,                   -- ISO timestamp
    "start" TEXT,                       -- Start timestamp
    "end" TEXT,                         -- End timestamp
    "generation" TEXT,                  -- JSON generation metadata
    "showInput" TEXT,                   -- Display setting
    "language" TEXT,                    -- Code language (if applicable)
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);
```
**Purpose:** Stores individual messages and interactions within conversations.

#### 4. **elements** Table
```sql
CREATE TABLE elements (
    "id" TEXT PRIMARY KEY,              -- Element UUID
    "threadId" TEXT,                    -- Foreign key to threads
    "type" TEXT,                        -- "image", "file", "pdf", etc.
    "url" TEXT,                         -- Public URL (if cloud storage)
    "chainlitKey" TEXT,                 -- Internal Chainlit key
    "name" TEXT NOT NULL,               -- File name
    "display" TEXT,                     -- Display mode
    "objectKey" TEXT,                   -- Cloud storage object key
    "size" TEXT,                        -- File size
    "page" INTEGER,                     -- Page number (for PDFs)
    "language" TEXT,                    -- Language (for code)
    "forId" TEXT,                       -- Associated step ID
    "mime" TEXT,                        -- MIME type
    "props" TEXT,                       -- JSON props
    "autoPlay" INTEGER,                 -- Boolean: 0 or 1
    "playerConfig" TEXT,                -- JSON player config
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);
```
**Purpose:** Stores file attachments, images, and other media elements.

#### 5. **feedbacks** Table
```sql
CREATE TABLE feedbacks (
    "id" TEXT PRIMARY KEY,              -- Feedback UUID
    "forId" TEXT NOT NULL,              -- Step ID this feedback is for
    "threadId" TEXT NOT NULL,           -- Thread ID
    "value" INTEGER NOT NULL,           -- Rating value (e.g., -1, 0, 1)
    "comment" TEXT,                     -- Optional comment
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);
```
**Purpose:** Stores user feedback and ratings on AI responses.

### Schema Adaptations for SQLite

**Key differences from PostgreSQL:**
- **UUIDs stored as TEXT:** SQLite doesn't have a native UUID type
- **Booleans stored as INTEGER:** 0 = false, 1 = true
- **JSON stored as TEXT:** SQLite stores JSON as text, parsed by application
- **Arrays stored as TEXT:** Tags stored as comma-separated or JSON strings
- **Foreign keys:** Explicitly enabled with `PRAGMA foreign_keys = ON`

### Testing Conversation Persistence

1. ✅ Start the app and sign in with Google OAuth
2. ✅ Have a conversation (send a few messages)
3. ✅ Look for the **"History" icon** (clock) in the sidebar
4. ✅ Click it to see your past conversations
5. ✅ Start a new conversation
6. ✅ Click on an old conversation to resume it
7. ✅ Verify the conversation state is restored from Firestore

### SQLite Characteristics

**Advantages:**
- ✅ Perfect for local development and testing
- ✅ Zero setup - no external database server needed
- ✅ File-based - easy to backup/delete (`chainlit_datalayer.db`)
- ✅ Portable - can copy database file between machines
- ✅ No configuration - works out of the box

**Limitations:**
- ⚠️ File-based locking - limited concurrent write operations
- ⚠️ Single server only - no distributed deployments
- ⚠️ Not cloud-friendly (file storage)
- ⚠️ Limited scalability compared to client-server databases

**When to upgrade:**
- Multiple concurrent users (>10 simultaneous users)
- Cloud deployment requirements
- Need for database replication or clustering
- Production applications requiring high availability

### Related Files

**Project files related to data persistence:**
- `app.py` - Contains `@cl.data_layer` decorator and `@cl.on_chat_resume` callback
- `init_sqlite_db.py` - Database initialization script for SQLite
- `chainlit_datalayer.db` - SQLite database file (created after running init script)
- `timestamped_firestore_saver.py` - Custom Firestore saver with TTL support
- `.env` - Contains `DATABASE_URL` configuration
- `FIRESTORE_TTL.md` - Documentation for Firestore TTL policies

## Troubleshooting

### SQLite-Specific Issues

**"No module named 'greenlet'"**
```bash
uv add greenlet
```

**"no such table: users"**
```bash
# Initialize the database schema
uv run init_sqlite_db.py
```

**"The asyncio extension requires an async driver"**
- Make sure your `DATABASE_URL` uses `sqlite+aiosqlite://` (not `sqlite://`)
- Verify aiosqlite is installed: `uv add aiosqlite`

**Database file locked**
- SQLite doesn't handle high concurrency well
- Close other connections to the database
- Kill any running instances of the app: `./kill.sh`
- If persistent, delete `chainlit_datalayer.db` and reinitialize: `uv run init_sqlite_db.py`

### General Issues

**Chat history not showing**
- Make sure `DATABASE_URL` is set in `.env`
- Verify authentication is working (must be logged in)
- Check browser console for errors

**"SQLAlchemyDataLayer storage client is not initialized"**
- This is a warning, not an error
- File attachments won't be persisted without cloud storage
- To enable, configure a storage provider (S3, GCS, Azure Blob)

## Next Steps

Once the data layer is working:
1. ✅ Test creating multiple conversations
2. ✅ Verify they appear in the history sidebar
3. ✅ Click an old conversation to resume it
4. ✅ Confirm LangGraph state is restored (conversation continues correctly)

The combination of:
- **Chainlit data layer** (conversation UI/metadata)
- **LangGraph Firestore checkpoints** (conversation state)
- **@cl.on_chat_resume** (restoration logic)

...gives you complete, production-ready conversation persistence!
