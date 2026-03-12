# EagleAgent Codebase Cleanup Plan

**Overall: 7.5/10** - Solid foundation with good patterns established, but some cleanup needed before adding more agents.

---

## RED - Fix Before Proceeding

### ~~1. GeneralAgent violates BaseSubAgent contract~~ DONE
- Added async hooks (`get_tools_async`, `get_system_prompt_async`) to `BaseSubAgent`
- Moved message trimming + checkpoint cleanup into base `__call__()`
- Refactored GeneralAgent to use hooks instead of overriding `__call__()`

### ~~2. Unused/redundant dependencies in pyproject.toml~~ DONE
- Removed `psycopg2-binary` and `langgraph-checkpoint-firestore`
- Switched all deps from `>=` to `~=` (compatible-release pinning)
- Dropped 7 packages from lockfile

### ~~3. `agents.yaml.example` is misleading~~ DONE
- Removed `config/agents.yaml.example` — routing is handled by `includes/agents/supervisor.py`

### ~~4. Dockerfile runs as root~~ DONE
- Added non-root `eagleagent` user (uid 1000) with proper ownership
- Added `HEALTHCHECK` for orchestrator liveness detection (also covers #10)

---

## ORANGE - Important Improvements

### ~~5. Unused imports in app.py~~ DONE
- Removed `AIMessage`, `BaseStore`, and `operator`

### ~~6. Message trimming is inconsistent~~ DONE
- Centralized in `BaseSubAgent.__call__()` as part of #1 refactor

### ~~7. Duplicate profile initialization logic~~ DONE
- Extracted `_ensure_user_profile()` helper in `app.py`
- Both `start()` and `on_chat_resume()` now call the shared helper

### ~~8. No tests for GeneralAgent~~ DONE
- Added 22 tests in `tests/agents/test_general_agent.py`
- Covers: init, sync/async tool retrieval, MCP integration, role filtering, system prompts, agent execution, message trimming, edge cases

### 9. TESTING.md references Firestore
- `TESTING.md` mentions "Firestore" in several places but the entire system uses PostgreSQL. Confusing for new devs.

### ~~10. Missing Dockerfile health check~~ DONE
- Added via #4 Dockerfile fix

---

## YELLOW - Nice to Have

### 11. Storage module boundary unclear
- `includes/storage_utils.py` and `includes/local_storage_client.py` overlap in functionality. Neither has a module docstring explaining when to use which.

### 12. Empty `__init__.py` in agents module
- `includes/agents/__init__.py` doesn't export anything, requiring full import paths everywhere.

### 13. Shell scripts inconsistent
- `start.sh` has proper `set -e` and env validation. `run.sh` and `kill.sh` have no error handling, no feedback, and `kill.sh` could kill unrelated processes.

### 14. CodeAgent/DataAgent stubs
- Both are non-functional placeholder files. Either complete them or remove them from the tree to reduce confusion. README.md mentions them as if they exist.

### 15. Supervisor routing keywords hardcoded
- Browser keywords are a hardcoded list in `includes/agents/supervisor.py`. Would benefit from being configurable (this is what `agents.yaml` could actually solve).

---

## What's Working Well

- **Supervisor pattern** is clean and the hybrid rule-based + LLM routing is a good architecture
- **Config management** (`config/settings.py`) is excellent — clean defaults, env overrides, secret masking
- **Prompts** (`includes/prompts.py`) are gold standard — well-structured, dynamic, self-documenting
- **Test quality** for what IS tested is high — especially test_prompts.py and test_user_profile.py
- **Documentation** for context architecture, cross-thread memory, and MCP is thorough
- **PostgreSQL persistence** layer is solid (checkpointer + store + Alembic migrations)

---

## Recommended Execution Order

1. Clean up dependencies (`psycopg2-binary`, `langgraph-checkpoint-firestore`, version pinning)
2. Fix GeneralAgent to comply with BaseSubAgent contract (or create AsyncBaseSubAgent)
3. Centralize message trimming in base.py
4. Remove or wire up `agents.yaml.example`
5. Add Dockerfile security (non-root user, health check)
6. Write GeneralAgent tests
7. Fix TESTING.md Firestore references
8. Extract duplicate profile logic in app.py
