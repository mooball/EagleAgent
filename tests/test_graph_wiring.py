import asyncio
import pathlib
import sys
import pytest

from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatResult, ChatGeneration
from includes.agents import RouteDecision


class StubChatModel(BaseChatModel):
    """Stub replacement for ChatGoogleGenerativeAI used in tests.

    Extends BaseChatModel so it's a proper LangChain Runnable, which is required
    by create_react_agent. Returns a deterministic AIMessage based on the latest
    human message, without making any network calls.
    """

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        last = messages[-1]
        content = getattr(last, "content", "")
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=f"stub-response: {content}"))]
        )

    @property
    def _llm_type(self):
        return "stub"

    def bind_tools(self, tools, **kwargs):
        """Support tool binding for compatibility."""
        return self

    def with_structured_output(self, schema):
        """Return a stub that produces a RouteDecision for supervisor routing.

        Routes user messages to GeneralAgent, agent responses to FINISH.
        """

        class _StructuredStub:
            async def ainvoke(self, messages, **kwargs):
                # Check last non-system message to decide routing
                from langchain_core.messages import HumanMessage as _HM
                last_non_system = [m for m in messages if not hasattr(m, "type") or m.type != "system"]
                if last_non_system and isinstance(last_non_system[-1], _HM):
                    return RouteDecision(next_agent="GeneralAgent")
                return RouteDecision(next_agent="FINISH")

        return _StructuredStub()


@pytest.mark.asyncio
async def test_langgraph_wiring_with_stub(monkeypatch):
    """Graph should run end-to-end using a stubbed model.

    This verifies that the LangGraph wiring and state handling works
    without touching the real Gemini API.
    """

    # Ensure the project root (where app.py lives) is on sys.path
    project_root = pathlib.Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # Patch the model factory so that *all* agents are built with the stub,
    # regardless of which modules have already imported ChatGoogleGenerativeAI.
    import includes.graph as _graph_module

    monkeypatch.setattr(
        _graph_module, "create_model", lambda agent_name: StubChatModel()
    )

    # Reset graph module state so setup_globals() rebuilds with the stub,
    # even if a previous test already initialized with the real model.
    monkeypatch.setattr(_graph_module, "globals_initialized", False)
    monkeypatch.setattr(_graph_module, "graph", None)
    monkeypatch.setattr(_graph_module, "research_graph", None)
    monkeypatch.setattr(_graph_module, "internal_graph", None)

    # Import after patching so that app.graph and app.model use the stub.
    import app  # noqa: WPS433  (import inside function is intentional for test)

    async def _run():
        await app.setup_globals()

        # Arrange: a simple user message and required LangGraph config
        config = {"configurable": {"thread_id": "test-thread"}}
        result = await app._graph().ainvoke(
            {"messages": [HumanMessage(content="hello")]}, config=config
        )

        # Assert: we got an AIMessage from the stub with expected content
        assert "messages" in result
        assert isinstance(result["messages"][-1], AIMessage)
        assert result["messages"][-1].content == "stub-response: hello"

    await _run()
