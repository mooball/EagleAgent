## Plan: Google Search Grounding & Per-Agent Model Configuration

### Overview

Add Google Search grounding to the BrowserAgent so it can use real-time web search results when answering questions. This requires passing a `google_search` tool via `ChatGoogleGenerativeAI`'s native integration (not a LangChain tool — this is a Gemini API-level feature). Since grounding is a model-level capability, introduce per-agent model configuration so that individual agents can use different models and model options without affecting the rest of the system.

---

### Background: How Google Search Grounding Works

Google Search grounding is a **Gemini API feature**, not a LangChain tool. It is passed as part of the model's configuration, and the model decides when to invoke web search based on the prompt.

Using the raw Google GenAI SDK:

```python
from google.genai import types

search_tool = types.Tool(google_search=types.GoogleSearch())

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="...",
    config=types.GenerateContentConfig(tools=[search_tool])
)
```

In LangChain, `ChatGoogleGenerativeAI` (from `langchain-google-genai`) supports this via the `additional_tools` parameter. The relevant LangChain integration for Gemini grounding is using `google.genai.types.Tool` objects passed alongside LangChain tools — they are sent directly to the Gemini API as native tools, separate from the LangChain tool execution loop.

---

### Phase 1: Per-Agent Model Configuration in Settings

Add environment variables to `config/settings.py` that allow overriding the model on a per-agent basis. Each agent gets an optional `<AGENT>_MODEL` environment variable that falls back to `DEFAULT_MODEL`.

1. **Add to `config/settings.py`** under the Model Configuration section:
   ```python
   # Per-agent model overrides (fall back to DEFAULT_MODEL if not set)
   BROWSER_AGENT_MODEL = os.getenv("BROWSER_AGENT_MODEL", "")
   GENERAL_AGENT_MODEL = os.getenv("GENERAL_AGENT_MODEL", "")
   PROCUREMENT_AGENT_MODEL = os.getenv("PROCUREMENT_AGENT_MODEL", "")
   SUPERVISOR_MODEL = os.getenv("SUPERVISOR_MODEL", "")
   ```

2. **Add a helper method** to `Config`:
   ```python
   @classmethod
   def get_agent_model(cls, agent_name: str) -> str:
       """Get the model for a specific agent, falling back to DEFAULT_MODEL."""
       agent_model_map = {
           "BrowserAgent": cls.BROWSER_AGENT_MODEL,
           "GeneralAgent": cls.GENERAL_AGENT_MODEL,
           "ProcurementAgent": cls.PROCUREMENT_AGENT_MODEL,
           "Supervisor": cls.SUPERVISOR_MODEL,
       }
       model = agent_model_map.get(agent_name, "")
       return model if model else cls.DEFAULT_MODEL
   ```

3. **Update `.env.example`** and `.env.docker.example` with commented entries:
   ```bash
   # Per-agent model overrides (optional, defaults to DEFAULT_MODEL)
   # BROWSER_AGENT_MODEL=gemini-2.5-flash
   # GENERAL_AGENT_MODEL=gemini-3-flash-preview
   # PROCUREMENT_AGENT_MODEL=gemini-3-flash-preview
   # SUPERVISOR_MODEL=gemini-3-flash-preview
   ```

---

### Phase 2: Update `app.py` to Create Per-Agent Models

Instead of passing a single `base_model` to all agents, create agent-specific model instances using the config helper.

1. **In `app.py` `setup_globals()`**, replace the single model pattern:
   ```python
   # Before (single model for all agents):
   base_model = ChatGoogleGenerativeAI(model=config.DEFAULT_MODEL, ...)
   browser_agent = BrowserAgent(model=base_model, store=store)
   
   # After (per-agent models):
   def create_model(agent_name: str) -> ChatGoogleGenerativeAI:
       return ChatGoogleGenerativeAI(
           model=config.get_agent_model(agent_name),
           google_api_key=os.getenv("GOOGLE_API_KEY")
       )
   
   browser_agent = BrowserAgent(model=create_model("BrowserAgent"), store=store)
   procurement_agent = ProcurementAgent(model=create_model("ProcurementAgent"), store=store)
   general_agent = GeneralAgent(model=create_model("GeneralAgent"), store=store, ...)
   supervisor_node = Supervisor(model=create_model("Supervisor"))
   ```

2. Keep `base_model` as a fallback reference for anything that doesn't have a specific agent name.

---

### Phase 3: Add Google Search Grounding to BrowserAgent

Enable Google Search as a Gemini-native grounding tool on the BrowserAgent. This is separate from LangChain tools — it is passed to the Gemini API alongside the LangChain tools so the model can search the web when it needs real-time information.

1. **Update `BaseSubAgent`** to support native Gemini tools:
   - Add an optional `get_native_tools()` method to `BaseSubAgent` that returns a list of `google.genai.types.Tool` objects (default: empty list).
   - In `BaseSubAgent.__call__()`, when creating the model invocation, pass any native tools via ChathGoogleGenerativeAI's support for additional/native tools.

2. **Update `BrowserAgent`** to return the Google Search grounding tool:
   ```python
   from google.genai import types as genai_types
   
   class BrowserAgent(BaseSubAgent):
       def get_native_tools(self) -> list:
           """Return Gemini-native tools like Google Search grounding."""
           return [genai_types.Tool(google_search=genai_types.GoogleSearch())]
   ```

3. **Update `BaseSubAgent.__call__()`** to merge native tools into the model call:
   - When `get_native_tools()` returns tools, these need to be passed to the model when invoking it.
   - `ChatGoogleGenerativeAI` supports passing `additional_tools` when binding or invoking — investigate the exact mechanism in `langchain-google-genai` to pass `google.genai.types.Tool` objects alongside LangChain tools.
   - The `create_react_agent` call may need adjustment: the native tools should be bound to the model itself (via `model.bind(tools=native_tools)` or similar), while LangChain tools are passed to `create_react_agent` for the tool execution loop.

   **Key implementation detail:** LangChain tools are executed by the ReAct agent loop. Native Gemini tools (like Google Search) are executed server-side by the Gemini API — they don't need a LangChain tool executor. They just need to be included in the API request's tool configuration. The approach is:
   ```python
   # In BaseSubAgent.__call__():
   native_tools = self.get_native_tools()
   if native_tools:
       # Bind native tools to the model so they're included in every API call
       model_with_native = self.model.bind(additional_tools=native_tools)
   else:
       model_with_native = self.model
   
   if tools:
       sub_agent_graph = create_react_agent(model_with_native, tools)
       ...
   ```

   **Note:** The exact parameter name and mechanism for passing native Gemini tools through `ChatGoogleGenerativeAI` needs to be verified against `langchain-google-genai` v4.2.x. Check the `ChatGoogleGenerativeAI` source/docs for the correct parameter — it may be `additional_tools`, `google_tools`, or handled via `model_kwargs`. If the current version doesn't support it natively, we may need to subclass or use `bind()` with the appropriate key.

---

### Phase 4: Testing

1. **Unit tests** — add tests in `tests/agents/`:
   - Test that `BrowserAgent.get_native_tools()` returns a list containing a Google Search tool.
   - Test that `BaseSubAgent.get_native_tools()` returns an empty list by default.
   - Test that `config.get_agent_model()` returns agent-specific models when set, and falls back to `DEFAULT_MODEL` when not.

2. **Integration test** — manually verify:
   - Ask BrowserAgent a question that requires current web information (e.g., "What is the current price of gold?").
   - Verify the response includes grounded search results (the response metadata should contain `grounding_metadata` with search entries and URLs).

3. **Verify no regressions:**
   - GeneralAgent and ProcurementAgent should continue to work without Google Search grounding.
   - The default model fallback works when no agent-specific model env vars are set.

---

### Relevant Files

- `config/settings.py` — Add per-agent model settings and `get_agent_model()` helper.
- `app.py` — Create per-agent model instances using config helper.
- `includes/agents/base.py` — Add `get_native_tools()` hook and update `__call__()` to pass native tools.
- `includes/agents/browser_agent.py` — Override `get_native_tools()` to return Google Search grounding tool.
- `.env.example` / `.env.docker.example` — Document new per-agent model env vars.
- `tests/agents/test_browser_agent.py` — Test Google Search grounding integration.
- `tests/test_settings.py` — Test per-agent model resolution.

---

### Decisions

- **Google Search grounding is a model-level feature**, not a LangChain tool. It is passed to the Gemini API and executed server-side. The model automatically decides when to search based on the query.
- **Per-agent models use environment variables** with fallback to `DEFAULT_MODEL`. This keeps the pattern consistent with existing config (no new config files needed).
- **The `get_native_tools()` hook is opt-in.** Agents that don't override it get no native tools (empty list). This means Google Search is only active on agents that explicitly request it.
- **No new dependencies required.** `google-genai` types are already available via the existing `langchain-google-genai` dependency which depends on `google-genai`.

### Questions / Considerations

1. **langchain-google-genai compatibility:** The exact mechanism for passing native `google.genai.types.Tool` objects through `ChatGoogleGenerativeAI` needs verification against the installed v4.2.x. If the integration doesn't support it cleanly, we may need to upgrade `langchain-google-genai` or use the `google-genai` SDK directly for the BrowserAgent's model.
2. **Cost implications:** Google Search grounding counts as an additional API call and may increase per-request cost. Consider whether it should be gated behind a feature flag (e.g., `ENABLE_GOOGLE_SEARCH_GROUNDING=true`) for cost control.
3. **Grounding metadata display:** The Gemini API returns `grounding_metadata` with source URLs when search grounding is used. Should we surface these URLs in the chat UI (e.g., as footnotes or a sources section)?
