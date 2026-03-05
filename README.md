
# EagleAgent

AI-powered assistant using Google Gemini and LangGraph, with Google OAuth authentication and complete conversation persistence.

## Features

- 🤖 **Google Gemini Integration**: Powered by gemini-3-flash-preview with multimodal support
- 🌐 **Browser Automation**: Includes a dedicated `BrowserAgent` capable of autonomous web browsing, interacting with pages, and taking screenshots (powered by `agent-browser` + Playwright).
- 🔐 **Google OAuth Authentication**: Secure login with Google accounts
- 💬 **Chat History**: Browse and resume past conversations
- 📎 **File Attachments**: Upload images, PDFs, text files, and audio with automatic processing
- 🖼️ **Vision Analysis**: Gemini vision capabilities for image understanding
- 📄 **Document Processing**: Automatic text extraction from PDFs and documents
- 💾 **Dual Persistence**: 
  - Chainlit data layer for conversation UI/metadata
  - Firestore checkpoints for LangGraph state
  - Google Cloud Storage for file attachments
- ⏰ **Automatic TTL**: Conversations expire after 7 days, files after 30 days (configurable)
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

Follow [CHAINLIT_DATA_LAYER_SETUP.md](CHAINLIT_DATA_LAYER_SETUP.md) for complete setup instructions.

**Quick setup:**
```bash
# Install required packages
uv add sqlalchemy aiosqlite greenlet

# Initialize SQLite database
uv run init_sqlite_db.py

# DATABASE_URL is already configured in .env
# DATABASE_URL=sqlite+aiosqlite:///./chainlit_datalayer.db
```

### 4. Configure Cloud Storage (Optional but Recommended)

For persistent file storage, create a Google Cloud Storage bucket:

```bash
# Using gcloud CLI
gcloud storage buckets create gs://YOUR_BUCKET_NAME --location=us-central1

# Or create manually in GCP Console: https://console.cloud.google.com/storage
```

Add the bucket name to your `.env` file:

```bash
GCP_BUCKET_NAME=YOUR_BUCKET_NAME
```

**Note**: File uploads work without GCS configured, but files will only be processed temporarily and won't persist across restarts. For production, GCS is required.

### 5. Run the Application

```bash
./run.sh
```

Visit `http://localhost:8000`, sign in with Google, and start chatting!

## How It Works

### Conversation Persistence

EagleAgent uses a two-layer persistence system:

1. **Chainlit Data Layer (SQLite)**:
   - Stores conversation metadata, messages, and elements
   - Powers the chat history sidebar UI
   - Enables browsing past conversations
   - File-based database (`chainlit_datalayer.db`)

2. **LangGraph Checkpoints (Firestore)**:
   - Stores the actual conversation state
   - Enables seamless conversation resumption
   - Automatic TTL cleanup after 7 days

When you resume a conversation:
- Chainlit restores the UI from SQLite
- `@cl.on_chat_resume` restores the thread_id
- LangGraph loads the state from Firestore checkpoints
- Your conversation continues exactly where you left off!

### Cross-Thread Memory & User Profiles
# File Attachments & Document Processing

EagleAgent supports uploading and processing various file types with intelligent content extraction:

**Supported File Types**:
- **Images** (JPEG, PNG, GIF, etc.): Processed using Gemini's vision capabilities for visual understanding
- **PDFs**: Automatic text extraction from all pages
- **Text files** (.txt, .md, .json, .csv, etc.): Direct content reading
- **Audio files**: Metadata tracking (transcription coming soon)

**How It Works**:

1. **Upload**: Click the paperclip icon and select files (up to 20 files, 50MB each)
2. **Storage**: Files uploaded to Google Cloud Storage bucket (configurable)
3. **Processing**:
   - Images → Base64 encoded and sent to Gemini vision API
   - PDFs → Text extracted using pdfplumber
   - Text files → Content read and included in context
   - Audio → Metadata stored (transcription planned)
4. **Context**: Extracted content automatically included in conversation
5. **Retention**: Files kept for 30 days, then auto-deleted

**File Storage**:
```
GCS Bucket Structure:
uploads/
  {user_id}/
    {thread_id}/
      20260302_143025_document.pdf
      20260302_143030_image.jpg
```

**File Cleanup**:

Run the cleanup script manually or via cron to remove files older than 30 days:

```bash
# Dry run (see what would be deleted)
./scripts/cleanup_old_files.py --dry-run

# Delete files older than 30 days
./scripts/cleanup_old_files.py

# Custom retention period (e.g., 60 days)
./scripts/cleanup_old_files.py --days 60
```

**Key files**:
- `includes/storage_utils.py`: GCS upload/download/delete functions
- `includes/document_processing.py`: File processing and content extraction
- `scripts/cleanup_old_files.py`: Automated file cleanup with TTL

##
EagleAgent includes a sophisticated **cross-thread memory system** that remembers user information across all conversations:

1. **User Profile Store (Firestore)**:
   - Stores user preferences, facts, and preferred names
   - Persists across all conversation threads
   - Users can say "call me X" in any conversation, and all future conversations remember it
   - Accessible via tools (`remember_user_info`, `recall_user_info`)

2. **Context Injection**:
   - User profile loaded from store on each turn
   - System prompt dynamically constructed with latest profile data
   - Agent addresses users by preferred name automatically

**Key files**:
- `includes/firestore_store.py`: Firestore-backed BaseStore implementation
- `includes/user_profile_tools.py`: Tools for saving/recalling user information
- See [CROSS_THREAD_MEMORY.md](CROSS_THREAD_MEMORY.md) for detailed architecture

## Agent Configuration

EagleAgent's behavior, personality, and system prompts are configured in a centralized location for easy customization.

### Modifying Agent Behavior

All agent configuration is managed in **`includes/prompts.py`**:

```python
# Agent identity (name, role, personality traits)
AGENT_CONFIG = {
    "name": "EagleAgent",
    "role": "AI Assistant",
    "personality": {
        "traits": ["Helpful and friendly", "Professional yet approachable"],
        "tone": "conversational and supportive"
    }
}

# Tool usage instructions
TOOL_INSTRUCTIONS = {
    "remember_user_info": {
        "prompt_template": "When the user tells you information..."
    }
}

# User profile context templates
PROFILE_TEMPLATES = {
    "header": "User profile information:",
    "sections": {
        "preferred_name": "- Preferred name: {preferred_name}...",
        "preferences": "- Preferences: {preferences}",
        "facts": "- Facts: {facts}"
    }
}
```

### What You Can Configure

- **Agent Identity**: Change the agent's name, role, and description
- **Personality**: Modify personality traits and conversation tone
- **Tool Instructions**: Customize how tools are introduced/used
- **Profile Context**: Change how user profile information is presented
- **Behavior Guidelines**: Add or modify behavioral rules

### How Context is Constructed

The system prompt sent to the LLM is built using the `build_system_prompt()` function:

1. **With User Profile**:
   ```
   User profile information:
   - Preferred name: Tom (use this to address the user)
   - Preferences: Python, concise explanations
   - Facts: Software engineer
   
   When the user tells you information about themselves...
   ```

2. **Without User Profile** (new users):
   ```
   When the user tells you information about themselves, use the remember_user_info tool...
   ```

See [CONTEXT_ARCHITECTURE.md](CONTEXT_ARCHITECTURE.md) for a complete explanation of how context flows through the system.

### Testing Prompt Changes
- `scripts/cleanup_old_files.py`: Remove expired file attachments from GCS

## Next Steps

- [ ] Deploy to production with proper domain configuration
- [x] Configure cloud storage for file attachments (GCS implemented)
- [ ] Add custom chat profiles for different use cases
- [ ] Consider upgrading to PostgreSQL for production deployments with high traffic
- [ ] Implement audio transcription for uploaded audio files (Whisper API)

Or run the full test suite:

```bash
bash run_tests.sh
```

### Future YAML Migration

The current configuration uses Python dictionaries that are structured to mirror YAML format. When you're ready to make prompts editable by non-developers:

1. Copy dictionary structures from `includes/prompts.py` to `config/prompts.yaml`
2. Add `pyyaml` dependency: `uv add pyyaml`
3. Update prompt loader to read from YAML file
4. See `config/prompts.yaml.example` for the YAML structure

This allows prompt engineering without code changes and enables non-technical team members to modify agent behavior.

## Configuration Files

- **[DEVELOPMENT_WORKFLOW.md](DEVELOPMENT_WORKFLOW.md)**: Quick reference for daily development workflow ⭐
- **[CLOUD_RUN_DEPLOYMENT.md](CLOUD_RUN_DEPLOYMENT.md)**: Complete Google Cloud Run deployment guide
- **[DOCKER_LOCAL_TESTING.md](DOCKER_LOCAL_TESTING.md)**: Testing Docker containers locally before deployment
- **[GOOGLE_OAUTH_SETUP.md](GOOGLE_OAUTH_SETUP.md)**: Step-by-step OAuth setup guide
- **[CHAINLIT_DATA_LAYER_SETUP.md](CHAINLIT_DATA_LAYER_SETUP.md)**: Database setup for conversation history
- **[CROSS_THREAD_MEMORY.md](CROSS_THREAD_MEMORY.md)**: Cross-thread user profile architecture
- **[CONTEXT_ARCHITECTURE.md](CONTEXT_ARCHITECTURE.md)**: Complete guide to context and message flow
- **[FIRESTORE_TTL.md](FIRESTORE_TTL.md)**: Configure automatic data expiration
- **[TESTING.md](TESTING.md)**: Testing guide and best practices
- **[.env.example](.env.example)**: Template for environment variables (local development)
- **[.env.docker.example](.env.docker.example)**: Template for Docker local testing
- **[.env.cloudrun.example](.env.cloudrun.example)**: Template for Cloud Run environment variables
- **`includes/prompts.py`**: Agent configuration and prompt templates

## Deployment

### Local Development vs Production

| Feature | Local Development | Cloud Run Production |
|---------|------------------|---------------------|
| **Database** | SQLite (file-based) | SQLite on GCSFuse volume mount |
| **Checkpoints** | Firestore Emulator or Cloud | Cloud Firestore |
| **Files** | Local `/tmp/files` | GCS bucket + ephemeral `/tmp` |
| **Auth** | Service account key file | Application Default Credentials |
| **Scaling** | Single instance | Auto-scaling 0-10 instances |
| **Region** | N/A | Australia (Sydney) |
| **Cost** | Free (local) | ~$26/month (light usage) |

### Production Deployment (Google Cloud Run)

Deploy EagleAgent to Google Cloud Run with persistent storage using GCSFuse volume mounts:

```
┌─────────────────────────────────┐
│   Google Cloud Run (Gen2)       │
│   - Python 3.12 + Node.js 20    │
│   - Auto-scaling (0-10)         │
│   - 2Gi RAM, 2 vCPU             │
└────────┬───────────────┬────────┘
         │               │
    /data mount     /tmp/files
         │          (ephemeral)
         ▼
┌────────────────────────┐
│  GCS Bucket (GCSFuse)  │
│  ├─ database/          │ ← SQLite persistence
│  ├─ uploads/           │ ← File attachments
│  └─ mcp_credentials/   │ ← MCP OAuth tokens
└────────────────────────┘
         │
    ┌────┴──────────────┐
    ▼                    ▼
Firestore            OAuth 2.0
```

**Key Features**:
- ✅ **True persistence**: SQLite database on GCS-mounted volume (survives restarts)
- ✅ **Auto-scaling**: Scales to zero when idle, up to 10 instances under load
- ✅ **Multi-runtime**: Single container with Python + Node.js for MCP servers
- ✅ **Secure**: Encrypted environment variables, Workload Identity for GitHub Actions
- ✅ **Cost-effective**: ~$26/month light usage, ~$5-10/month with scale-to-zero
- ✅ **Australian region**: Deployed to Sydney for low latency

**Quick Deploy**:

```bash
# See full guide for prerequisites and setup
# CLOUD_RUN_DEPLOYMENT.md

# One-line deploy
gcloud run deploy eagleagent \
  --source . \
  --region australia-southeast1 \
  --execution-environment gen2 \
  --memory 2Gi \
  --concurrency 1 \
  --add-volume name=gcs-data,type=cloud-storage,bucket=eagleagent-data \
  --add-volume-mount volume=gcs-data,mount-path=/data
```

**Local Testing**:

Before deploying to Cloud Run, test the Docker container locally:

```bash
# Build Docker image
docker build -t eagleagent:local .

# Run with local Firestore emulator
docker run -d --name eagleagent \
  -v /tmp/eagleagent-data:/data \
  -p 8080:8080 \
  --env-file .env.docker \
  eagleagent:local

# Access at http://localhost:8080
open http://localhost:8080
```

📖 **See [DOCKER_LOCAL_TESTING.md](DOCKER_LOCAL_TESTING.md) for complete local testing guide**

**CI/CD Options**:
- **GitHub Actions**: Automated deployment on push to `main` (see [.github/workflows/deploy-cloud-run.yml](.github/workflows/deploy-cloud-run.yml))
- **Cloud Build**: GCP-native CI/CD alternative (see [cloudbuild.yaml](cloudbuild.yaml))

📖 **See [CLOUD_RUN_DEPLOYMENT.md](CLOUD_RUN_DEPLOYMENT.md) for complete deployment instructions**

## Architecture

- **Chainlit**: Web UI, authentication, and data persistence
- **LangGraph**: Conversation orchestration with state management
- **Firestore**: Checkpoint persistence with TTL policies
- **SQLite**: Conversation history and metadata
- **Google Gemini**: LLM backend

## Utility Scripts

- `./run.sh`: Start the development server
- `./kill.sh`: Stop the server
- `scripts/list_checkpoints.py`: View Firestore checkpoints
- `scripts/clear_checkpoints.py`: Delete old checkpoints
- `scripts/verify_timestamps.py`: Verify TTL timestamps
- `scripts/manage_user_profile.py`: View/edit user profiles in the cross-thread store

## Next Steps

- [ ] Deploy to production with proper domain configuration
- [ ] Configure cloud storage for file attachments (GCS, S3, or Azure)
- [ ] Add custom chat profiles for different use cases
- [ ] Consider upgrading to PostgreSQL for production deployments with high traffic