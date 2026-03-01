
# EagleAgent

AI-powered assistant using Google Gemini and LangGraph, with Google OAuth authentication and complete conversation persistence.

## Features

- 🤖 **Google Gemini Integration**: Powered by gemini-3-flash-preview
- 🔐 **Google OAuth Authentication**: Secure login with Google accounts
- 💬 **Chat History**: Browse and resume past conversations
- 💾 **Dual Persistence**: 
  - Chainlit data layer for conversation UI/metadata
  - Firestore checkpoints for LangGraph state
- ⏰ **Automatic TTL**: Conversations automatically expire after 7 days (configurable)
- 🎨 **Chainlit UI**: Clean, modern chat interface with history sidebar

## Quick Start

### 1. Install Dependencies

```bash
uv sync
```

### 2. Set Up Google OAuth

Follow the detailed instructions in [GOOGLE_OAUTH_SETUP.md](GOOGLE_OAUTH_SETUP.md) to:
- Generate authentication secret
- Create Google OAuth credentials
- Configure environment variables

### 3. Set Up Data Layer (For Conversation History)

Follow [CHAINLIT_DATA_LAYER_SETUP.md](CHAINLIT_DATA_LAYER_SETUP.md) to set up PostgreSQL for storing conversation history.

**Quick option (local testing):**
```bash
# Start PostgreSQL with Docker
docker run -d --name chainlit-postgres \
  -e POSTGRES_PASSWORD=secret \
  -e POSTGRES_DB=chainlit_db \
  -p 5432:5432 postgres:14

# Add to .env
DATABASE_URL="postgresql://postgres:secret@localhost:5432/chainlit_db"

# Run migrations (clone and run official data layer repo)
# See CHAINLIT_DATA_LAYER_SETUP.md for details
```

### 4. Run the Application

```bash
./run.sh
```

Visit `http://localhost:8000`, sign in with Google, and start chatting!

## How It Works

### Conversation Persistence

EagleAgent uses a two-layer persistence system:

1. **Chainlit Data Layer (PostgreSQL)**:
   - Stores conversation metadata, messages, and elements
   - Powers the chat history sidebar UI
   - Enables browsing past conversations

2. **LangGraph Checkpoints (Firestore)**:
   - Stores the actual conversation state
   - Enables seamless conversation resumption
   - Automatic TTL cleanup after 7 days

When you resume a conversation:
- Chainlit restores the UI from PostgreSQL
- `@cl.on_chat_resume` restores the thread_id
- LangGraph loads the state from Firestore checkpoints
- Your conversation continues exactly where you left off!

## Configuration Files

- **[GOOGLE_OAUTH_SETUP.md](GOOGLE_OAUTH_SETUP.md)**: Step-by-step OAuth setup guide
- **[CHAINLIT_DATA_LAYER_SETUP.md](CHAINLIT_DATA_LAYER_SETUP.md)**: Database setup for conversation history
- **[FIRESTORE_TTL.md](FIRESTORE_TTL.md)**: Configure automatic data expiration
- **[.env.example](.env.example)**: Template for environment variables

## Architecture

- **Chainlit**: Web UI, authentication, and data persistence
- **LangGraph**: Conversation orchestration with state management
- **Firestore**: Checkpoint persistence with TTL policies
- **PostgreSQL**: Conversation history and metadata
- **Google Gemini**: LLM backend

## Utility Scripts

- `./run.sh`: Start the development server
- `./kill.sh`: Stop the server
- `list_checkpoints.py`: View Firestore checkpoints
- `clear_checkpoints.py`: Delete old checkpoints
- `verify_timestamps.py`: Verify TTL timestamps

## Next Steps

- [ ] Deploy to production with proper domain configuration  
- [ ] Set up cloud PostgreSQL (Google Cloud SQL recommended)
- [ ] Configure cloud storage for file attachments
- [ ] Add custom chat profiles for different use cases