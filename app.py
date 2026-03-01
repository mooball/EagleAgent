import chainlit as cl
from chainlit.types import ThreadDict
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.store.base import BaseStore
from langgraph.prebuilt import ToolNode
from langchain_google_genai import ChatGoogleGenerativeAI
from typing import TypedDict, Sequence, Annotated, Dict, Optional, Any, Literal
import operator
import os
from dotenv import load_dotenv
from timestamped_firestore_saver import TimestampedFirestoreSaver
from firestore_store import FirestoreStore
from user_profile_tools import create_profile_tools

# Load environment variables
load_dotenv()

# Add cross-thread persistent store for user profiles and long-term memory
# Initialize this early so we can use it to create tools
store = FirestoreStore(project_id=os.getenv("GOOGLE_PROJECT_ID"), collection="user_memory")


# Initialize the model
# Using gemini-3-flash-preview as per user request (and verified existence)
# Provide your API key in the .env file
base_model = ChatGoogleGenerativeAI(model="gemini-3-flash-preview", google_api_key=os.getenv("GOOGLE_API_KEY"))

# Define the state
class AgentState(TypedDict):
    messages: Annotated[Sequence[HumanMessage | AIMessage | SystemMessage], operator.add]
    user_id: str  # User email for cross-thread memory lookup

# Define the node function that calls the model
async def call_model(
    state: AgentState
):
    """
    Call the LLM with user profile context from cross-thread memory.
    
    Args:
        state: Current conversation state
    """
    messages = state["messages"]
    user_id = state.get("user_id")
    
    # Create user-specific tools
    if user_id and store:
        tools = create_profile_tools(store, user_id)
        model_with_tools = base_model.bind_tools(tools)
    else:
        model_with_tools = base_model
    
    # Load user profile from store (cross-thread memory)
    user_profile = None
    if user_id and store:
        user_profile = await store.aget(("users",), user_id)
    
    # If user profile exists, add context to the conversation
    enhanced_messages = list(messages)
    if user_profile and user_profile.value:
        profile_data = user_profile.value
        
        # Build context string from profile
        profile_context = "User profile information:"
        if "preferred_name" in profile_data:
            profile_context += f"\n- Preferred name: {profile_data['preferred_name']} (use this to address the user)"
        elif "name" in profile_data:
            profile_context += f"\n- Name: {profile_data['name']}"
        if "preferences" in profile_data:
            prefs = profile_data['preferences']
            if isinstance(prefs, list):
                profile_context += f"\n- Preferences: {', '.join(prefs)}"
            else:
                profile_context += f"\n- Preferences: {prefs}"
        if "facts" in profile_data:
            facts = profile_data['facts']
            if isinstance(facts, list):
                profile_context += f"\n- Facts: {', '.join(facts)}"
            else:
                profile_context += f"\n- Facts: {facts}"
        
        # Add tool usage instructions
        profile_context += "\n\nWhen the user tells you information about themselves, use the remember_user_info tool to save it for future conversations. If they say 'call me X' or 'I prefer X', use the 'preferred_name' category."
        
        # Prepend system message with user context if not already present
        if not any(isinstance(m, SystemMessage) for m in enhanced_messages):
            enhanced_messages = [SystemMessage(content=profile_context)] + enhanced_messages
    else:
        # No profile yet - just add instruction to save info
        if user_id and not any(isinstance(m, SystemMessage) for m in enhanced_messages):
            enhanced_messages = [SystemMessage(content="When the user tells you information about themselves, use the remember_user_info tool to save it for future conversations. If they say 'call me X' or 'I prefer X', use the 'preferred_name' category.")] + enhanced_messages
    
    response = await model_with_tools.ainvoke(enhanced_messages)
    return {"messages": [response]}

# Tool execution node
def should_continue(state: AgentState) -> Literal["tools", END]:
    """Determine if we should continue to tools or end."""
    messages = state["messages"]
    last_message = messages[-1]
    
    # If the LLM makes a tool call, route to tools node
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    
    # Otherwise, end
    return END

async def call_tools(
    state: AgentState
):
    """Execute tool calls."""
    user_id = state.get("user_id")
    messages = state["messages"]
    last_message = messages[-1]
    
    # Create tools for this user
    tools = create_profile_tools(store, user_id) if user_id and store else []
    
    # Create a tool node and invoke it
    tool_node = ToolNode(tools)
    result = await tool_node.ainvoke(state)
    
    return result

# Build the graph
builder = StateGraph(AgentState)
builder.add_node("model", call_model)
builder.add_node("tools", call_tools)

# Add edges
builder.add_edge(START, "model")
builder.add_conditional_edges("model", should_continue, ["tools", END])
builder.add_edge("tools", "model")

# Add Firestore-based memory to persist state across interactions and restarts
# TimestampedFirestoreSaver adds `created_at` field to each checkpoint for TTL policies
checkpointer = TimestampedFirestoreSaver(project_id=os.getenv("GOOGLE_PROJECT_ID"), checkpoints_collection="checkpoints")

# Compile graph with both checkpointer (thread state) and store (cross-thread memory)
graph = builder.compile(checkpointer=checkpointer, store=store)

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
        
        # Store user's name info from Google OAuth in metadata
        # Google provides: name, given_name, family_name, email, picture
        if raw_user_data.get("name"):
            default_user.metadata["name"] = raw_user_data["name"]
        if raw_user_data.get("given_name"):
            default_user.metadata["given_name"] = raw_user_data["given_name"]
        
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
    
    # Get user's name for personalized greeting
    user_name = None
    
    if user:
        # Store user_id for cross-thread memory
        cl.user_session.set("user_id", user.identifier)
        
        # Priority order:
        # 1. Preferred name from user profile store
        # 2. Given name from Google OAuth
        # 3. Email as fallback
        
        # Try to get preferred_name from user profile store
        user_profile = await store.aget(("users",), user.identifier)
        if user_profile and user_profile.value and "preferred_name" in user_profile.value:
            user_name = user_profile.value["preferred_name"]
        
        # Fall back to given_name from Google OAuth
        if not user_name and user.metadata and "given_name" in user.metadata:
            user_name = user.metadata["given_name"]
        
        # Fall back to email if name not available
        if not user_name:
            user_name = user.identifier
    
    # Personalized welcome message
    if user_name:
        welcome_msg = f"Hello {user_name}! I am connected to Google Gemini. How can I help you today?"
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
    
    # Store user_id for cross-thread memory
    user = cl.user_session.get("user")
    if user:
        cl.user_session.set("user_id", user.identifier)
    
    # Log for debugging
    print(f"Resuming conversation with thread_id: {thread_id}")
    
    # Note: Chainlit automatically restores messages to the UI
    # LangGraph's checkpointer will automatically load the state when we use this thread_id
    
    # Get user's name for personalized greeting
    user_name = None
    
    if user:
        # Priority order:
        # 1. Preferred name from user profile store
        # 2. Given name from Google OAuth
        # 3. Email as fallback
        
        # Try to get preferred_name from user profile store
        user_profile = await store.aget(("users",), user.identifier)
        if user_profile and user_profile.value and "preferred_name" in user_profile.value:
            user_name = user_profile.value["preferred_name"]
        
        # Fall back to given_name from Google OAuth
        if not user_name and user.metadata and "given_name" in user.metadata:
            user_name = user.metadata["given_name"]
        
        # Fall back to email if name not available
        if not user_name:
            user_name = user.identifier
    
    # Optional: Send a welcome back message
    if user_name:
        await cl.Message(
            content=f"Welcome back, {user_name}! Continuing our previous conversation.",
            author="system"
        ).send()

@cl.on_message
async def main(message: cl.Message):
    # Use the session ID as the thread ID to maintain conversation history
    thread_id = cl.user_session.get("thread_id")
    user_id = cl.user_session.get("user_id", "")
    config = {"configurable": {"thread_id": thread_id}}
    
    # Run the graph with the new user message and user_id
    inputs = {
        "messages": [HumanMessage(content=message.content)],
        "user_id": user_id
    }
    
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
