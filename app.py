import chainlit as cl
import uuid
from chainlit.types import ThreadDict
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage, trim_messages, RemoveMessage, BaseMessage
from langchain_core.runnables.config import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.store.base import BaseStore
from langgraph.prebuilt import ToolNode
from langchain_google_genai import ChatGoogleGenerativeAI
from typing import TypedDict, Sequence, Annotated, Dict, Optional, Any, Literal, NotRequired
import operator
import os
import logging
from dotenv import load_dotenv
from config import config
from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres import AsyncPostgresStore
from includes.tools.user_profile import create_profile_tools
from includes.prompts import build_system_prompt
from includes.commands import handle_deleteall_command
from includes.storage_utils import (
    upload_file_locally,
    generate_object_key,
    generate_signed_url
)
from includes.document_processing import process_file, create_multimodal_content
from includes.local_storage_client import LocalStorageClient
from includes.mcp_config import load_mcp_config
from includes.agents.browser_agent import BrowserAgent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.tools import tool

# Set up Chainlit static file serving for local file attachments
import chainlit.server as cl_server
from fastapi.staticfiles import StaticFiles

# Create the data directory if it doesn't exist
os.makedirs(os.path.join(config.DATA_DIR, "attachments"), exist_ok=True)
# Mount the local data directory to the /files route so Chainlit UI can load images
# Notice we mount DATA_DIR/attachments explicitly because Chainlit's data layer saves files inside an "attachments" subfolder
cl_server.app.mount("/files", StaticFiles(directory=os.path.join(config.DATA_DIR, "attachments")), name="files")

# FIX: Chainlit has a catch-all route `/{full_path:path}` that intercepts `/files` if our mount is at the end.
# We must move our newly added mount BEFORE the catch-all route.
routes = cl_server.app.router.routes
# The mount we just added is at the very end of the list
files_mount = routes.pop()
# Find the catch-all index
catch_all_idx = next((i for i, r in enumerate(routes) if getattr(r, 'path', '') == '/{full_path:path}'), len(routes))
# Insert our mount just before the catch-all
routes.insert(catch_all_idx, files_mount)

# Load environment variables (still needed for secrets like GOOGLE_API_KEY)
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)

# Initialize PostgreSQL connection pool
# Using the CHECKPOINT_DATABASE_URL which defaults to psycopg style dsns
pg_pool = AsyncConnectionPool(
    config.CHECKPOINT_DATABASE_URL,
    min_size=1,
    max_size=10,
    kwargs={"autocommit": True},
    open=False, # We will open this explicitly in an async context or lazily
)

# Add cross-thread persistent store for user profiles and long-term memory
# Initialize this early so we can use it to create tools
store = None  # Will be initialized in start()

# Initialize MCP client for external tool integration
# Loads MCP server configurations from config/mcp_servers.yaml
mcp_client = None


# Initialize the model
# Model configuration is in config/settings.py (DEFAULT_MODEL)
# API key is loaded from environment variable (secret)
base_model = ChatGoogleGenerativeAI(model=config.DEFAULT_MODEL, google_api_key=os.getenv("GOOGLE_API_KEY"))

# Initialize browser agent for web automation
browser_agent = None

# Define the state
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    user_id: str  # User email for cross-thread memory lookup
    file_attachments: NotRequired[list[Dict[str, Any]]]  # Optional: uploaded file metadata

def make_use_browser_agent_tool(user_id: str | None):
    """Factory to create a browser agent delegation tool with the right user context."""
    @tool
    async def use_browser_agent(task: str, config: RunnableConfig) -> str:
        """
        Delegate web browsing and automation tasks to specialized browser agent.
        
        Use this tool when you need to:
        - Search for information online
        - Navigate to and extract data from websites  
        - Interact with web pages (click, fill forms, etc.)
        - Get current/real-time information from the web
        
        The browser agent has access to a headless Chromium browser and can:
        - Open web pages
        - Click buttons and links
        - Fill out forms
        - Extract text and data
        - Take screenshots
        
        CRITICAL RULE FOR SCREENSHOTS:
        When the user asks for a screenshot, instruct the browser agent to capture it. 
        The system will AUTOMATICALLY display the actual image in the user's interface out-of-band.
        You MUST NOT attempt to output markdown image links, JSON placeholders, or hallucinated URLs for the screenshot. 
        Simply tell the user "The screenshot has been captured and displayed."
        
        Args:
            task: Clear description of the browsing task to perform.
                  Examples:
                  - "Search Google for Python 3.12 release date"
                  - "Go to python.org and find the latest version"
                  - "Search for weather in Sydney and extract the forecast"
        
        Returns:
            Results from the browser agent as a string
        """
        try:
            # Create a sub-state for the browser agent
            browser_state = {
                "messages": [HumanMessage(content=task)],
                "user_id": user_id
            }
            
            # Invoke the browser agent
            logging.info(f"Delegating to browser agent: {task[:100]}...")
            result = await browser_agent(browser_state, config)
            
            # Extract the response message
            response_message = result["messages"][-1]
            response_text = response_message.content if hasattr(response_message, "content") else str(response_message)
            
            logging.info(f"Browser agent completed: {len(response_text)} chars")
            return response_text
            
        except Exception as e:
            logging.error(f"Browser agent error: {e}")
            return f"Browser agent error: {str(e)}"
            
    return use_browser_agent

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
    
    # Create user-specific tools and add MCP tools
    tools = []
    if user_id and store:
        tools.extend(create_profile_tools(store, user_id))
    
    # Add browser agent delegation tool
    tools.append(make_use_browser_agent_tool(user_id))
    
    # Add MCP tools if available
    if mcp_client:
        try:
            mcp_tools = await mcp_client.get_tools()
            tools.extend(mcp_tools)
            logging.debug(f"Added {len(mcp_tools)} MCP tools")
        except Exception as e:
            logging.error(f"Failed to get MCP tools: {e}")
    
    if tools:
        model_with_tools = base_model.bind_tools(tools)
    else:
        model_with_tools = base_model
    
    # Load user profile from store (cross-thread memory)
    user_profile = None
    if user_id and store:
        user_profile = await store.aget(("users",), user_id)
    
    # Build system prompt using centralized configuration from includes/prompts.py
    # Include only relevant tool instructions based on available tools
    enhanced_messages = list(messages)
    if not any(isinstance(m, SystemMessage) for m in enhanced_messages):
        # Construct system prompt with user profile (if available) and available tool names
        profile_data = user_profile.value if (user_profile and user_profile.value) else None
        tool_names = [tool.name for tool in tools] if tools else None
        system_content = build_system_prompt(profile_data, available_tool_names=tool_names)
        enhanced_messages = [SystemMessage(content=system_content)] + enhanced_messages
    
    # Trim history to ~30 messages to prevent hitting PostgreSQL 1MiB checkpoint limit
    trimmed_messages = trim_messages(
        enhanced_messages,
        max_tokens=30, # Max number of messages to retain
        strategy="last",
        token_counter=len, # Count each message as 1
        include_system=True,
        allow_partial=False
    )
    
    response = await model_with_tools.ainvoke(trimmed_messages)
    
    # Explicitly remove pruned messages from state so they don't bloat the checkpointer
    retained_ids = {m.id for m in trimmed_messages if getattr(m, "id", None)}
    
    # Exclude system message when figuring out what to remove, as we dynamically add it and it's not in State
    messages_to_remove = [
        RemoveMessage(id=m.id) 
        for m in messages 
        if getattr(m, "id", None) and m.id not in retained_ids
    ]
    
    return {"messages": messages_to_remove + [response]}

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
    tools = []
    if user_id and store:
        tools.extend(create_profile_tools(store, user_id))
    
    # Add browser agent delegation tool
    tools.append(make_use_browser_agent_tool(user_id))
    
    # Add MCP tools if available
    if mcp_client:
        try:
            mcp_tools = await mcp_client.get_tools()
            tools.extend(mcp_tools)
        except Exception as e:
            logging.error(f"Failed to get MCP tools during execution: {e}")
    
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

# Add PostgreSQL-based memory to persist state across interactions and restarts
checkpointer = None

# Compile graph with both checkpointer (thread state) and store (cross-thread memory)
graph = None

globals_initialized = False

async def setup_globals():
    """Initialize async-dependent global variables."""
    global store, mcp_client, browser_agent, checkpointer, graph, globals_initialized
    
    if globals_initialized:
        return
        
    # Open pg_pool
    try:
        await pg_pool.open()
    except Exception:
        pass
        
    # Set up store
    store = AsyncPostgresStore(pg_pool)
    await store.setup()
    
    # Set up checkpointer
    checkpointer = AsyncPostgresSaver(pg_pool)
    await checkpointer.setup()
    
    # Set up MCP
    try:
        mcp_config = load_mcp_config("config/mcp_servers.yaml")
        if mcp_config:
            mcp_client = MultiServerMCPClient(mcp_config)
            logging.info(f"MCP client initialized with {len(mcp_config)} server(s)")
        else:
            logging.info("No MCP servers configured")
    except Exception as e:
        logging.warning(f"Failed to initialize MCP client: {e}. Agent will work without MCP tools.")
        mcp_client = None
        
    # Initialize browser agent
    browser_agent = BrowserAgent(model=base_model, store=store)
    
    # Compile graph
    graph = builder.compile(checkpointer=checkpointer, store=store)
    
    globals_initialized = True

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
        # Check if user's domain is in the allowed domains list
        allowed_domains_str = config.OAUTH_ALLOWED_DOMAINS
        if allowed_domains_str:
            allowed_domains = [domain.strip() for domain in allowed_domains_str.split(",")]
            user_domain = raw_user_data.get("hd")
            
            # Reject if no domain (personal Gmail) or domain not in allowed list
            if not user_domain or user_domain not in allowed_domains:
                print(f"Authentication rejected: domain '{user_domain}' not in allowed list: {allowed_domains}")
                return None
        
        # Store all available user data from Google OAuth in metadata
        # Google provides: name, given_name, family_name, email, picture, locale, hd
        if raw_user_data.get("name"):
            default_user.metadata["name"] = raw_user_data["name"]
        if raw_user_data.get("given_name"):
            default_user.metadata["given_name"] = raw_user_data["given_name"]
        if raw_user_data.get("family_name"):
            default_user.metadata["family_name"] = raw_user_data["family_name"]
        if raw_user_data.get("email"):
            default_user.metadata["email"] = raw_user_data["email"]
        if raw_user_data.get("picture"):
            default_user.metadata["picture"] = raw_user_data["picture"]
        if raw_user_data.get("locale"):
            default_user.metadata["locale"] = raw_user_data["locale"]
        if raw_user_data.get("hd"):
            default_user.metadata["hd"] = raw_user_data["hd"]
        
        # Authentication successful
        return default_user
    
    return None

@cl.data_layer
def get_data_layer():
    """
    Configure PostgreSQL-based data layer for conversation history persistence.
    This enables the chat history sidebar in the Chainlit UI.
    Includes local storage client for persistent file attachments.
    """
    # Initialize Local storage client for file attachments
    import os
    attachments_dir = os.path.join(config.DATA_DIR, "attachments")
    storage_client = LocalStorageClient(base_dir=attachments_dir)
    
    return SQLAlchemyDataLayer(
        conninfo=config.DATABASE_URL,
        storage_provider=storage_client,
        show_logger=True,
    )

@cl.on_chat_start
async def start():
    import uuid
    
    # Initialize the pg pool and database schemas if not already done securely
    # AsyncConnectionPool open can be safely called multiple times if we just open it.
    await setup_globals()
    
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
    Restores the thread_id so LangGraph can load the conversation state from PostgreSQL.
    
    Args:
        thread: The persisted conversation thread containing id, steps, and metadata
    """
    # Ensure our async dependencies are initialized
    await setup_globals()
    
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
    
    # === COMMAND HANDLING ===
    content = message.content.strip()
    
    # Check if we're waiting for /deleteall confirmation
    if cl.user_session.get("awaiting_delete_confirmation"):
        cl.user_session.set("awaiting_delete_confirmation", False)
        if content.lower() in ["y", "yes"]:
            if user_id:
                await handle_deleteall_command(user_id, store, pg_pool)

            # Reset current thread to start fresh in LangGraph
            new_thread = str(uuid.uuid4())
            cl.user_session.set("thread_id", new_thread)
            await cl.Message(content="🗑️ All stored knowledge, files, and conversation history about you has been completely erased from all databases.\n\n*Note: Please refresh your browser window now to clear this chat log.*", author="system").send()
        else:
            await cl.Message(content="Deletion cancelled. Resuming normal conversation.", author="system").send()
        return

    # Check for slash commands
    if content.startswith("/"):
        command = content.split()[0].lower()
        if command == "/new":
            new_thread = str(uuid.uuid4())
            cl.user_session.set("thread_id", new_thread)
            await cl.Message(content="🔄 Started a new conversation thread.", author="system").send()
            return
        elif command == "/deleteall":
            cl.user_session.set("awaiting_delete_confirmation", True)
            await cl.Message(content="⚠️ **Warning:** This will permanently delete all preferences, settings, and memories associated with your profile, and start a new blank conversation.\n\n**Do you really want me to delete all your data?** (Reply with **Yes** or **No**)", author="system").send()
            return
        else:
            await cl.Message(content=f"Unknown command: {command}", author="system").send()
            return
    # === END COMMAND HANDLING ===

    graph_config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": config.GRAPH_RECURSION_LIMIT
    }
    
    # Process file attachments if present
    # Re-attach elements to response message to trigger persistence
    processed_files = []
    file_metadata = []
    uploaded_elements = []  # Track elements for re-attachment
    
    if message.elements:
        logging.info(f"Received {len(message.elements)} file attachments")
        for element in message.elements:
            # Log element details for debugging
            logging.info(f"Element: id={element.id}, name={element.name}, for_id={element.for_id}, thread_id={element.thread_id}")
            try:
                # Process file content for LLM
                with open(element.path, "rb") as f:
                    file_bytes = f.read()
                
                processed_file = process_file(file_bytes, element.mime, element.name)
                processed_files.append(processed_file)
                
                # Keep track of elements for persistence
                uploaded_elements.append(element)
                
                # Store metadata
                file_metadata.append({
                    "name": element.name,
                    "mime_type": element.mime,
                    "size": element.size,
                    "processed_type": processed_file.get("processed_type")
                })
                
                logging.info(f"Processed file: {element.name} ({processed_file.get('processed_type')})")
                
            except Exception as e:
                logging.error(f"Error processing file {element.name}: {e}")
                await cl.Message(
                    content=f"⚠️ Error processing {element.name}: {str(e)}",
                    author="system"
                ).send()
        
        # Re-attach elements to a confirmation message to trigger persistence
        if uploaded_elements:
            await cl.Message(
                content=f"📎 Received {len(uploaded_elements)} file(s)",
                elements=uploaded_elements
            ).send()
    
    # Create multimodal message content (text + files)
    message_content = create_multimodal_content(message.content, processed_files)
    
    # Run the graph with the new user message and user_id
    inputs = {
        "messages": [HumanMessage(content=message_content)],
        "user_id": user_id
    }
    
    if file_metadata:
        inputs["file_attachments"] = file_metadata
    
    # Invoke the graph and stream the response
    msg = cl.Message(content="")
    await msg.send()
    
    async for event in graph.astream_events(inputs, config=graph_config, version="v1"):
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
        elif kind == "on_tool_end":
            data = event.get("data", {})
            output = data.get("output")
            
            # Extract string content from tool output robustly
            output_str = ""
            if isinstance(output, str):
                output_str = output
            elif hasattr(output, "content"):
                output_str = str(output.content)
            elif hasattr(output, "get") and "output" in output:
                output_str = str(output["output"])
            else:
                output_str = str(output)
            
            if "Screenshot saved to" in output_str:
                # Intercept logic moved back to the tool itself for context stability
                pass

        elif kind == "on_chat_model_end":
            output = event.get("data", {}).get("output")
            
            # Extract usage_metadata depending on whether output is a dict or an object
            usage = None
            if hasattr(output, "usage_metadata") and output.usage_metadata:
                usage = output.usage_metadata
            elif isinstance(output, dict):
                if "usage_metadata" in output:
                    usage = output["usage_metadata"]
                elif "generations" in output and output["generations"] and len(output["generations"]) > 0 and len(output["generations"][0]) > 0:
                    gen = output["generations"][0][0]
                    if isinstance(gen, dict) and "message" in gen:
                        msg_obj = gen["message"]
                        if hasattr(msg_obj, "usage_metadata") and msg_obj.usage_metadata:
                            usage = msg_obj.usage_metadata
            if not usage and hasattr(output, "response_metadata") and output.response_metadata:
                usage = output.response_metadata.get("usage_metadata") or output.response_metadata.get("token_usage")

            if usage:
                prompt_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
                completion_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
                total_tokens = usage.get("total_tokens", 0)
                
                # HTML enabled in chainlit config so we can inject exact precision styles!
                token_info = f"\n\n<div style='margin-top:20px; border-top:1px solid #444; padding-top:5px; font-size:0.8em; color:#a1a1aa; font-style:italic;'>Tokens: {total_tokens:,} (Context: {prompt_tokens:,}, Generated: {completion_tokens:,})</div>"
                await msg.stream_token(token_info)
                
                # Track cumulative tokens in session
                current_total = cl.user_session.get("total_tokens_used", 0)
                cl.user_session.set("total_tokens_used", current_total + total_tokens)
                
    
    await msg.update()
