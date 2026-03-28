import chainlit as cl
import uuid
import urllib.parse
from chainlit.types import ThreadDict
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from langchain_core.messages import HumanMessage, BaseMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langchain_google_genai import ChatGoogleGenerativeAI
from typing import TypedDict, Sequence, Annotated, Dict, Optional, Any, Literal, NotRequired
import os
import logging
from dotenv import load_dotenv
from config import config
from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres import AsyncPostgresStore
from includes.commands import handle_deleteall_command
from includes.actions import dispatch_action, get_actions_for_user, is_help_request, send_action_buttons
from includes.document_processing import process_file, create_multimodal_content
from includes.local_storage_client import LocalStorageClient
from includes.mcp_config import load_mcp_config
from includes.agents import BrowserAgent, GeneralAgent, ProcurementAgent, Supervisor, SysAdminAgent
from includes.job_runner import JobRunner
from langchain_mcp_adapters.client import MultiServerMCPClient
from starlette.responses import RedirectResponse

# Set up Chainlit static file serving for local file attachments
import chainlit.server as cl_server
from fastapi.staticfiles import StaticFiles

# Create the data directory if it doesn't exist
os.makedirs(os.path.join(config.DATA_DIR, "attachments"), exist_ok=True)


class OAuthErrorRedirectMiddleware:
    """Pure ASGI middleware: redirects 401s on OAuth callback paths to the login page.

    Uses raw ASGI to avoid the known issues that BaseHTTPMiddleware causes with
    WebSocket connections and streaming responses in Starlette/Chainlit.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # Only intercept HTTP requests on OAuth callback paths
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not (path.startswith("/auth/oauth/") and path.endswith("/callback")):
            await self.app(scope, receive, send)
            return

        status_code = None

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                if status_code == 401:
                    params = urllib.parse.urlencode(
                        {"error": "Access denied. Your account is not authorised to use this application."}
                    )
                    redirect_url = f"/login?{params}"
                    await send({
                        "type": "http.response.start",
                        "status": 302,
                        "headers": [
                            [b"location", redirect_url.encode()],
                            [b"content-length", b"0"],
                        ],
                    })
                    return
                await send(message)
            elif message["type"] == "http.response.body":
                if status_code == 401:
                    await send({"type": "http.response.body", "body": b""})
                    return
                await send(message)

        await self.app(scope, receive, send_wrapper)


# Guard module-level ASGI app modifications so they only run once.
# On hot-reload Chainlit re-executes this module; adding middleware or
# mounting routes a second time would crash with "Cannot add middleware
# after an application has started".
if not getattr(cl_server.app, "_eagleagent_patched", False):
    cl_server.app._eagleagent_patched = True

    # Mount the local data directory to the /files route so Chainlit UI can load images
    cl_server.app.mount(
        "/files",
        StaticFiles(directory=os.path.join(config.DATA_DIR, "attachments")),
        name="files",
    )

    cl_server.app.add_middleware(OAuthErrorRedirectMiddleware)

    # FIX: Chainlit has a catch-all route `/{full_path:path}` that intercepts
    # `/files` if our mount is at the end. Move our mount BEFORE the catch-all.
    routes = cl_server.app.router.routes
    files_mount = routes.pop()
    catch_all_idx = next(
        (i for i, r in enumerate(routes) if getattr(r, 'path', '') == '/{full_path:path}'),
        len(routes),
    )
    routes.insert(catch_all_idx, files_mount)

# Load environment variables (still needed for secrets like GOOGLE_API_KEY)
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Define which tools require Admin privileges
ADMIN_ONLY_TOOLS = ["delete_all_user_data"]

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
# Model configuration is in config/settings.py (DEFAULT_MODEL + per-agent overrides)
# API key is loaded from environment variable (secret)
def create_model(agent_name: str) -> ChatGoogleGenerativeAI:
    """Create a model instance for a specific agent, using per-agent model overrides."""
    return ChatGoogleGenerativeAI(
        model=config.get_agent_model(agent_name),
        google_api_key=os.getenv("GOOGLE_API_KEY")
    )

# Initialize agents
browser_agent = None
general_agent = None
supervisor_node = None

# Define the state with supervisor pattern
class SupervisorState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    user_id: str  # User email for cross-thread memory lookup
    file_attachments: NotRequired[list[Dict[str, Any]]]  # Optional: uploaded file metadata
    next_agent: NotRequired[str]  # Which agent to route to



# Add PostgreSQL-based memory to persist state across interactions and restarts
checkpointer = None

# Compile graph with both checkpointer (thread state) and store (cross-thread memory)
graph = None

# System Admin graph — single-agent for admin script/job management
sysadmin_graph = None

# Background job runner for admin script execution
job_runner = JobRunner()

globals_initialized = False

async def setup_globals():
    """Initialize async-dependent global variables."""
    global store, mcp_client, browser_agent, general_agent, supervisor_node, checkpointer, graph, sysadmin_graph, globals_initialized
    
    if globals_initialized:
        return
        
    # Open pg_pool
    try:
        await pg_pool.open()
    except Exception:
        pass

    # Start the background job runner
    await job_runner.start()
        
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
        
    # Initialize agents
    browser_agent = BrowserAgent(model=create_model("BrowserAgent"), store=store)
    procurement_agent = ProcurementAgent(model=create_model("ProcurementAgent"), store=store)
    general_agent = GeneralAgent(model=create_model("GeneralAgent"), store=store, mcp_client=mcp_client, admin_only_tools=ADMIN_ONLY_TOOLS)
    supervisor_node = Supervisor(model=create_model("Supervisor"))
    
    # Build the graph inside setup_globals where agents are initialized
    builder = StateGraph(SupervisorState)

    async def run_supervisor(state, config):
        return await supervisor_node(state, config)
    
    async def run_general(state, config):
        return await general_agent(state, config)
        
    async def run_browser(state, config):
        return await browser_agent(state, config)

    async def run_procurement(state, config):
        return await procurement_agent(state, config)

    # Add nodes
    builder.add_node("Supervisor", run_supervisor)
    builder.add_node("GeneralAgent", run_general)
    builder.add_node("BrowserAgent", run_browser)
    builder.add_node("ProcurementAgent", run_procurement)

    # Add edges
    builder.add_edge(START, "Supervisor")

    # Conditional routing from Supervisor
    def router(state: SupervisorState) -> Literal["GeneralAgent", "BrowserAgent", "ProcurementAgent", "__end__"]:
        next_agent = state.get("next_agent", "FINISH")
        if next_agent == "GeneralAgent":
            return "GeneralAgent"
        elif next_agent == "BrowserAgent":
            return "BrowserAgent"
        elif next_agent == "ProcurementAgent":
            return "ProcurementAgent"
        else:
            return END

    builder.add_conditional_edges("Supervisor", router)

    # Agents always route back to Supervisor
    builder.add_edge("GeneralAgent", "Supervisor")
    builder.add_edge("BrowserAgent", "Supervisor")
    builder.add_edge("ProcurementAgent", "Supervisor")

    # Compile graph
    graph = builder.compile(checkpointer=checkpointer, store=store)

    # Build System Admin graph — single-agent, no supervisor routing
    sysadmin_agent = SysAdminAgent(
        model=create_model("SysAdminAgent"), store=store, job_runner=job_runner
    )

    async def run_sysadmin(state, config):
        return await sysadmin_agent(state, config)

    sa_builder = StateGraph(SupervisorState)
    sa_builder.add_node("SysAdminAgent", run_sysadmin)
    sa_builder.add_edge(START, "SysAdminAgent")
    sa_builder.add_edge("SysAdminAgent", END)
    sysadmin_graph = sa_builder.compile(checkpointer=checkpointer, store=store)

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
                logger.warning(f"Authentication rejected: domain '{user_domain}' not in allowed list: {allowed_domains}")
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

async def _ensure_user_profile(user: cl.User) -> tuple:
    """Load or create a user profile and resolve their display name.
    
    Returns:
        (user_name, is_new_user) where user_name may be None if no user is provided.
    """
    user_profile = await store.aget(("users",), user.identifier)
    is_new_user = False
    
    if not user_profile or not user_profile.value:
        is_new_user = True
        profile_data = {
            "first_name": user.metadata.get("given_name", "") if user.metadata else "",
            "last_name": user.metadata.get("family_name", "") if user.metadata else "",
            "full_name": user.metadata.get("name", "") if user.metadata else "",
            "email": user.metadata.get("email", user.identifier) if user.metadata else user.identifier
        }
        await store.aput(("users",), user.identifier, profile_data)
        user_profile = await store.aget(("users",), user.identifier)

    # Resolve display name: preferred_name > given_name from OAuth > email
    user_name = None
    if user_profile and user_profile.value and "preferred_name" in user_profile.value:
        user_name = user_profile.value["preferred_name"]
    if not user_name and user.metadata and "given_name" in user.metadata:
        user_name = user.metadata["given_name"]
    if not user_name:
        user_name = user.identifier
    
    return user_name, is_new_user


@cl.set_chat_profiles
async def chat_profile(current_user: cl.User):
    """Define available chat profiles. Admin users see the System Admin profile."""
    is_admin = (
        current_user
        and current_user.identifier.lower() in config.get_admin_emails()
    )

    profiles = [
        cl.ChatProfile(
            name="EagleAgent",
            markdown_description="General assistant for product procurement, supplier information, and web search.",
            icon="/public/avatars/EagleAgent.png",
            default=True,
        ),
    ]

    if is_admin:
        profiles.append(
            cl.ChatProfile(
                name="System Admin",
                markdown_description="Server administration — run scripts, manage background jobs, and system maintenance.",
                icon="/public/avatars/EagleAgent.png",
                starters=[
                    cl.Starter(
                        label="List available scripts",
                        message="What scripts are available?",
                        icon="/public/avatars/EagleAgent.png",
                    ),
                    cl.Starter(
                        label="Check running jobs",
                        message="Are there any running jobs?",
                        icon="/public/avatars/EagleAgent.png",
                    ),
                    cl.Starter(
                        label="Update product embeddings",
                        message="Please update the product embeddings",
                        icon="/public/avatars/EagleAgent.png",
                    ),
                ],
            )
        )

    return profiles


@cl.set_starters
async def set_starters():
    """Show suggested prompts when a new chat thread starts."""
    return [
        cl.Starter(
            label="What can you help me with?",
            message="What can you help me with?",
            icon="/public/avatars/EagleAgent.png",
        ),
        cl.Starter(
            label="Search for a product",
            message="I need to find a product",
            icon="/public/avatars/EagleAgent.png",
        ),
        cl.Starter(
            label="Show available actions",
            message="actions",
            icon="/public/avatars/EagleAgent.png",
        ),
    ]


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
    
    # Load/create user profile and resolve display name
    user_name = None
    is_first_visit = False
    
    if user:
        cl.user_session.set("user_id", user.identifier)
        user_name, is_first_visit = await _ensure_user_profile(user)
    
    # Select graph based on chosen chat profile
    chat_profile_name = cl.user_session.get("chat_profile")
    if chat_profile_name == "System Admin":
        cl.user_session.set("active_graph", sysadmin_graph)
    else:
        cl.user_session.set("active_graph", graph)

    # Personalized welcome message
    if chat_profile_name == "System Admin":
        if user_name:
            welcome_msg = f"Welcome to System Admin mode, {user_name}. I can run scripts, check background jobs, and manage system tasks. What would you like to do?"
        else:
            welcome_msg = "Welcome to System Admin mode. I can run scripts, check background jobs, and manage system tasks. What would you like to do?"

        # Embedding update buttons for System Admin profile
        action_buttons = [
            cl.Action(
                name="confirm_run_script",
                payload={"script_name": "update_product_embeddings"},
                label="Update Product Embeddings",
                description="Regenerate missing product vector embeddings",
            ),
            cl.Action(
                name="confirm_run_script",
                payload={"script_name": "update_supplier_embeddings"},
                label="Update Supplier Embeddings",
                description="Regenerate missing supplier vector embeddings",
            ),
        ]
        await cl.Message(content=welcome_msg, actions=action_buttons).send()
    else:
        if is_first_visit and user_name:
            welcome_msg = f"Welcome to EagleAgent, {user_name}! I don't think we've met before. Is it OK to call you {user_name} or do you have a preferred name?"
        elif is_first_visit:
            welcome_msg = "Welcome to EagleAgent! I don't think we've met before. What is your preferred name?"
        elif user_name:
            welcome_msg = f"Hello {user_name}! How can I help you today?"
        else:
            welcome_msg = "Hello! How can I help you today?"

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

    # Select graph based on chat profile (persisted with thread)
    chat_profile_name = cl.user_session.get("chat_profile")
    if chat_profile_name == "System Admin":
        cl.user_session.set("active_graph", sysadmin_graph)
    else:
        cl.user_session.set("active_graph", graph)
    
    # Log for debugging
    print(f"Resuming conversation with thread_id: {thread_id}")
    
    # Load/create user profile and resolve display name
    user_name = None
    if user:
        user_name, _ = await _ensure_user_profile(user)
    
    # Optional: Send a welcome back message
    if user_name:
        if chat_profile_name == "System Admin":
            action_buttons = [
                cl.Action(
                    name="confirm_run_script",
                    payload={"script_name": "update_product_embeddings"},
                    label="Update Product Embeddings",
                    description="Regenerate missing product vector embeddings",
                ),
                cl.Action(
                    name="confirm_run_script",
                    payload={"script_name": "update_supplier_embeddings"},
                    label="Update Supplier Embeddings",
                    description="Regenerate missing supplier vector embeddings",
                ),
            ]
            await cl.Message(
                content=f"Welcome back, {user_name}! Continuing System Admin session.",
                author="EagleAgent",
                actions=action_buttons,
            ).send()
        else:
            await cl.Message(
                content=f"Welcome back, {user_name}! Continuing our previous conversation.",
                author="EagleAgent",
            ).send()


# ---------------------------------------------------------------------------
# Shutdown hook — kill background jobs on app teardown
# ---------------------------------------------------------------------------

@cl.on_stop
async def on_stop():
    """Gracefully shut down the job runner when the app stops."""
    await job_runner.shutdown()


# ---------------------------------------------------------------------------
# Action button callbacks
# ---------------------------------------------------------------------------

@cl.action_callback("new_conversation")
async def on_action_new_conversation(action: cl.Action):
    """Handle the New Conversation action button."""
    await dispatch_action("new_conversation")


@cl.action_callback("delete_all_data")
async def on_action_delete_all_data(action: cl.Action):
    """Handle the Delete All Data action button (sends confirmation)."""
    await dispatch_action("delete_all_data")


@cl.action_callback("confirm_delete_all")
async def on_action_confirm_delete(action: cl.Action):
    """Handle the Yes/confirm button from the delete confirmation."""
    user_id = cl.user_session.get("user_id", "")
    if user_id:
        await handle_deleteall_command(user_id, store, pg_pool)

    new_thread = str(uuid.uuid4())
    cl.user_session.set("thread_id", new_thread)
    await cl.Message(
        content=(
            "🗑️ All stored knowledge, files, and conversation history about you "
            "has been completely erased from all databases.\n\n"
            "*Note: Please refresh your browser window now to clear this chat log.*"
        ),
        author="EagleAgent",
    ).send()


@cl.action_callback("cancel_delete_all")
async def on_action_cancel_delete(action: cl.Action):
    """Handle the Cancel button from the delete confirmation."""
    await cl.Message(
        content="Deletion cancelled. Resuming normal conversation.",
        author="EagleAgent",
    ).send()


@cl.action_callback("confirm_run_script")
async def on_action_confirm_run_script(action: cl.Action):
    """Run button from the run_script confirmation prompt."""
    from includes.job_progress import monitor_job

    script_name = action.payload.get("script_name", "")
    thread_id = cl.user_session.get("thread_id", "")

    try:
        job = await job_runner.run_script(script_name, [], thread_id=thread_id)
    except ValueError as e:
        await cl.Message(
            content=f"Could not start `{script_name}`: {e}",
            author="EagleAgent",
        ).send()
        return

    import asyncio
    asyncio.create_task(monitor_job(job_runner, job))


@cl.action_callback("cancel_run_script")
async def on_action_cancel_run_script(action: cl.Action):
    """Cancel button from the run_script confirmation prompt."""
    script_name = action.payload.get("script_name", "")
    await cl.Message(
        content=f"Cancelled — `{script_name}` was not started.",
        author="EagleAgent",
    ).send()


@cl.action_callback("cancel_job")
async def on_action_cancel_job(action: cl.Action):
    """Cancel button attached to job start messages."""
    job_id = action.payload.get("job_id", "")
    try:
        job = await job_runner.cancel(job_id)
        await cl.Message(
            content=f"Cancelled job `{job.id[:8]}` ({job.script_name}).",
            author="EagleAgent",
        ).send()
    except ValueError as e:
        await cl.Message(
            content=f"Could not cancel: {e}",
            author="EagleAgent",
        ).send()


@cl.on_message
async def main(message: cl.Message):
    # Use the session ID as the thread ID to maintain conversation history
    thread_id = cl.user_session.get("thread_id")
    user_id = cl.user_session.get("user_id", "")

    # Show action buttons when the user asks for help / actions
    if is_help_request(message.content):
        await send_action_buttons(user_id)
        return

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
                    author="EagleAgent"
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
    
    active_agent = "GeneralAgent"
    
    active_graph = cl.user_session.get("active_graph", graph)
    async for event in active_graph.astream_events(inputs, config=graph_config, version="v1"):
        kind = event["event"]
        name = event.get("name", "")
        tags = event.get("tags", [])
        
        if kind == "on_chain_start" and name in ["GeneralAgent", "BrowserAgent", "ProcurementAgent", "SysAdminAgent"]:
            active_agent = name
            
        # Skip streaming internal routing decisions
        if "supervisor_routing" in tags:
            continue
            
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
                token_info = f"\n\n<div style='margin-top:20px; font-size:0.8em; color:#a1a1aa; font-style:italic;'>Agent: {active_agent} | Tokens: {total_tokens:,} (Context: {prompt_tokens:,}, Generated: {completion_tokens:,})</div>\n\n"
                await msg.stream_token(token_info)
                
                # Track cumulative tokens in session
                current_total = cl.user_session.get("total_tokens_used", 0)
                cl.user_session.set("total_tokens_used", current_total + total_tokens)
                
    
    await msg.update()
