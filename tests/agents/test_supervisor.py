import pytest
from unittest.mock import AsyncMock, MagicMock
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
    """AI messages now go through LLM router for agent-to-agent delegation."""
    state = {"messages": [AIMessage(content="Hello")]}
    mock_model.with_structured_output.return_value.ainvoke.return_value = RouteDecision(next_agent="FINISH")
    result = await supervisor(state)
    assert result["next_agent"] == "FINISH"

@pytest.mark.asyncio
async def test_supervisor_rule_based_general_web(supervisor, mock_model):
    state = {"messages": [HumanMessage(content="Can you search Google for Python tutorials?")]}
    mock_model.with_structured_output.return_value.ainvoke.return_value = RouteDecision(next_agent="GeneralAgent")
    result = await supervisor(state)
    assert result["next_agent"] == "GeneralAgent"

@pytest.mark.asyncio
async def test_supervisor_llm_routing_general(supervisor, mock_model):
    state = {"messages": [HumanMessage(content="What is my name?")]}
    mock_model.with_structured_output.return_value.ainvoke.return_value = RouteDecision(next_agent="GeneralAgent")
    result = await supervisor(state)
    assert result["next_agent"] == "GeneralAgent"

@pytest.mark.asyncio
async def test_supervisor_llm_routing_fallback(supervisor, mock_model):
    state = {"messages": [HumanMessage(content="Complex ambiguous query")]}
    mock_model.with_structured_output.return_value.ainvoke.side_effect = Exception("LLM Error")
    result = await supervisor(state)
    # Fallback is now FINISH instead of GeneralAgent
    assert result["next_agent"] == "FINISH"

@pytest.mark.asyncio
async def test_supervisor_llm_routing_procurement_supplier(supervisor, mock_model):
    state = {"messages": [HumanMessage(content="Find me a water pump with part number 123")]}
    mock_model.with_structured_output.return_value.ainvoke.return_value = RouteDecision(next_agent="ProcurementAgent")
    result = await supervisor(state)
    assert result["next_agent"] == "ProcurementAgent"

@pytest.mark.asyncio
async def test_supervisor_llm_routing_purchase_history(supervisor, mock_model):
    state = {"messages": [HumanMessage(content="Do you have purchase history records?")]}
    mock_model.with_structured_output.return_value.ainvoke.return_value = RouteDecision(next_agent="ProcurementAgent")
    result = await supervisor(state)
    assert result["next_agent"] == "ProcurementAgent"

@pytest.mark.asyncio
async def test_supervisor_llm_routing_purchase_order(supervisor, mock_model):
    state = {"messages": [HumanMessage(content="How many purchase orders do we have?")]}
    mock_model.with_structured_output.return_value.ainvoke.return_value = RouteDecision(next_agent="ProcurementAgent")
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
async def test_supervisor_non_procurement_intent_falls_through(supervisor, mock_model):
    """Intent context without procurement tool names should not trigger intent routing."""
    state = {
        "messages": [HumanMessage(content="hello")],
        "intent_context": "The user wants general help with something.",
    }
    mock_model.with_structured_output.return_value.ainvoke.return_value = RouteDecision(next_agent="GeneralAgent")
    result = await supervisor(state)
    assert result["next_agent"] == "GeneralAgent"

@pytest.mark.asyncio
async def test_supervisor_llm_routing_procurement(supervisor, mock_model):
    # Use a string without keywords to trigger LLM routing into procurement
    state = {"messages": [HumanMessage(content="I need replacement bearing components for the warehouse")]}
    
    # Mock LLM decision
    mock_model.with_structured_output.return_value.ainvoke.return_value = RouteDecision(next_agent="ProcurementAgent")
    result = await supervisor(state)
    assert result["next_agent"] == "ProcurementAgent"
