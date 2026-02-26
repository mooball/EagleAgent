import chainlit as cl
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.graph import END, START, StateGraph
from typing import TypedDict, Sequence, Annotated
import operator

# Define the state
class AgentState(TypedDict):
    messages: Annotated[Sequence[HumanMessage | AIMessage], operator.add]

# Define a simple node function
def echo_node(state: AgentState):
    last_message = state["messages"][-1]
    return {"messages": [AIMessage(content=f"Echo: {last_message.content}")]}

# Build the graph
builder = StateGraph(AgentState)
builder.add_node("echo", echo_node)
builder.add_edge(START, "echo")
builder.add_edge("echo", END)
graph = builder.compile()

@cl.on_chat_start
async def start():
    await cl.Message(content="Hello! I am a simple Chainlit + LangGraph bot. Type something and I will echo it back using a LangGraph workflow.").send()

@cl.on_message
async def main(message: cl.Message):
    # Run the graph
    inputs = {"messages": [HumanMessage(content=message.content)]}
    result = await graph.ainvoke(inputs)
    
    # Get the last message from the result
    response = result["messages"][-1].content
    
    await cl.Message(content=response).send()
