import chainlit as cl
from chainlit.types import ThreadDict
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langchain_google_genai import ChatGoogleGenerativeAI
from typing import TypedDict, Sequence, Annotated, Dict, Optional
import operator
import os
from dotenv import load_dotenv
from timestamped_firestore_saver import TimestampedFirestoreSaver

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

# Add Firestore-based memory to persist state across interactions and restarts
# TimestampedFirestoreSaver adds `created_at` field to each checkpoint for TTL policies
checkpointer = TimestampedFirestoreSaver(project_id="mooballai", checkpoints_collection="checkpoints")
graph = builder.compile(checkpointer=checkpointer)

@cl.oauth_callback
def oauth_callback(
    provider_id: str,
    token: str,
    raw_user_data: Dict[str, str],
    default_user: cl.User,
) -> Optional[cl.User]:
    """
    OAuth callback to authenticate users via Google.
    
    Args:
        provider_id: The OAuth provider (e.g., "google")
        token: The OAuth token
        raw_user_data: User data from the OAuth provider
        default_user: Default user object created by Chainlit
    
    Returns:
        cl.User if authentication successful, None otherwise
    """
    if provider_id == "google":
        # Optional: restrict to specific domain
        # if raw_user_data.get("hd") == "yourdomain.com":
        #     return default_user
        # return None
        
        # Allow all Google users
        return default_user
    
    return None

@cl.data_layer
def get_data_layer():
    """
    Configure SQLite-based data layer for conversation history persistence.
    This enables the chat history sidebar in the Chainlit UI.
    """
    return SQLAlchemyDataLayer(conninfo=os.getenv("DATABASE_URL"))

@cl.on_chat_start
async def start():
    import uuid
    
    # Get authenticated user
    user = cl.user_session.get("user")
    
    # Create thread_id (will be managed by Chainlit's data layer once set up)
    thread_id = str(uuid.uuid4())
    cl.user_session.set("thread_id", thread_id)
    
    # Personalized welcome message
    if user:
        welcome_msg = f"Hello {user.identifier}! I am connected to Google Gemini. How can I help you today?"
    else:
        welcome_msg = "Hello! I am connected to Google Gemini. How can I help you today?"
    
    await cl.Message(content=welcome_msg).send()

@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict):
    """
    Called when a user resumes a previous conversation.
    Restores the thread_id so LangGraph can load the conversation state from Firestore.
    
    Args:
        thread: The persisted conversation thread containing id, steps, and metadata
    """
    # Extract the thread_id from the persisted conversation
    thread_id = thread["id"]
    
    # Store it in the user session
    cl.user_session.set("thread_id", thread_id)
    
    # Log for debugging
    print(f"Resuming conversation with thread_id: {thread_id}")
    
    # Note: Chainlit automatically restores messages to the UI
    # LangGraph's checkpointer will automatically load the state when we use this thread_id
    
    # Optional: Send a welcome back message
    user = cl.user_session.get("user")
    if user:
        await cl.Message(
            content=f"Welcome back, {user.identifier}! Continuing our previous conversation.",
            author="system"
        ).send()

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
