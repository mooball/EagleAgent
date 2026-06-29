import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import HumanMessage, AIMessage
from includes.agents import Supervisor, RouteDecision

@pytest.fixture
def mock_model():
    model = MagicMock()
    # Chain .with_structured_output to return a mock that has .ainvoke
    structured_mock = MagicMock()
    model.with_structured_output.return_value = structured_mock
    structured_mock.ainvoke = AsyncMock()
    return model

@pytest.fixture
def supervisor(mock_model):
    return Supervisor(model=mock_model)


@pytest.mark.asyncio
async def test_supervisor_empty_messages(supervisor):
    state = {"messages": []}
    result = await supervisor(state)
    assert result == {"next_agent": "GeneralAgent"}


@pytest.mark.asyncio
async def test_supervisor_ai_message(supervisor, mock_model):
    """AI messages go through LLM router for agent-to-agent delegation."""
    state = {"messages": [AIMessage(content="Hello")]}
    mock_model.with_structured_output.return_value.ainvoke.return_value = RouteDecision(next_agent="FINISH")
    result = await supervisor(state)
    assert result["next_agent"] == "FINISH"


@pytest.mark.asyncio
async def test_supervisor_web_search(supervisor):
    """User asking for web research → ResearchAgent."""
    with patch("includes.intent_classifier.classify_intent", new_callable=AsyncMock, return_value="WEB_RESEARCH"):
        state = {"messages": [HumanMessage(content="Can you search Google for Python tutorials?")]}
        result = await supervisor(state)
        assert result["next_agent"] == "ResearchAgent"


@pytest.mark.asyncio
async def test_supervisor_general_chat(supervisor):
    """General chat → GeneralAgent."""
    with patch("includes.intent_classifier.classify_intent", new_callable=AsyncMock, return_value="GENERAL"):
        state = {"messages": [HumanMessage(content="What is my name?")]}
        result = await supervisor(state)
        assert result["next_agent"] == "GeneralAgent"


@pytest.mark.asyncio
async def test_supervisor_uncertain(supervisor):
    """Unclear intent → FINISH with clarifying message."""
    with patch("includes.intent_classifier.classify_intent", new_callable=AsyncMock, return_value="UNCERTAIN"):
        state = {"messages": [HumanMessage(content="Hmm let me think")]}
        result = await supervisor(state)
        assert result["next_agent"] == "FINISH"
        assert "messages" in result


@pytest.mark.asyncio
async def test_supervisor_db_query(supervisor):
    """Database query without RFQ context → ProcurementAgent with DB_QUERY intent."""
    with patch("includes.intent_classifier.classify_intent", new_callable=AsyncMock, return_value="DB_QUERY"):
        state = {"messages": [HumanMessage(content="Find me a water pump with part number 123")]}
        result = await supervisor(state)
        assert result["next_agent"] == "ProcurementAgent"
        assert result.get("intent") == "DB_QUERY"


@pytest.mark.asyncio
async def test_supervisor_db_query_purchase_history(supervisor):
    with patch("includes.intent_classifier.classify_intent", new_callable=AsyncMock, return_value="DB_QUERY"):
        state = {"messages": [HumanMessage(content="Do you have purchase history records?")]}
        result = await supervisor(state)
        assert result["next_agent"] == "ProcurementAgent"


@pytest.mark.asyncio
async def test_supervisor_db_query_purchase_orders(supervisor):
    with patch("includes.intent_classifier.classify_intent", new_callable=AsyncMock, return_value="DB_QUERY"):
        state = {"messages": [HumanMessage(content="How many purchase orders do we have?")]}
        result = await supervisor(state)
        assert result["next_agent"] == "ProcurementAgent"


@pytest.mark.asyncio
async def test_supervisor_intent_routes_to_procurement(supervisor):
    """When intent_context contains short procurement keywords, route directly to ProcurementAgent."""
    state = {
        "messages": [HumanMessage(content="who can supply hilti products")],
        "intent_context": "search_brands",
    }
    result = await supervisor(state)
    assert result["next_agent"] == "ProcurementAgent"


@pytest.mark.asyncio
async def test_supervisor_intent_preserved_for_agent(supervisor):
    """Intent context should be preserved in state so the sub-agent can use it."""
    state = {
        "messages": [HumanMessage(content="anything")],
        "intent_context": "search_products",
    }
    result = await supervisor(state)
    assert result["next_agent"] == "ProcurementAgent"


@pytest.mark.asyncio
async def test_supervisor_non_procurement_intent_falls_through(supervisor):
    """Intent context without procurement tool names → GENERAL."""
    with patch("includes.intent_classifier.classify_intent", new_callable=AsyncMock, return_value="GENERAL"):
        state = {
            "messages": [HumanMessage(content="hello")],
            "intent_context": "The user wants general help with something.",
        }
        result = await supervisor(state)
        assert result["next_agent"] == "GeneralAgent"


@pytest.mark.asyncio
async def test_supervisor_db_query_bearing_components(supervisor):
    """Non-RFQ product query → ProcurementAgent with DB_QUERY intent."""
    with patch("includes.intent_classifier.classify_intent", new_callable=AsyncMock, return_value="DB_QUERY"):
        state = {"messages": [HumanMessage(content="I need replacement bearing components for the warehouse")]}
        result = await supervisor(state)
        assert result["next_agent"] == "ProcurementAgent"
