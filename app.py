import chainlit as cl
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver
from langchain_google_genai import ChatGoogleGenerativeAI
from typing import TypedDict, Sequence, Annotated
import operator
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize the model
# Using gemini-3-flash-preview as per user request (and verified existence)
# Provide your API key in the .env file
model = ChatGoogleGenerativeAI(model="gemini-3-flash-preview", google_api_key=os.getenv("GOOGLE_API_KEY"))

# Define the state
class AgentState(TypedDict):
    messages: Annotated[Sequence[HumanMessage | AIMessage | SystemMessage], operator.add]

# Define the node function that calls the model
async def call_model(state: AgentState):
    messages = state["messages"]
    response = await model.ainvoke(messages)
    return {"messages": [response]}

# Build the graph
builder = StateGraph(AgentState)
builder.add_node("model", call_model)
builder.add_edge(START, "model")
builder.add_edge("model", END)

# Add memory to maintain state across interactions
memory = MemorySaver()
graph = builder.compile(checkpointer=memory)

@cl.on_chat_start
async def start():
    # Generate a session ID for the thread
    import uuid
    thread_id = str(uuid.uuid4())
    cl.user_session.set("thread_id", thread_id)
    await cl.Message(content="Hello! I am connected to Google Gemini. How can I help you today?").send()

@cl.on_message
async def main(message: cl.Message):
    # Use the session ID as the thread ID to maintain conversation history
    thread_id = cl.user_session.get("thread_id")
    config = {"configurable": {"thread_id": thread_id}}
    
    # Run the graph with the new user message
    inputs = {"messages": [HumanMessage(content=message.content)]}
    
    # Invoke the graph and stream the response
    msg = cl.Message(content="")
    await msg.send()
    
    async for event in graph.astream_events(inputs, config=config, version="v1"):
        kind = event["event"]
        if kind == "on_chat_model_stream":
            content = event["data"]["chunk"].content
            if content:
                # Handle list of content parts (e.g. from Gemini experimental models)
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            chunk_text = part.get("text", "")
                            if chunk_text:
                                await msg.stream_token(chunk_text)
                        elif isinstance(part, str):
                            await msg.stream_token(part)
                # Handle single string content
                elif isinstance(content, str):
                    await msg.stream_token(content)
                
    await msg.update()
