## Plan: Multi-Agent Architecture with Browser Sub-Agent

**TL;DR:** Implement a scalable multi-agent architecture starting with a browser sub-agent for web automation. Phase 1 adds the browser agent as a specialized tool with agent-browser CLI integration. Phase 2 refactors to a supervisor pattern supporting multiple future sub-agents (code, data analysis, etc.). Uses hybrid routing (rules + LLM fallback) and organizes code in `includes/agents/` and `includes/tools/` for clarity. Proven pattern from research: shared state, base agent abstraction, domain-specific tool binding.

**Steps**

**Phase 1: Foundation & Browser Agent (Week 1-2) - COMPLETED**

- [x] 1. **Create folder structure for multi-agent system**
   - Create [includes/agents/](includes/agents/) directory with `__init__.py`
   - Create [includes/agents/base.py](includes/agents/base.py) with `BaseSubAgent` class (shared patterns for all future agents)
   - Create [includes/tools/](includes/tools/) directory with `__init__.py`
   - Move [includes/user_profile_tools.py](includes/user_profile_tools.py) → [includes/tools/user_profile.py](includes/tools/user_profile.py)
   - Update import in [app.py](app.py#L19) from `includes.user_profile_tools` to `includes.tools.user_profile`
   - Run tests to verify refactor didn't break anything

- [x] 2. **Install agent-browser and verify setup**
   - Add to [pyproject.toml](pyproject.toml): `agent-browser = "^0.16.3"` (latest version)
   - Run `uv sync` to install
   - Test CLI: `agent-browser --version` and `agent-browser install` to download Chromium
   - Verify `agent-browser --json` outputs valid JSON

- [x] 3. **Create browser tools module**
   - Create [includes/tools/browser_tools.py](includes/tools/browser_tools.py)
   - Implement `browser()` tool using subprocess to call `agent-browser` CLI
   - Add error handling (timeouts, invalid commands, session management)
   - Tool docstring includes condensed workflow from agent-browser SKILL.md
   - Add helper function `create_browser_tools()` following user_profile pattern

- [x] 4. **Create browser agent**
   - Create [includes/agents/browser_agent.py](includes/agents/browser_agent.py) extending `BaseSubAgent`
   - Override `get_tools(user_id)` to return browser tools
   - Override `get_system_prompt()` with browser-specific instructions (from agent-browser SKILL.md)
   - Implement browser session cleanup in agent lifecycle
   - Agent manages agent-browser session state (refs from snapshots, current URL, etc.)

- [x] 5. **Add browser agent prompts**
   - Update [includes/prompts.py](includes/prompts.py#L58-L85) `TOOL_INSTRUCTIONS` dict
   - Add `"use_browser_agent"` entry with concise delegation instructions (~50 tokens)
   - Add `AGENT_PROMPTS["browser_agent"]` with full browser workflow (~500 tokens, only used when browser agent active)
   - Modify `build_system_prompt()` to accept `available_tool_names` parameter (dynamic instructions)

- [x] 6. **Integrate browser agent into main graph (as tool for now)**
   - In [app.py](app.py#L77-L93) `call_model()`, create `use_browser_agent` delegation tool
   - Tool invokes browser agent sub-graph and returns results
   - Main agent sees one tool: `use_browser_agent(task: str) -> str`
   - Browser agent handles all agent-browser CLI complexity internally

**Phase 2: Testing & Validation (Week 2) - COMPLETED**

- [x] 7. **Create browser agent tests**
   - Create [tests/agents/](tests/agents/) directory structure
   - Create [tests/agents/test_browser_agent.py](tests/agents/test_browser_agent.py)
   - Test: Browser agent initialization and tool loading
   - Test: Browser command execution (mocked subprocess)
   - Test: Error handling (timeouts, invalid commands)
   - Test: Session cleanup

- [x] 8. **Create integration tests**
   - In [tests/test_integration.py](tests/test_integration.py), add browser agent scenarios
   - Test: Main agent delegates browse request to browser agent
   - Test: Browser agent returns results to main agent
   - Test: Multi-turn browser tasks (search, then click, then extract)
   - Use mocked agent-browser responses (no actual browser needed in tests)

- [x] 9. **Update documentation**
   - Create [includes/agents/README.md](includes/agents/README.md) explaining sub-agent pattern
   - Document base agent abstraction and how to add new agents
   - Update main [README.md](README.md) with browser capabilities
   - Add agent-browser setup instructions to deployment docs

**Phase 3: Supervisor Refactor (Week 3-4) - COMPLETED**

- [x] 10. **Extract current agent as GeneralAgent**
    - Create [includes/agents/general_agent.py](includes/agents/general_agent.py)
    - Move `call_model()` logic from [app.py](app.py#L68-L112) into GeneralAgent class
    - GeneralAgent gets profile tools + MCP tools (not browser tools)
    - Verify existing functionality still works

- [x] 11. **Create supervisor node**
    - Create [includes/agents/supervisor.py](includes/agents/supervisor.py)
    - Implement `route_to_agent()` function with hybrid routing:
      - Rule-based: Check keywords (search/browse → browser, code/implement → future code agent)
      - LLM fallback: Use fast model to classify intent for ambiguous cases
    - Supervisor has minimal system prompt (just routing instructions)

- [x] 12. **Rebuild graph with supervisor pattern**
    - In [app.py](app.py#L152-L162), change from simple loop to supervisor pattern:
      ```
      START → supervisor → [general_agent | browser_agent] → supervisor → END
      ```
    - Update `AgentState` → `SupervisorState` (add `next_agent` field)
    - Add conditional edges based on routing decision
    - Each agent returns to supervisor (allows chaining: browse, then summarize)

- [x] 13. **Migration & backward compatibility**
    - Ensure existing thread_ids still work (state format unchanged)
    - Test with existing conversation history in Firestore
    - Verify user profiles still load correctly
    - Check file attachments flow through supervisor

**Phase 4: Polish & Future Agents (Week 4+) - COMPLETED**

- [x] 14. **Optimize context management**
    - Implement dynamic tool instructions in [includes/prompts.py](includes/prompts.py#L177-L230)
    - Only include instructions for agents/tools actually available in current context
    - Measure token usage before/after (expect ~40% reduction for non-browser conversations)

- [x] 15. **Prepare for future agents**
    - Document agent creation template in [includes/agents/README.md](includes/agents/README.md)
    - Create [config/agents.yaml.example](config/agents.yaml.example) for agent routing rules
    - Add metrics/logging for agent usage (which agents invoked, success rates)
    - Create placeholder files with TODOs:
      - [includes/agents/code_agent.py](includes/agents/code_agent.py) (code generation/debugging)
      - [includes/agents/data_agent.py](includes/agents/data_agent.py) (data analysis/visualization)

- [x] 16. **Testing & monitoring**
    - Add supervisor routing tests in [tests/agents/test_supervisor.py](tests/agents/test_supervisor.py)
    - Test rule-based routing (keywords trigger correct agent)
    - Test LLM fallback routing (ambiguous requests)
    - Test agent chaining (browser → general summarization)
    - Monitor performance: latency per agent, token usage, routing accuracy

**Verification**

**Manual Testing:**
- Start app: `./run.sh`
- Test browser delegation: "Search for Python tutorials"
- Verify browser agent invoked (check logs)
- Test multi-step: "Find the latest Python release notes and summarize them"
- Verify agent chaining (browser finds, general agent summarizes)

**Automated Tests:**
```bash
# Run all tests including new agent tests
uv run pytest tests/ -v

# Run only agent tests
uv run pytest tests/agents/ -v

# Run with coverage
uv run pytest tests/ --cov=includes/agents --cov-report=html
```

**Success Criteria:**
- ✅ Browser agent can execute agent-browser commands and return results
- ✅ Main agent successfully delegates browse tasks
- ✅ Supervisor routes requests to correct agent (>90% accuracy)
- ✅ All existing functionality preserved (profile tools, MCP, file uploads)
- ✅ Token usage reduced for non-browser conversations
- ✅ Tests pass with >80% coverage for new code
- ✅ Adding new agents is straightforward (copy template, implement tools)

**Decisions**

- **Routing Strategy:** Hybrid (keywords + LLM fallback) - fast for common cases, flexible for ambiguous ones
- **Execution Path:** Phased approach - prove browser agent works before full supervisor refactor
- **State Management:** Shared `SupervisorState` across all agents - simpler than isolated state
- **Tool Binding:** Per-agent tool lists - browser agent only sees browser tools, keeps context focused
- **File Organization:** `includes/agents/` for agent logic, `includes/tools/` for tool implementations - clear separation, easy navigation

---

## Proposed Folder Structure

```
/Users/tom/src/EagleAgent/
├── app.py                      # Main supervisor graph (orchestrator)
├── includes/
│   ├── agents/                 # 🆕 Sub-agent definitions
│   │   ├── __init__.py
│   │   ├── base.py            # 🆕 BaseSubAgent (shared patterns)
│   │   ├── browser_agent.py   # 🆕 Browser automation sub-agent
│   │   ├── general_agent.py   # 🆕 General conversation (current agent)
│   │   ├── supervisor.py      # 🆕 Routing logic
│   │   ├── code_agent.py      # Future: Code generation
│   │   └── data_agent.py      # Future: Data analysis
│   │
│   ├── tools/                  # 🆕 Tool modules (organized by domain)
│   │   ├── __init__.py
│   │   ├── user_profile.py    # Renamed from user_profile_tools.py
│   │   ├── browser_tools.py   # 🆕 Agent-browser integration
│   │   ├── file_tools.py      # Future: File operations
│   │   └── search_tools.py    # Future: Web/document search
│   │
│   ├── prompts.py             # Keep: Centralized prompts
│   ├── firestore_store.py     # Keep: Cross-thread memory
│   ├── timestamped_firestore_saver.py  # Keep: Checkpointer
│   ├── document_processing.py # Keep: File processing
│   ├── storage_utils.py       # Keep: GCS utilities
│   └── mcp_config.py          # Keep: MCP loader
│
├── config/
│   ├── settings.py            # Keep: Non-secret config
│   ├── prompts.yaml.example   # Keep: Future YAML prompts
│   ├── mcp_servers.yaml       # Keep: MCP servers
│   └── agents.yaml.example    # 🆕 Sub-agent routing rules
│
└── tests/
    ├── conftest.py            # Keep: Shared fixtures
    ├── agents/                # 🆕 Sub-agent tests
    │   ├── test_browser_agent.py
    │   ├── test_general_agent.py
    │   └── test_supervisor.py
    └── tools/                 # 🆕 Tool tests (reorganized)
        ├── test_user_profile.py
        └── test_browser_tools.py
```

## Architecture Diagrams

**Current Architecture (Simple Loop):**
```
START → model → [tools loop] → END
```

**Phase 1 Architecture (Browser as Tool):**
```
START → model (with use_browser_agent tool) → [tools | browser_agent subgraph] → END
```

**Phase 3 Architecture (Full Supervisor):**
```
                    ┌─────────────┐
                    │  Supervisor │
                    │  (routing)  │
                    └──────┬──────┘
                           │
         ┌─────────────────┼─────────────────┐
         ▼                 ▼                 ▼
    ┌─────────┐      ┌──────────┐     ┌──────────┐
    │ General │      │ Browser  │     │  Future  │
    │  Agent  │      │  Agent   │     │  Agents  │
    └────┬────┘      └────┬─────┘     └────┬─────┘
         │                │                 │
         └────────────────┼─────────────────┘
                          │
                          ▼
                        END
```

## Key Implementation Details

### BaseSubAgent Pattern
```python
# includes/agents/base.py
from typing import List, Optional, Dict, Any
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.store.base import BaseStore

class BaseSubAgent:
    """Base class for all sub-agents with common patterns."""
    
    def __init__(self, name: str, model: ChatGoogleGenerativeAI, store: BaseStore):
        self.name = name
        self.model = model
        self.store = store
    
    def get_tools(self, user_id: str) -> List[BaseTool]:
        """Override to provide agent-specific tools."""
        return []
    
    def get_system_prompt(self) -> str:
        """Override to provide agent-specific system prompt."""
        return f"You are {self.name}."
    
    async def __call__(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Standard agent execution pattern."""
        user_id = state.get("user_id")
        messages = state["messages"]
        
        # Get agent-specific tools
        tools = self.get_tools(user_id)
        
        # Bind tools to model
        if tools:
            model_with_tools = self.model.bind_tools(tools)
        else:
            model_with_tools = self.model
        
        # Build messages with agent-specific system prompt
        # (Similar to current call_model pattern)
        from langchain_core.messages import SystemMessage
        enhanced_messages = [SystemMessage(content=self.get_system_prompt())] + list(messages)
        
        # Invoke model
        response = await model_with_tools.ainvoke(enhanced_messages)
        
        return {"messages": [response]}
```

### Browser Agent Implementation
```python
# includes/agents/browser_agent.py
from .base import BaseSubAgent
from includes.tools.browser_tools import create_browser_tools

class BrowserAgent(BaseSubAgent):
    """Specialized agent for web browsing and automation."""
    
    def __init__(self, model, store):
        super().__init__("BrowserAgent", model, store)
        self.session_id = None  # Track browser session
    
    def get_tools(self, user_id: str):
        """Provide browser-specific tools."""
        return create_browser_tools()
    
    def get_system_prompt(self) -> str:
        """Browser-specific workflow instructions."""
        return """You are BrowserAgent, specialized in web browsing and automation.

Your workflow:
1. Open pages: browser("open <url>")
2. Get interactive elements: browser("snapshot -i --json")
   - Returns refs like @e1, @e2 for buttons, links, inputs
3. Interact: browser("click @e1") or browser("fill @e2 'text'")
4. Take screenshots: browser("screenshot --annotate")

The browser session persists between commands. Always re-snapshot after navigation.

Focus on completing the browsing task efficiently, then return results to the user."""
    
    async def cleanup(self):
        """Close browser session when done."""
        # Implement session cleanup
        pass
```

### Dynamic Tool Instructions
```python
# includes/prompts.py - Updated build_system_prompt
def build_system_prompt(
    profile_data: Optional[Dict[str, Any]] = None,
    available_tool_names: Optional[List[str]] = None  # NEW
) -> str:
    """Build system prompt with only relevant tool instructions."""
    parts = []
    
    # Agent identity
    agent_identity = get_agent_identity_prompt()
    if agent_identity:
        parts.append(agent_identity)
        parts.append("")
    
    # User profile
    if profile_data:
        parts.append(PROFILE_TEMPLATES["header"])
        profile_sections = build_profile_context(profile_data)
        parts.extend(profile_sections)
        parts.append("")
    
    # ONLY include instructions for available tools
    if available_tool_names:
        for tool_name in available_tool_names:
            if tool_name in TOOL_INSTRUCTIONS:
                parts.append(TOOL_INSTRUCTIONS[tool_name]["prompt_template"])
                parts.append("")
    
    return "\n".join(parts)
```
