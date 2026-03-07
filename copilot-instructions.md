# Copilot Instructions for EagleAgent

## Language & Tooling
- Default language: Python 3.12.
- Dependency & environment manager: `uv` (no `pip` or `venv` commands unless explicitly requested).
- Prefer standard library over extra deps when reasonable.
- Use type hints everywhere (functions, class attributes, public APIs).

## Project Structure
- Keep application code in the repo root for now (`app.py`, small modules).
- As the project grows, prefer a `src/` layout:
  - `src/eagleagent/` for core code.
  - `src/eagleagent/agents/` for LangGraph graphs and nodes.
  - `src/eagleagent/ui/` for Chainlit-specific glue code.
- Avoid deeply nested packages unless clearly justified.

## Model & Agent Design
- Default orchestration library: **LangGraph**.
- Default UI: **Chainlit**.
- For any model-facing feature:
  - Prefer implementing it as a LangGraph graph (with `StateGraph` or `MessageGraph`) instead of ad-hoc function chains.
  - Use explicit state types (TypedDict / Pydantic models) for graph state.
  - If in doubt, centralize model clients in a dedicated module (e.g. `models.py` or `src/eagleagent/models.py`).
- Use streaming where supported and expose it through Chainlit for better UX.

## Environment & Configuration

### Configuration Module (`config/settings.py`)
- **Non-secret configuration** (project IDs, bucket names, model names, etc.) lives in `config/settings.py`.
- This file is **version-controlled** and provides defaults for all configuration values.
- Configuration values can be overridden by environment variables if needed.
- To add a new configuration setting:
  1. Add it to the `Config` class in `config/settings.py` with a sensible default.
  2. Use `os.getenv("VAR_NAME", "default_value")` to allow environment override.
  3. Document the setting with a comment.
  4. Import and use: `from config import config` then `config.YOUR_SETTING`.

### Secrets & Environment Variables
- **Secrets** (API keys, OAuth secrets, auth tokens) must **never** be in version control.
- Store secrets in `.env` file locally (ignored by git).
- Read secrets using `os.getenv("SECRET_NAME")` directly in code, **not** via config module.
- Keep `.env.example` updated whenever new secrets are added (with placeholder values).

### Configuration Hierarchy
1. **Defaults**: Defined in `config/settings.py` (version-controlled)
2. **Environment Variables**: Override defaults when present (`.env` locally, GitHub Secrets in CI/CD)
3. **Runtime**: Application uses values from `config` module or `os.getenv()` for secrets

### When to Use Each Approach
- **Use `config/settings.py`** for:
  - Non-secret settings (project IDs, bucket names, model names)
  - Settings that should be visible and auditable
  - Settings that are the same across dev/prod (or have sensible defaults)
  
- **Use `.env` / environment variables** for:
  - API keys, passwords, OAuth secrets
  - Environment-specific secrets (different keys for dev/prod)
  - Local development overrides

### Validation
- Validate required configuration early at startup using `config.validate()` if needed.
- Fail fast with clear error messages when required config is missing.

### Code Examples

**Using configuration values:**
```python
from config import config

# Use configuration settings
project_id = config.GCP_PROJECT_ID
bucket_name = config.Local File Storage_BUCKET_NAME
model = config.DEFAULT_MODEL
```

**Using secrets:**
```python
import os
from dotenv import load_dotenv

load_dotenv()

# Secrets are read directly from environment
api_key = os.getenv("GOOGLE_API_KEY")
oauth_secret = os.getenv("OAUTH_GOOGLE_CLIENT_SECRET")
```

**Mixed approach (typical in application code):**
```python
import os
from dotenv import load_dotenv
from config import config

load_dotenv()

# Configuration from config module
model = ChatGoogleGenerativeAI(
    model=config.DEFAULT_MODEL,  # From config/settings.py
    google_api_key=os.getenv("GOOGLE_API_KEY")  # Secret from .env
)
```

### GitHub Secrets & Cloud Deployment
- **GitHub Secrets** should only contain actual secrets (API keys, OAuth credentials, service account keys).
- **Non-secret configuration** is now in `config/settings.py`, not in GitHub Secrets.
- When deploying to Railway:
  - Secrets are passed as environment variables via `--set-env-vars` in deployment commands.
  - Configuration values come from `config/settings.py` (baked into the Docker image).
  - Cloud-specific overrides can still use environment variables if needed.
- See `CLOUD_RUN_DEPLOYMENT.md` for deployment documentation.

## Chainlit
- Use `chainlit` handlers as the boundary between UI and core logic:
  - `@cl.on_chat_start` should set up any per-session IDs / LangGraph thread IDs.
  - `@cl.on_message` should:
    - Construct the LangGraph input state from the incoming message.
    - Call the graph (`ainvoke` or streaming APIs).
    - Stream tokens or final responses back to the UI.
- Keep Chainlit handlers thin by delegating logic to separate modules.

## LangGraph
- Use `MemorySaver` (or another checkpointer) when conversation state must persist across turns.
- Prefer named nodes with single, clear responsibilities.
- When adding tools / branches later:
  - Keep node functions small and pure where possible.
  - Encapsulate external I/O (APIs, DB, filesystem) in dedicated helper modules.

## Error Handling & Logging
- Fail fast on configuration issues (missing env vars, invalid model names).
- For user-facing errors:
  - Capture exceptions in LangGraph/Chainlit handlers.
  - Send a friendly error message back through Chainlit, and log the technical details.
- Prefer Python `logging` over `print` for non-debug output.

## Style & Quality
- Follow PEP 8 style and PEP 484 type hints.
- Use descriptive variable and function names (avoid single-letter names except for very short scopes).
- Prefer small, composable functions over large monoliths.
- When adding tests, use `pytest` and keep tests in a top-level `tests/` folder.

## Testing
- **Always use `uv run pytest`** to run tests, not direct `pytest` commands.
- The `pytest` script:
  - Automatically checks if PostgreSQL emulator is running.
  - Starts the emulator if needed (on `localhost:8686`).
  - Sets the `FIRESTORE_EMULATOR_HOST` environment variable.
  - Runs `pytest` with proper environment setup.
  - Accepts additional pytest arguments: `uv run pytest -k test_name` or `uv run pytest -v`.
- Tests require PostgreSQL emulator for:
  - Checkpoint persistence tests (`test_checkpoint_saver.py`)
  - PostgreSQL store tests (`test_firestore_store.py`)
  - Integration tests that use checkpointer/store
- To manually start/stop the emulator:
  - Start: `gcloud emulators firestore start --host-port=localhost:8686`
  - Stop: `pkill -f "firestore"`
  - Check status: `ps aux | grep cloud-firestore-emulator`

## Git & Repository
- Do not commit `.env`, `.venv`, or other secrets / local artifacts.
- Keep `pyproject.toml` as the single source of truth for dependencies and project metadata.
- When adding scripts, prefer small shell wrappers (`run.sh`, `kill.sh`, etc.) that call `uv` rather than direct `python`.

## When Unsure
- Prefer LangGraph-based designs for new model workflows.
- Prefer adding small, well-named modules over growing a single large file.
- Keep config, secrets, and infra concerns separate from core business logic.
