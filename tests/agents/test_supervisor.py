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
async def test_supervisor_ai_message(supervisor):
    state = {"messages": [AIMessage(content="Hello")]}
    result = await supervisor(state)
    assert result == {"next_agent": "FINISH"}

@pytest.mark.asyncio
async def test_supervisor_rule_based_browser(supervisor, mock_model):
    state = {"messages": [HumanMessage(content="Can you search Google for Python tutorials?")]}
    mock_model.with_structured_output.return_value.ainvoke.return_value = RouteDecision(next_agent="BrowserAgent")
    result = await supervisor(state)
    assert result == {"next_agent": "BrowserAgent"}

@pytest.mark.asyncio
async def test_supervisor_llm_routing_general(supervisor, mock_model):
    state = {"messages": [HumanMessage(content="What is my name?")]}
    
    # Mock LLM decision
    mock_model.with_structured_output.return_value.ainvoke.return_value = RouteDecision(next_agent="GeneralAgent")
    
    result = await supervisor(state)
    
    assert result == {"next_agent": "GeneralAgent"}
    mock_model.with_structured_output.return_value.ainvoke.assert_called_once()

@pytest.mark.asyncio
async def test_supervisor_llm_routing_fallback(supervisor, mock_model):
    state = {"messages": [HumanMessage(content="Complex ambiguous query")]}
    
    # Force exception 
    mock_model.with_structured_output.return_value.ainvoke.side_effect = Exception("LLM Error")
    
    result = await supervisor(state)
    
    assert result == {"next_agent": "GeneralAgent"}

@pytest.mark.asyncio
async def test_supervisor_llm_routing_procurement_supplier(supervisor, mock_model):
    state = {"messages": [HumanMessage(content="Find me a water pump with part number 123")]}
    mock_model.with_structured_output.return_value.ainvoke.return_value = RouteDecision(next_agent="ProcurementAgent")
    result = await supervisor(state)
    assert result == {"next_agent": "ProcurementAgent"}
    mock_model.with_structured_output.return_value.ainvoke.assert_called_once()

@pytest.mark.asyncio
async def test_supervisor_llm_routing_purchase_history(supervisor, mock_model):
    state = {"messages": [HumanMessage(content="Do you have purchase history records?")]}
    mock_model.with_structured_output.return_value.ainvoke.return_value = RouteDecision(next_agent="ProcurementAgent")
    result = await supervisor(state)
    assert result == {"next_agent": "ProcurementAgent"}
    mock_model.with_structured_output.return_value.ainvoke.assert_called_once()

@pytest.mark.asyncio
async def test_supervisor_llm_routing_purchase_order(supervisor, mock_model):
    state = {"messages": [HumanMessage(content="How many purchase orders do we have?")]}
    mock_model.with_structured_output.return_value.ainvoke.return_value = RouteDecision(next_agent="ProcurementAgent")
    result = await supervisor(state)
    assert result == {"next_agent": "ProcurementAgent"}
    mock_model.with_structured_output.return_value.ainvoke.assert_called_once()

@pytest.mark.asyncio
async def test_supervisor_intent_routes_to_procurement(supervisor):
    """When intent_context contains procurement tool names, route directly to ProcurementAgent."""
    state = {
        "messages": [HumanMessage(content="who can supply hilti products")],
        "intent_context": "The user wants to find suppliers who carry a specific brand. First use search_brands to verify the brand name, then use search_suppliers with the brand parameter.",
    }
    result = await supervisor(state)
    assert result["next_agent"] == "ProcurementAgent"
    assert result["intent_context"] is None

@pytest.mark.asyncio
async def test_supervisor_intent_cleared_after_use(supervisor):
    """Intent context should be set to None after routing so it doesn't re-trigger."""
    state = {
        "messages": [HumanMessage(content="anything")],
        "intent_context": "Use search_products to find the product.",
    }
    result = await supervisor(state)
    assert result["intent_context"] is None

@pytest.mark.asyncio
async def test_supervisor_non_procurement_intent_falls_through(supervisor, mock_model):
    """Intent context without procurement tool names should not trigger intent routing."""
    state = {
        "messages": [HumanMessage(content="hello")],
        "intent_context": "The user wants general help with something.",
    }
    mock_model.with_structured_output.return_value.ainvoke.return_value = RouteDecision(next_agent="GeneralAgent")
    result = await supervisor(state)
    assert result == {"next_agent": "GeneralAgent"}

@pytest.mark.asyncio
async def test_supervisor_llm_routing_procurement(supervisor, mock_model):
    # Use a string without keywords to trigger LLM routing into procurement
    state = {"messages": [HumanMessage(content="I need replacement bearing components for the warehouse")]}
    
    # Mock LLM decision
    mock_model.with_structured_output.return_value.ainvoke.return_value = RouteDecision(next_agent="ProcurementAgent")
    
    result = await supervisor(state)
    
    assert result == {"next_agent": "ProcurementAgent"}
    mock_model.with_structured_output.return_value.ainvoke.assert_called_once()
