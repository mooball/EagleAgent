# EagleAgent

EagleAgent is a sophisticated AI agent built using LangGraph, integrated with a React-based conversational UI via Chainlit and a FastAPI dashboard for supplier/product management. With persistent memory and user profiles, EagleAgent supports multiple complex procurement, research, and administrative operations.

The architecture uses a dual-app pattern: **FastAPI** (`main.py`) handles Google OAuth, session management, and serves the HTMX dashboard, while **Chainlit** (`app.py`) provides the chat UI with LangGraph multi-agent orchestration. Both share a single PostgreSQL database for checkpointing, memory, and application data.

## Key Features

- 🧠 **Persistent Memory**: Uses PostgreSQL for maintaining cross-session memory, user profiles, and LangGraph state.
- 🎨 **Web-based UI**: Powered by Chainlit for beautiful, interactive, and responsive chat.
- 📊 **Dashboard**: FastAPI/HTMX dashboard for suppliers, products, RFQs, and user management.
- 🔐 **Authentication**: Google OAuth 2.0 via FastAPI, with session injection into Chainlit.
- 🌐 **Web Interaction**: Headless Chromium (Playwright via agent-browser) for automated web browsing, scraping, and form-filling.
- 🛠️ **MCP Tools**: Built-in support for Model Context Protocol (MCP) integrations using custom configs.
- 🚀 **Railway Ready**: Optimized dockerization, natively configured for deployment on Railway's App Platform.

## Architecture

![Architecture Diagram](https://img.shields.io/badge/Architecture-Component_Overview-blue.svg)

1. **FastAPI App (`main.py`)**: The ASGI entry point. Handles Google OAuth authentication, session middleware, serves the HTMX dashboard (suppliers, products, RFQs, users), and mounts Chainlit at `/chat`.
2. **Chainlit UI (`app.py`)**: The chat interface where users interact with agents. Features real-time token streaming, chat profiles, and action buttons.
3. **LangGraph Supervisor Pattern (Back-end Orchestration)**: A multi-agent architecture where a central `Supervisor` node evaluates user requests and routes them to specialized sub-agents:
   - **GeneralAgent**: Handles general conversation, context aggregation, memory retrieval, and MCP tool integration.
   - **ProcurementAgent**: Supplier/product database search, purchase history, brand lookup.
   - **ResearchAgent**: Google Search grounding for web research, optional RFQ tools.
   - **SysAdminAgent**: Administrative script execution and job management (admin-only).
   - **BrowserAgent**: Web automation via headless Playwright (available but disabled in main graph).
4. **Dashboard ↔ Chat Bridge** (`includes/agent_bridge.py`): Bidirectional communication — the dashboard can dispatch messages to the agent, and agents can notify the dashboard to refresh.
5. **Storage & Databases** (PostgreSQL & Local File Mount):
   - **PostgreSQL Database**: Holds Chainlit user sessions, LangGraph checkpointing states, cross-thread user profiles, and application data (suppliers, products, brands, RFQs).
   - **Local File Storage**: Uses `/app/data/attachments` (or local equivalent) for fast read/writes without the overhead of external providers.

## Getting Started

### Prerequisites

- [uv](https://github.com/astral-sh/uv) (Python package manager)
- **Node.js**: (Version 20.x or above) for running MCP servers.
- **PostgreSQL Database**: For local dev, a Docker Compose file can be used, or connect straight to your Railway dev instance.

### 1. Installation

Clone the repository and install the dependencies securely using `uv`:

```bash
git clone https://github.com/mooball/EagleAgent.git
cd EagleAgent

# Install the standard project dependencies (creates .venv)
uv sync

# Install Playwright browser contexts
uv pip install playwright
uv run playwright install --with-deps chromium

# Global install of browser-agent dependency for node handling
npm install -g agent-browser@0.16.3
```

### 2. Environment Variables

Copy the `.env.example` file:

```bash
cp .env.example .env
```

Ensure the following variables are filled properly:
```env
# Required Model Access
GOOGLE_API_KEY=your_gemini_api_key

# Database Connection (Same connection handles all logic)
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/eagleagent

# Authentication (Chainlit)
CHAINLIT_AUTH_SECRET=generate_something_random
OAUTH_GOOGLE_CLIENT_ID=your_oauth_id
OAUTH_GOOGLE_CLIENT_SECRET=your_oauth_secret
OAUTH_ALLOWED_DOMAINS=your_domains.com

# File storage location
DATA_DIR=./data
```

### 3. Database Initialization (Alembic)

The database schema, including JSONB conversions required for Chainlit thread states, is managed via **Alembic**. Initialize the database locally or remotely:

```bash
uv run alembic upgrade head
```

### 4. Running the App

Start the application using the run script:

```bash
./run.sh
# OR manually via uvicorn (main.py is the ASGI entry point)
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```
Navigate your browser to `http://localhost:8000` to access the dashboard and chat.

## Deployment on Railway

Deploying EagleAgent on Railway simply involves binding a PostgreSQL database to standard variables.

1. Create a **PostgreSQL Database** within your Railway Project.
2. Import the `EagleAgent` Repository.
3. Add the `.env` configuration securely into the Railway Dashboard (making sure `DATABASE_URL` uses the supplied Private Network URL provided by Railway's Postgres schema `postgresql+asyncpg://`).
4. During Railway setup, add a volume mount mapped to `/app/data` so `DATA_DIR` resolves attachments correctly.
5. The supplied `Dockerfile` builds Python and Node.js resources side-by-side. The app spins up executing `start.sh` where Alembic database migrations trigger automatically preceding node execution.

## Documentation

- [Agent Bridge](./docs/AGENT_BRIDGE.md): Dashboard ↔ Chainlit bidirectional communication architecture.
- [Context Architecture](./docs/CONTEXT_ARCHITECTURE.md): How context and messages flow through the multi-agent system.
- [Cross-Thread Memory](./docs/CROSS_THREAD_MEMORY.md): Persistent user profiles across conversation threads.
- [Development Workflow](./docs/DEVELOPMENT_WORKFLOW.md): Daily dev cycle, database migrations, deployment.
- [File Attachments](./docs/FILE_ATTACHMENTS.md): File upload, processing, and storage.
- [Google OAuth Setup](./docs/GOOGLE_OAUTH_SETUP.md): Setting up Google OAuth authentication.
- [MCP Integration](./docs/MCP_INTEGRATION.md): Model Context Protocol server integration.
- [Server Scripts](./docs/SERVER_SCRIPTS.md): Admin script execution from the chat UI.
- [Testing Guide](./docs/TESTING.md): Running tests and writing new ones.

## License
MIT
