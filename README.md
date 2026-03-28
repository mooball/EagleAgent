# EagleAgent

EagleAgent is a sophisticated AI agent built using LangGraph, integrated with a React-based conversational UI via Chainlit. With persistent memory and user profiles, EagleAgent supports multiple complex browser and code-based operations.

The current architecture exclusively uses PostgreSQL for both application state (Chainlit) and LangGraph checkpointing/memory storage, simplifying deployment and ensuring efficient long-term operations on Railway.

## Key Features

- 🧠 **Persistent Memory**: Uses PostgreSQL for maintaining cross-session memory, user profiles, and LangGraph state.
- 🎨 **Web-based UI**: Powered by Chainlit for beautiful, interactive, and responsive chat.
- 🔐 **Authentication**: Direct Google OAuth 2.0 integration, restricted to allowed domains.
- 🌐 **Web Interaction**: Headless Chromium (Playwright via agent-browser) for automated web browsing, scraping, and form-filling.
- 🛠️ **MCP Tools**: Built-in support for Model Context Protocol (MCP) integrations using custom configs.
- 🚀 **Railway Ready**: Optimized dockerization, natively configured for deployment on Railway's App Platform.

## Architecture

![Architecture Diagram](https://img.shields.io/badge/Architecture-Component_Overview-blue.svg)

1. **Chainlit UI (Front-end)**: The user interface where users interact with the agents. Features real-time token tracking and agent-state routing visibility.
2. **LangGraph Supervisor Pattern (Back-end Orchestration)**: A scalable multi-agent architecture where a central `Supervisor` node evaluates user requests and dynamically routes them to specialized modular sub-agents:
   - **GeneralAgent**: Handles general conversation, context aggregation, memory retrieval, task planning, and document summarization.
   - **BrowserAgent**: Specialized for web search, web automation, opening URLs, taking headless Playwright screenshots, and scraping live data.
   - *(Extensible design prepared for future sub-agents like CodeAgent, DataAgent, etc.)*
3. **Storage & Databases** (PostgreSQL & Native File Mount):
   - **PostgreSQL Database**: Holds Chainlit user sessions (`users`, `threads`, `steps`, `elements` tables) as well as the LangGraph checkpointing states directly.
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

Start the application using Chainlit's standard run syntax:

```bash
uv run run.sh
# OR manually
uv run chainlit run app.py -w
```
Navigate your browser to `http://localhost:8000` to interact.

## Deployment on Railway

Deploying EagleAgent on Railway simply involves binding a PostgreSQL database to standard variables.

1. Create a **PostgreSQL Database** within your Railway Project.
2. Import the `EagleAgent` Repository.
3. Add the `.env` configuration securely into the Railway Dashboard (making sure `DATABASE_URL` uses the supplied Private Network URL provided by Railway's Postgres schema `postgresql+asyncpg://`).
4. During Railway setup, add a volume mount mapped to `/app/data` so `DATA_DIR` resolves attachments correctly.
5. The supplied `Dockerfile` builds Python and Node.js resources side-by-side. The app spins up executing `start.sh` where Alembic database migrations trigger automatically preceding node execution.

## Documentation

- [File Attachments](./docs/FILE_ATTACHMENTS.md): Overview on how files, metadata, and images route seamlessly.
- [Cross-Thread Memory](./docs/CROSS_THREAD_MEMORY.md): Dive into persisting long-term profiling parameters.
- [Server Scripts](./docs/SERVER_SCRIPTS.md): Admin script execution from the chat UI — embedding updates, imports, and more.
- [Testing Guide](./docs/TESTING.md): Run Python tests and manage graph verification nodes.
- [Context Architecture](./docs/CONTEXT_ARCHITECTURE.md): Structural information about component binding logic.

## License
MIT
