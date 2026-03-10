# Cross-Thread User Memory

EagleAgent now supports **persistent cross-thread memory** using LangGraph's Store system with PostgreSQL backend.

## What is Cross-Thread Memory?

### Before (Thread-Only Memory)
- ✅ Agent remembers within the same conversation thread
- ❌ Agent forgets when you start a new conversation
- Example: "My name is Tom" → Start new thread → "What's my name?" → "I don't know"

### After (Cross-Thread Memory)
- ✅ Agent remembers within the same conversation thread
- ✅ **Agent remembers across ALL conversation threads**
- Example: "My name is Tom" → Start new thread → "What's my name?" → "Your name is Tom!"

## How It Works

### Two-Layer Memory System

| Layer | Technology | Scope | Lifespan | Purpose |
|-------|-----------|-------|----------|---------|
| **Conversation State** | PostgreSQL Checkpoints | Single thread | 7 days | Current conversation context |
| **User Profile** | PostgreSQL Store | All threads | Permanent | Long-term facts about the user |

### Architecture

```
PostgreSQL Database (mooballai)
├── checkpoints/              ← Thread-specific conversation state
│   ├── thread-uuid-1_...    (expires in 7 days)
│   └── thread-uuid-2_...    (expires in 7 days)
│
└── user_memory/              ← Cross-thread user profiles (NEW!)
    └── users:tom@mooball.net
        ├── value:
        │   ├── name: "Tom"
        │   ├── preferences: ["Python", "AI"]
        │   └── facts: ["works at MooBall"]
        ├── created_at: timestamp
        └── updated_at: timestamp
```

## Current Implementation Status

### ✅ What's Working Now

1. **Profile Storage**: User profiles stored in PostgreSQL `user_memory` collection
2. **Auto-Loading**: Agent automatically loads user profile at conversation start
3. **Context Injection**: Profile data injected as system message context
4. **Cross-Thread Access**: Same profile accessible from any conversation thread
5. **🎉 Auto-Save (NEW!)**: Agent autonomously saves information you share using tool calling

### Agent Tool Calling

The agent automatically has access to three tools:

- **`remember_user_info`**: Saves facts about you (name, preferences, etc.)
- **`get_user_info`**: Retrieves saved facts
- **`forget_user_info`**: Deletes specific facts

Just tell the agent information naturally and it will save it:

- "My name is Tom" → Agent calls `remember_user_info("name", "Tom")`
- "I love Python" → Agent calls `remember_user_info("preferences", "loves Python")`
- "I work at MooBall" → Agent calls `remember_user_info("facts", "works at MooBall")`

### Manual Profile Management (Alternative)

You can also manage profiles manually using the CLI script:

```bash
# Set user name
uv run manage_user_profile.py set tom@mooball.net name "Tom"

# Add preferences
uv run manage_user_profile.py set tom@mooball.net preferences "loves Python"

# Add facts
uv run manage_user_profile.py set tom@mooball.net facts "works at MooBall"

# View profile
uv run manage_user_profile.py get tom@mooball.net
```

### 🚧 Future Enhancement: Agent Tools

The `user_profile_tools.py` module provides tools for agent-driven profile updates:
- `remember_user_info()` - Agent saves information when user shares it
- `get_user_info()` - Agent retrieves stored information
- `forget_user_info()` - Agent deletes specific information

**To enable this**, you would need to:
1. Bind tools to the model
2. Add tool-calling logic to the graph
3. Or use `create_react_agent()` prebuilt pattern

## Testing Cross-Thread Memory

### Scenario 1: Auto-Save with Tool Calling (Recommended)

1. **Start conversation (Thread 1):**
   - You: "My name is Tom and I love Python and AI"
   - Agent: (calls remember_user_info tools) "Nice to meet you Tom! I've noted that you love Python and AI."

2. **Verify it was saved:**
   ```bash
   uv run manage_user_profile.py get tom@mooball.net
   ```
   Should show: `name: "Tom"`, `preferences: ["Python", "AI"]`

3. **Start NEW conversation (Thread 2):**
   - You: "What's my name?"
   - Agent: "Your name is Tom!"
   - ✅ **Agent remembers across threads!**

### Scenario 2: Manual Profile Management

1. **Set your profile manually:**
   ```bash
   uv run manage_user_profile.py set tom@mooball.net name "Tom"
   uv run manage_user_profile.py set tom@mooball.net preferences "loves AI and Python"
   ```

2. **Start conversation (Thread 1):**
   - You: "Hello!"
   - Agent: "Hello Tom! I see you love AI and Python. How can I help?"

3. **Start NEW conversation (Thread 2):**
   - You: "What do I like?"
   - Agent: "You love AI and Python!"
   - ✅ **Agent remembers across threads!**

### Scenario 3: Testing Tool Calling

1. **Check terminal output** to see tool calls:
   ```bash
   # In another terminal, watch the app logs
   tail -f <your terminal output>
   ```

2. **Say something memorable:**
   - You: "I work at MooBall"
   - Watch for: `ToolCall(name='remember_user_info', args={'category': 'facts', 'information': 'works at MooBall'})`

3. **Verify storage:**
   ```bash
   uv run manage_user_profile.py get tom@mooball.net
   ```

## Managing User Profiles

### Set Profile Data

```bash
# Basic syntax
uv run manage_user_profile.py set USER_EMAIL FIELD VALUE

# Examples
uv run manage_user_profile.py set tom@mooball.net name "Tom"
uv run manage_user_profile.py set tom@mooball.net job "AI Engineer"
uv run manage_user_profile.py set tom@mooball.net location "San Francisco"
uv run manage_user_profile.py set tom@mooball.net preferences "Python programming"
uv run manage_user_profile.py set tom@mooball.net facts "has a dog named Max"
```

**Note:** `preferences` and `facts` are treated as lists - you can add multiple items:
```bash
uv run manage_user_profile.py set tom@mooball.net preferences "Python"
uv run manage_user_profile.py set tom@mooball.net preferences "AI"
# Result: preferences: ["Python", "AI"]
```

### View Profile Data

```bash
# View entire profile
uv run manage_user_profile.py get tom@mooball.net

# View specific field
uv run manage_user_profile.py get tom@mooball.net name

# List all user profiles
uv run manage_user_profile.py list
```

### Delete Profile Data

```bash
# Delete entire profile
uv run manage_user_profile.py delete tom@mooball.net
```

## Profile Data Structure

User profiles are stored as JSON with flexible schema:

```json
{
  "name": "Tom",
  "job": "AI Engineer",
  "location": "San Francisco",
  "preferences": ["Python", "AI", "hiking"],
  "facts": ["has a dog named Max", "works at MooBall"],
  "custom_field": "custom value"
}
```

**Fields are arbitrary** - you can add any field name you want!

## Files Created

### Core Implementation
- **`native Postgres PostgresStore`** - LangGraph BaseStore implementation for PostgreSQL
  - Handles get/put/delete/search operations
  - Namespace-based document organization
  - Async-compatible interface

### Management Tools
- **`manage_user_profile.py`** - CLI tool for profile management
  - Set, get, delete profile data
  - List all user profiles
  - Used for manual profile management

### Future Tools (Not Yet Integrated)
- **`user_profile_tools.py`** - Agent tools for automated profile updates
  - `remember_user_info()` - Save user information
  - `get_user_info()` - Retrieve user information
  - `forget_user_info()` - Delete user information

### Updated Files
- **`app.py`**:
  - Added `user_id` to `AgentState`
  - Integrated `AsyncPostgresStore` with graph compilation
  - Updated `call_model()` to load and inject user profile
  - Set `user_id` in `on_chat_start` and `on_chat_resume`

## Technical Details

### Store vs Checkpointer

| Feature | Checkpointer | Store |
|---------|-------------|-------|
| **Purpose** | Conversation state | User profiles, facts |
| **Scope** | Single thread | Cross-thread |
| **Lifespan** | 7 days (TTL) | Permanent |
| **Key Format** | `thread_id` | `(namespace, key)` |
| **Implementation** | `AsyncPostgresSaver` | `AsyncPostgresStore` |
| **Collection** | `checkpoints` | `user_memory` |

### Namespace Convention

Store uses hierarchical namespaces:
- `("users",)` - User profiles
- `("users", "preferences")` - User preference details
- `("facts",)` - General facts
- `("system",)` - System-level data

### Document ID Format

Store documents use this ID pattern:
```
namespace1/namespace2/.../namespacen:key
```

Examples:
- `users:tom@mooball.net` - Tom's user profile
- `users/preferences:tom@mooball.net` - Tom's detailed preferences

## Future Enhancements

1. **Agent Tool Integration**
   - Bind profile tools to the model
   - Agent autonomously updates profiles during conversation
   - Example: User says "My name is Tom" → Agent calls `remember_user_info("name", "Tom")`

2. **Auto-Detection**
   - NLP-based extraction of user information
   - Automatic profile updates without explicit commands
   - Smart categorization of information

3. **Profile UI**
   - Chainlit sidebar for viewing/editing profile
   - Visual profile management
   - History of profile changes

4. **Advanced Features**
   - Profile versioning
   - Profile sharing across workspaces
   - Privacy controls
   - Profile export/import

## Learn More

- **LangGraph Store Documentation**: https://langchain-ai.github.io/langgraph/reference/store/

