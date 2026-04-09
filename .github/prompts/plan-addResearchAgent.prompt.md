## Plan: Add Research Agent with Google Search Grounding

### TL;DR
Create a new `ResearchAgent` with its own single-agent graph (`research_graph`), powered by Gemini's native Google Search grounding. Admin-only initially. Follows the `SysAdminAgent` single-agent graph pattern.

---

### Phase 1: Config — add Research Agent model setting
1. Add `RESEARCH_AGENT_MODEL` env var to `config/settings.py` (default: empty → falls back to `DEFAULT_MODEL`)
2. Add `"ResearchAgent"` entry to `agent_model_map` in `get_agent_model()`
3. Document in `.env.example` / `.env.docker.example`

---

### Phase 2: Create ResearchAgent class
1. Create `includes/agents/research_agent.py` — new file, modeled on `includes/agents/sysadmin_agent.py`:
   - `get_tools()` → profile tools via `create_profile_tools()` (user memory/personalization)
   - `get_native_tools()` → `[genai_types.Tool(google_search=genai_types.GoogleSearch())]` — the key differentiator; Gemini executes search server-side
   - `get_system_prompt_async()` → loads user profile, returns research-focused prompt
2. Add `build_research_prompt(profile_data)` to `includes/prompts.py` — research-oriented personality (cite sources, analyze, synthesize)
3. Export `ResearchAgent` from `includes/agents/__init__.py`
4. No changes to `includes/agents/base.py` — `get_native_tools()` hook and native tool binding in `__call__` already exist

---

### Phase 3: Wire up research_graph in app.py
1. Add global `research_graph = None` declaration
2. In `setup_globals()`, after `sysadmin_graph`: instantiate `ResearchAgent` with `create_model("ResearchAgent")` + `store`, build graph (`START → ResearchAgent → END`)
3. In `on_chat_start` / `on_chat_resume`: `"Research Agent"` profile → `research_graph`
4. In `set_chat_profiles`: move Research Agent profile inside `if is_admin:` block (admin-only)

---

### Phase 4: Tests
1. New `tests/agents/test_research_agent.py` — test `get_native_tools()` returns Google Search tool, `get_tools()` returns profile tools, `get_system_prompt()` returns research-focused string
2. Update `tests/test_settings.py` — test `get_agent_model("ResearchAgent")` override and fallback

---

### Verification
1. `uv run pytest tests/` — all existing + new tests pass
2. Manual: admin sees Research Agent in profile dropdown; non-admin does not
3. Manual: Research Agent answers current-events question with grounded web results
4. Manual: Eagle Agent remains unaffected by Google Search grounding

### Decisions
- **Own single-agent graph** (no supervisor routing) — simplest approach, can add more agents later
- **Uses existing `get_native_tools()` infrastructure** in `base.py` (lines 132-145, handled in `__call__` lines 244-261)
- **Profile tools included** for user memory/personalization
- **Admin-only** via `is_admin` gate in `set_chat_profiles`
- **New research-focused prompt** — not reusing GeneralAgent's procurement-oriented prompt
- **No new dependencies** — `google.genai.types` already available via `langchain-google-genai`
