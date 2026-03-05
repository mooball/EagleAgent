# EagleAgent Multi-Agent Architecture

This directory contains the implementations for the multi-agent architecture within EagleAgent. The design follows a sub-agent pattern, effectively nesting specialized agents within a larger workflow or routing them via a supervisor.

## Architecture Pattern

Currently, we are moving towards a **Supervisor Pattern** with isolated sub-agents. 
- **Base Agent Abstraction**: All agents inherit from `BaseSubAgent` which provides common mechanisms for standard state execution, injecting context (`RunnableConfig`), and handling tools dynamically.
- **Specialized Tool Binding**: sub-agents only receive the tools relevant to their domain (e.g., `BrowserAgent` exclusively sees browser automation tools). This reduces token usage, context bloat, and LLM hallucination.

### Available Agents

#### 1. `BrowserAgent` (`browser_agent.py`)
A specialized AI agent that natively runs `agent-browser` (a Playwright-backed web automation tool).
- Can open URLs, extract accessibility trees, click buttons, type, and take screenshots.
- Handled asynchronously via `asyncio.create_subprocess_shell` passing `cl.context`.
- Falls back gracefully via text mapping when Chainsit UX limits out nested graphing context.

## Creating a New Sub-Agent

Creating a new agent is straightforward. You must follow the base abstraction to ensure it ties seamlessly into the main graph (and future Supervisor Nodes):

1. **Create the Agent Class**:
```python
# includes/agents/my_new_agent.py
from .base import BaseSubAgent
from includes.tools.my_new_tools import get_my_new_tools

class MyNewAgent(BaseSubAgent):
    def __init__(self, model, store):
        super().__init__("MyNewAgent", model, store)
        
    def get_tools(self, user_id):
        # Only inject relevant tools
        return get_my_new_tools(user_id)
        
    def get_system_prompt(self):
        return "You are a specialized agent for X..."
```

2. **Register the Specific Prompts**:
Add dynamic tool usage and agent instructions in `includes/prompts.py`. 

3. **Integrate into Graph/Supervisor**:
For Phase 1 (Tool-based invocation), you can wrap the sub-graph inside a `@tool` like `use_my_new_agent(prompt)`. For Phase 3 (Supervisor Pattern), you'll add the new class to the conditional edges graph routing mechanisms.
