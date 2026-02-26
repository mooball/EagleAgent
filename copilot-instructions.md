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
- Never hard-code API keys or secrets.
- Read configuration from environment variables, loaded via `python-dotenv` from `.env`.
- Keep an example file: `.env.example` updated whenever new config is added.
- Validate required env vars early (e.g. at startup) and fail with clear messages.

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

## Git & Repository
- Do not commit `.env`, `.venv`, or other secrets / local artifacts.
- Keep `pyproject.toml` as the single source of truth for dependencies and project metadata.
- When adding scripts, prefer small shell wrappers (`run.sh`, `kill.sh`, etc.) that call `uv` rather than direct `python`.

## When Unsure
- Prefer LangGraph-based designs for new model workflows.
- Prefer adding small, well-named modules over growing a single large file.
- Keep config, secrets, and infra concerns separate from core business logic.
