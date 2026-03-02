# EagleAgent Context Architecture

This document explains how context and messages flow through the EagleAgent system, from user input to LLM response. Understanding this architecture is crucial for modifying prompts, debugging conversations, or extending agent capabilities.

## Table of Contents

1. [Overview](#overview)
2. [Message Types](#message-types)
3. [Context Construction Flow](#context-construction-flow)
4. [Dual-Memory Architecture](#dual-memory-architecture)
5. [System Prompt Composition](#system-prompt-composition)
6. [Context Priority & Order](#context-priority--order)
7. [Configuration Locations](#configuration-locations)
8. [Best Practices](#best-practices)

---

## Overview

EagleAgent uses **LangGraph** for orchestration and **LangChain message types** for conversation structure. The agent maintains **two types of memory**:

1. **Thread-scoped memory** (conversation history) - Firestore checkpointer
2. **Cross-thread memory** (user profiles) - Firestore store

The complete context sent to the LLM on each turn consists of:
- **SystemMessage**: Agent instructions + user profile context
- **Conversation history**: Previous HumanMessages, AIMessages, ToolMessages
- **Current user input**: New HumanMessage

---

## Message Types

EagleAgent uses LangChain's message types from `langchain_core.messages`:

### SystemMessage
**Purpose**: Provide instructions, context, and behavioral guidelines to the LLM  
**Constructed**: Dynamically in `call_model()` using [`includes/prompts.py`](includes/prompts.py)  
**Frequency**: Created fresh on each LLM invocation (not persisted in conversation history)  
**Contents**:
- User profile information (if available)
- Tool usage instructions
- (Future) Agent identity and personality

**Example**:
```
User profile information:
- Preferred name: Tom (use this to address the user)
- Preferences: Python, AI, LangGraph
- Facts: Software engineer, likes concise explanations

When the user tells you information about themselves, use the remember_user_info tool...
```

### HumanMessage
**Purpose**: Represent user input  
**Constructed**: In `@cl.on_message` handler from `message.content`  
**Frequency**: One per user message  
**Persisted**: Yes, stored in LangGraph checkpointer  

**Example**:
```python
HumanMessage(content="What's the weather like today?")
```

### AIMessage
**Purpose**: Represent LLM responses  
**Constructed**: Returned by `model.ainvoke()` call  
**Frequency**: One per LLM response  
**Persisted**: Yes, stored in LangGraph checkpointer  
**May contain**: `tool_calls` attribute if LLM requests tool execution

**Example**:
```python
AIMessage(
    content="I don't have access to real-time weather data...",
    tool_calls=[{"name": "get_weather", "args": {"location": "..."}}]
)
```

### ToolMessage
**Purpose**: Represent tool execution results  
**Constructed**: By LangGraph's `ToolNode` after executing tools  
**Frequency**: One per tool call executed  
**Persisted**: Yes, stored in LangGraph checkpointer  

**Example**:
```python
ToolMessage(
    content='{"status": "success", "message": "Saved preferred_name: Tom"}',
    tool_call_id="call_abc123"
)
```

---

## Context Construction Flow

Here's the complete flow from user message to LLM response:

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. User sends message via Chainlit UI                          │
└────────────────────┬────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│ 2. @cl.on_message handler                                      │
│    - Retrieves thread_id from session                          │
│    - Retrieves user_id from session                            │
│    - Creates HumanMessage from message.content                 │
│    - Prepares graph inputs: {messages: [...], user_id: ...}    │
└────────────────────┬────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│ 3. LangGraph invocation                                         │
│    graph.astream_events(inputs, config={"thread_id": ...})     │
│                                                                 │
│    LangGraph loads conversation state from checkpointer:       │
│    - Previous messages (HumanMessage, AIMessage, ToolMessage)  │
│    - Merges with new message using operator.add                │
└────────────────────┬────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│ 4. call_model() node execution                                 │
│                                                                 │
│    Step 4a: Load user profile from cross-thread store          │
│    ┌─────────────────────────────────────────────────┐         │
│    │ user_profile = await store.aget(                │         │
│    │     ("users",),                                 │         │
│    │     user_id                                     │         │
│    │ )                                               │         │
│    └─────────────────────────────────────────────────┘         │
│                                                                 │
│    Step 4b: Build system prompt                                │
│    ┌─────────────────────────────────────────────────┐         │
│    │ from includes.prompts import build_system_prompt│         │
│    │                                                 │         │
│    │ if user_profile and user_profile.value:        │         │
│    │     system_content = build_system_prompt(      │         │
│    │         user_profile.value                     │         │
│    │     )                                           │         │
│    │ else:                                           │         │
│    │     system_content = build_system_prompt(None) │         │
│    └─────────────────────────────────────────────────┘         │
│                                                                 │
│    Step 4c: Construct message sequence                         │
│    ┌─────────────────────────────────────────────────┐         │
│    │ enhanced_messages = [                           │         │
│    │     SystemMessage(content=system_content),     │         │
│    │     ...state["messages"]  # History + current  │         │
│    │ ]                                               │         │
│    └─────────────────────────────────────────────────┘         │
│                                                                 │
│    Step 4d: Create user-specific tools                         │
│    ┌─────────────────────────────────────────────────┐         │
│    │ tools = create_profile_tools(store, user_id)   │         │
│    │ model_with_tools = base_model.bind_tools(tools)│         │
│    └─────────────────────────────────────────────────┘         │
│                                                                 │
│    Step 4e: Invoke LLM                                         │
│    ┌─────────────────────────────────────────────────┐         │
│    │ response = await model_with_tools.ainvoke(     │         │
│    │     enhanced_messages                           │         │
│    │ )                                               │         │
│    └─────────────────────────────────────────────────┘         │
└────────────────────┬────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│ 5. Routing decision (should_continue)                          │
│    - If response contains tool_calls → route to "tools" node   │
│    - Otherwise → END                                            │
└────────────────────┬────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│ 6a. Tool execution (if tool calls present)                     │
│     - ToolNode executes each tool call                         │
│     - Returns ToolMessages with results                        │
│     - Graph loops back to call_model() with tool results       │
│                                                                 │
│ 6b. Response streaming (if no tool calls)                      │
│     - Stream response tokens to Chainlit UI                    │
│     - Save final response to checkpointer                      │
└─────────────────────────────────────────────────────────────────┘
```

---

## Dual-Memory Architecture

EagleAgent uses a sophisticated dual-memory system:

### Thread-Scoped Memory (Checkpointer)

**Implementation**: [`TimestampedFirestoreSaver`](includes/timestamped_firestore_saver.py)  
**Storage**: Firestore collection `checkpoints`  
**Scope**: Per conversation thread  
**Key**: `thread_id` (UUID generated per conversation)  
**Contents**: Complete conversation state including all messages  
**Lifecycle**: Persists across app restarts, user sessions  
**Purpose**: Resume conversations exactly where they left off

**What's stored**:
```python
{
    "thread_id": "uuid-1234-5678",
    "checkpoint": {
        "messages": [
            HumanMessage(...),
            AIMessage(...),
            ToolMessage(...),
            ...
        ],
        "user_id": "user@example.com"
    },
    "created_at": timestamp  # For TTL cleanup
}
```

**User experience**:
- User starts conversation → creates new thread_id
- User closes browser → thread_id stored in Chainlit data layer
- User returns → Chainlit restores thread_id → LangGraph loads full conversation state
- Messages appear in sidebar, agent remembers conversation context

### Cross-Thread Memory (Store)

**Implementation**: [`FirestoreStore`](includes/firestore_store.py)  
**Storage**: Firestore collection `user_memory`  
**Scope**: Per user across ALL conversation threads  
**Key**: `user_id` (user's email from OAuth)  
**Contents**: User profile, preferences, facts  
**Lifecycle**: Persists indefinitely across all conversations  
**Purpose**: Remember user information across different conversation threads

**What's stored**:
```python
{
    "namespace": ("users",),
    "key": "user@example.com",
    "value": {
        "preferred_name": "Tom",
        "preferences": ["Python", "concise explanations"],
        "facts": ["software engineer", "works on AI projects"]
    }
}
```

**User experience**:
- User says "Call me Tom" in conversation A → Saved to store
- User starts new conversation B next week → Agent still calls them "Tom"
- User updates preferences → Available in all future and current conversations

### How They Work Together

```
Thread 1 (Monday)                Thread 2 (Tuesday)
┌─────────────────┐              ┌─────────────────┐
│ HumanMessage    │              │ HumanMessage    │
│ AIMessage       │              │ AIMessage       │
│ ToolMessage     │              │ HumanMessage    │
│ ...             │              │ AIMessage       │
└────────┬────────┘              └────────┬────────┘
         │                                │
         │  Stored via checkpointer      │
         │  (thread-specific)            │
         │                                │
         └────────────────┬───────────────┘
                         │
                         │  Both threads access
                         │  same user profile
                         ▼
                ┌─────────────────┐
                │  User Profile   │
                │  (Store)        │
                │  - preferred    │
                │    name: Tom    │
                │  - preferences  │
                │  - facts        │
                └─────────────────┘
                    Cross-thread
                    persistent
```

---

## System Prompt Composition

The system prompt is dynamically constructed on each LLM call to ensure it reflects the latest user profile state.

### Components

The system prompt consists of up to three parts (configured in [`includes/prompts.py`](includes/prompts.py)):

1. **User Profile Header** (if profile exists)
   ```
   User profile information:
   ```

2. **User Profile Sections** (if data available)
   - Preferred name (priority over regular name)
   - Name (fallback if no preferred name)
   - Preferences
   - Facts

3. **Tool Instructions** (always included)
   ```
   When the user tells you information about themselves, use the remember_user_info tool...
   ```

### Construction Logic

See [`includes/prompts.py:build_system_prompt()`](includes/prompts.py) for implementation.

**With User Profile**:
```
User profile information:
- Preferred name: Tom (use this to address the user)
- Preferences: Python, AI
- Facts: Software engineer

When the user tells you information about themselves, use the remember_user_info tool...
```

**Without User Profile** (new user):
```
When the user tells you information about themselves, use the remember_user_info tool...
```

### Why Dynamic Construction?

**Question**: Why not store the system message in conversation history?  
**Answer**: User profile can change during the conversation!

**Scenario**:
1. User starts conversation (no profile yet)
2. User says "Call me Tom" → Tool updates profile in store
3. Agent response uses tool → gets new message from user
4. `call_model()` is invoked again → loads updated profile → system prompt now includes "Tom"

If we cached the system message, it would be stale after tool execution.

---

## Context Priority & Order

Messages are sent to the LLM in this exact order:

### Message Sequence

```
1. SystemMessage       ← Dynamically constructed from includes/prompts.py
   └─ User profile (if available)
   └─ Tool instructions

2. Previous messages   ← Loaded from checkpointer (thread-scoped history)
   └─ HumanMessage
   └─ AIMessage
   └─ ToolMessage
   └─ HumanMessage
   └─ AIMessage
   └─ ...

3. Current message     ← New user input
   └─ HumanMessage
```

### Priority Rules

When the same information appears in multiple sources:

| Information Type | Primary Source | Fallback |
|-----------------|----------------|----------|
| User's name | `preferred_name` from store | `name` from store |
| User identity | Store (`user_memory`) | OAuth metadata |
| Tool instructions | `includes/prompts.py` | N/A |
| Agent behavior | `includes/prompts.py` | N/A |
| Conversation history | Checkpointer | N/A |

### Name Resolution Example

Priority order for determining how to address the user:

1. **Highest**: `preferred_name` from user profile store
   - User explicitly said "call me X"
   - Saved via `remember_user_info` tool
   
2. **Medium**: `given_name` from Google OAuth metadata
   - Retrieved during authentication
   - Stored in `cl.User.metadata`
   
3. **Lowest**: `identifier` (email address)
   - Always available from authentication
   - Used as fallback

**Code location**: See [`app.py:on_chat_start()`](app.py) lines 221-233

---

## Configuration Locations

Understanding where different types of configuration live:

### Infrastructure Configuration
**File**: `.env`  
**Contents**:
- `GOOGLE_API_KEY` - LLM access
- `GOOGLE_PROJECT_ID` - Firestore/GCP
- `DATABASE_URL` - Chainlit data layer (SQLite)
- `OAUTH_*` - Authentication settings

**When to modify**: Changing deployment environment, credentials, OAuth providers

### Prompt Configuration
**File**: [`includes/prompts.py`](includes/prompts.py)  
**Contents**:
- `AGENT_CONFIG` - Agent identity, personality
- `TOOL_INSTRUCTIONS` - Guidance for tool usage
- `PROFILE_TEMPLATES` - User profile formatting
- Helper functions for system prompt construction

**When to modify**: Changing agent behavior, tool instructions, profile context

### UI Configuration
**File**: [`chainlit.md`](chainlit.md)  
**Contents**: Welcome message shown in Chainlit UI  
**Scope**: User-facing only, NOT sent to LLM

**When to modify**: Changing welcome screen content

### Graph Configuration
**File**: [`app.py`](app.py)  
**Contents**:
- Graph structure (nodes, edges)
- State schema (`AgentState`)
- Model initialization
- Routing logic

**When to modify**: Adding new nodes, changing conversation flow, modifying state

### Tool Definitions
**File**: [`includes/user_profile_tools.py`](includes/user_profile_tools.py)  
**Contents**: Tool schemas and implementations

**When to modify**: Adding new tools, changing tool behavior

---

## Best Practices

### 1. Separation of Concerns

✅ **DO**: Keep prompts in [`includes/prompts.py`](includes/prompts.py)  
❌ **DON'T**: Hard-code prompt strings in business logic

✅ **DO**: Use environment variables for infrastructure config  
❌ **DON'T**: Put API keys or credentials in code

### 2. System Prompt Construction

✅ **DO**: Construct system prompt dynamically on each call  
❌ **DON'T**: Cache system prompts (profile can change)

✅ **DO**: Use helper functions from [`includes/prompts.py`](includes/prompts.py)  
❌ **DON'T**: Inline string concatenation in [`app.py`](app.py)

### 3. Testing Prompts

✅ **DO**: Unit test prompt construction in isolation  
❌ **DON'T**: Only test prompts via full integration tests

✅ **DO**: Test edge cases (empty profile, partial profile, lists vs strings)  
❌ **DON'T**: Assume profile data is always well-formed

### 4. Profile Data

✅ **DO**: Handle missing profile fields gracefully  
❌ **DON'T**: Assume all profile fields are present

✅ **DO**: Priority: `preferred_name` > `name`  
❌ **DON'T**: Use `name` if `preferred_name` is available

### 5. Message History

✅ **DO**: Let LangGraph manage message accumulation  
❌ **DON'T**: Manually manipulate `state["messages"]`

✅ **DO**: Use `operator.add` for message sequence in `AgentState`  
❌ **DON'T**: Try to merge messages manually

### 6. Debugging Context

To debug what's being sent to the LLM:

```python
# Add logging in call_model()
print(f"System prompt:\n{system_content}\n")
print(f"Message count: {len(enhanced_messages)}")
for i, msg in enumerate(enhanced_messages):
    print(f"{i}: {type(msg).__name__} - {msg.content[:100]}")
```

### 7. Future Migration to YAML

When ready to migrate prompts to YAML:

1. Copy dictionary structures from [`includes/prompts.py`](includes/prompts.py)
2. Paste into `config/prompts.yaml` with proper YAML syntax
3. Add `pyyaml` dependency: `uv add pyyaml`
4. Load config at startup:
   ```python
   import yaml
   PROMPTS = yaml.safe_load(Path("config/prompts.yaml").read_text())
   ```
5. Update helper functions to reference `PROMPTS` dict
6. No changes needed to [`app.py`](app.py) - still uses `build_system_prompt()`

---

## Troubleshooting

### Agent doesn't use preferred name

**Check**:
1. Is profile saved? `uv run scripts/manage_user_profile.py view <email>`
2. Is `preferred_name` field present in profile?
3. Is system prompt being constructed with profile data? (Add logging)
4. Is user_id being passed to `call_model()`?

### Profile information not persisting

**Check**:
1. Is Firestore emulator running? (production) or emulator host set?
2. Is `user_id` being set in `@cl.on_chat_start` and `@cl.on_chat_resume`?
3. Is store initialized correctly? Check [`app.py`](app.py) line 22
4. Are tool calls executing successfully? Check tool message content

### System prompt seems stale

**Check**:
- System prompt is constructed fresh on each `call_model()` invocation
- If profile changed but prompt didn't update → check store.aget() is returning updated data
- Clear Firestore cache if using emulator (restart emulator)

### Conversation history not loading on resume

**Check**:
1. Is thread_id being restored in `@cl.on_chat_resume`?
2. Is checkpointer configured correctly? Check [`app.py`](app.py) line 142
3. Are messages being saved? Check Firestore `checkpoints` collection
4. Is `config={"configurable": {"thread_id": ...}}` being passed to graph?

---

## Related Documentation

- [Cross-Thread Memory Architecture](CROSS_THREAD_MEMORY.md) - Detailed explanation of the Store pattern
- [Testing Guide](TESTING.md) - How to test different components
- [Google OAuth Setup](GOOGLE_OAUTH_SETUP.md) - User authentication flow
- [Firestore TTL](FIRESTORE_TTL.md) - Checkpoint cleanup strategy

---

## Summary

**Key Takeaways**:

1. **Two memory systems**: Checkpointer (thread state) + Store (user profile)
2. **System prompt is dynamic**: Reconstructed on each LLM call with latest profile
3. **Message order matters**: SystemMessage → History → Current input
4. **Configuration is centralized**: [`includes/prompts.py`](includes/prompts.py) for all prompts
5. **Priority rules exist**: `preferred_name` > `name`, store > OAuth metadata
6. **Future-ready**: Dictionary structure maps to YAML for easy migration

**For most use cases**, you only need to modify [`includes/prompts.py`](includes/prompts.py) to change agent behavior or add new context to system prompts.
