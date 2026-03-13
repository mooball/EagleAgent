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
async def test_supervisor_rule_based_browser(supervisor):
    state = {"messages": [HumanMessage(content="Can you search Google for Python tutorials?")]}
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
async def test_supervisor_rule_based_procurement(supervisor):
    state = {"messages": [HumanMessage(content="Find me a water pump with part number 123")]}
    result = await supervisor(state)
    assert result == {"next_agent": "ProcurementAgent"}

@pytest.mark.asyncio
async def test_supervisor_llm_routing_procurement(supervisor, mock_model):
    # Use a string without keywords to trigger LLM routing into procurement
    state = {"messages": [HumanMessage(content="I need replacement bearing components for the warehouse")]}
    
    # Mock LLM decision
    mock_model.with_structured_output.return_value.ainvoke.return_value = RouteDecision(next_agent="ProcurementAgent")
    
    result = await supervisor(state)
    
    assert result == {"next_agent": "ProcurementAgent"}
    mock_model.with_structured_output.return_value.ainvoke.assert_called_once()
