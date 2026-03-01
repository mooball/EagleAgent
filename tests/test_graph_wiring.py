import asyncio
import pathlib
import sys

from langchain_core.messages import HumanMessage, AIMessage


class StubChatModel:
    """Stub replacement for ChatGoogleGenerativeAI used in tests.

    It pretends to be the LLM and returns a deterministic AIMessage
    based on the latest human message, without making any network calls.
    """

    def __init__(self, *args, **kwargs) -> None:  # signature-compatible
        pass

    async def ainvoke(self, messages):
        # Find the last human message content (if any)
        last = messages[-1]
        content = getattr(last, "content", "")
        return AIMessage(content=f"stub-response: {content}")
    
    def bind_tools(self, tools):
        """Support tool binding for compatibility."""
        return self


def test_langgraph_wiring_with_stub(monkeypatch):
    """Graph should run end-to-end using a stubbed model.

    This verifies that the LangGraph wiring and state handling works
    without touching the real Gemini API.
    """

    # Ensure the project root (where app.py lives) is on sys.path
    project_root = pathlib.Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # Patch the ChatGoogleGenerativeAI symbol *before* importing app,
    # so app.model is constructed using the stub instead of the real class.
    import langchain_google_genai

    monkeypatch.setattr(
        langchain_google_genai, "ChatGoogleGenerativeAI", StubChatModel, raising=True
    )

    # Import after patching so that app.graph and app.model use the stub.
    import app  # noqa: WPS433  (import inside function is intentional for test)

    async def _run():
        # Arrange: a simple user message and required LangGraph config
        config = {"configurable": {"thread_id": "test-thread"}}
        result = await app.graph.ainvoke(
            {"messages": [HumanMessage(content="hello")]}, config=config
        )

        # Assert: we got an AIMessage from the stub with expected content
        assert "messages" in result
        assert isinstance(result["messages"][-1], AIMessage)
        assert result["messages"][-1].content == "stub-response: hello"

    asyncio.run(_run())
