import chainlit as cl
import uuid
import urllib.parse
from chainlit.types import ThreadDict
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from datetime import datetime, timezone
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage
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
from includes.chat.commands import handle_deleteall_command
from includes.chat.actions import dispatch_action, get_actions_for_user, is_help_request, send_action_buttons
from includes.chat.document_processing import process_file, create_multimodal_content
from includes.chat.local_storage_client import LocalStorageClient
from includes.mcp_config import load_mcp_config
from includes.agents import BrowserAgent, GeneralAgent, ProcurementAgent, Supervisor, SysAdminAgent
from includes.job_runner import JobRunner
from langchain_mcp_adapters.client import MultiServerMCPClient
from starlette.responses import RedirectResponse
import asyncio

# Import per-RFQ lock from quote_tools (single source of truth)
from includes.tools.quote_tools import get_rfq_lock as _get_rfq_lock

# Set up Chainlit server reference for middleware patching
import chainlit.server as cl_server

# Create the data directory if it doesn't exist
os.makedirs(os.path.join(config.DATA_DIR, "attachments"), exist_ok=True)


class FixedSQLAlchemyDataLayer(SQLAlchemyDataLayer):
    """Fix two upstream Chainlit bugs in SQLAlchemyDataLayer:

    1. get_current_timestamp() uses datetime.now() (local time) + "Z" suffix,
       producing timestamps that claim to be UTC but are actually local time.
    2. update_thread() includes createdAt in the ON CONFLICT UPDATE clause,
       which overwrites the original creation date every time a step is created.

    Fix: override get_current_timestamp() and update_thread() to exclude
    createdAt from the UPDATE (only set it on initial INSERT).  The parent
    create_step() is intentionally left intact so that Chainlit's internal
    flush_thread_queues() can set userId, name, and tags on the thread row.
    """

    async def get_current_timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    async def update_thread(
        self,
        thread_id: str,
        name=None,
        user_id=None,
        metadata=None,
        tags=None,
    ):
        import json as _json

        if self.show_logger:
            logger.info(f"SQLAlchemy: update_thread, thread_id={thread_id}")

        user_identifier = None
        if user_id:
            user_identifier = await self._get_user_identifer_by_id(user_id)

        if metadata is not None:
            existing = await self.execute_sql(
                query='SELECT "metadata" FROM threads WHERE "id" = :id',
                parameters={"id": thread_id},
            )
            base = {}
            if isinstance(existing, list) and existing:
                raw = existing[0].get("metadata") or {}
                if isinstance(raw, str):
                    try:
                        base = _json.loads(raw)
                    except _json.JSONDecodeError:
                        base = {}
                elif isinstance(raw, dict):
                    base = raw
            incoming = {k: v for k, v in metadata.items() if v is not None}
            metadata = {**base, **incoming}

        name_value = name
        if name_value is None and metadata:
            name_value = metadata.get("name")
        created_at_value = (
            await self.get_current_timestamp() if metadata is None else None
        )

        data = {
            "id": thread_id,
            "createdAt": created_at_value,
            "name": name_value,
            "userId": user_id,
            "userIdentifier": user_identifier,
            "tags": ",".join(tags) if isinstance(tags, list) else tags,
            "metadata": _json.dumps(metadata) if metadata else None,
        }
        parameters = {
            key: value for key, value in data.items() if value is not None
        }
        columns = ", ".join(f'"{key}"' for key in parameters.keys())
        values = ", ".join(f":{key}" for key in parameters.keys())
        # FIX: exclude createdAt from the UPDATE so the original timestamp
        # is preserved when re-upserting the same thread.
        updates = ", ".join(
            f'"{key}" = EXCLUDED."{key}"'
            for key in parameters.keys()
            if key not in ("id", "createdAt")
        )

        if updates:
            query = f"""
                INSERT INTO threads ({columns})
                VALUES ({values})
                ON CONFLICT ("id") DO UPDATE
                SET {updates};
            """
        else:
            # Nothing to update — just ensure the row exists
            query = f"""
                INSERT INTO threads ({columns})
                VALUES ({values})
                ON CONFLICT ("id") DO NOTHING;
            """
        await self.execute_sql(query=query, parameters=parameters)


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

    cl_server.app.add_middleware(OAuthErrorRedirectMiddleware)

# Load environment variables (Vertex AI config, OAuth secrets, etc.)
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Surface Google API retries to Chainlit UI in real-time
# ---------------------------------------------------------------------------
class _GeminiRetryNotifier(logging.Handler):
    """Intercepts google_genai retry log messages and pushes them to the UI.
    
    Debounced: only sends one UI notification per 10-second window to avoid spam
    when Google does rapid-fire exponential backoff retries.
    """

    def __init__(self, level: int = logging.NOTSET):
        super().__init__(level)
        self._last_notified = 0.0

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "Retrying" not in msg:
            return
        if "503" not in msg and "429" not in msg:
            return
        import time
        now = time.monotonic()
        if now - self._last_notified < 10:
            return  # Debounce: skip if we notified recently
        self._last_notified = now
        try:
            import asyncio
            loop = asyncio.get_running_loop()
            loop.create_task(self._send_notification())
        except RuntimeError:
            pass  # No event loop — nothing we can do

    @staticmethod
    async def _send_notification() -> None:
        try:
            await cl.Message(
                content="\u23f3 Model temporarily overloaded — retrying automatically...",
                author="System",
            ).send()
        except Exception:
            pass  # Never break the app for a UI notification

_genai_logger = logging.getLogger("google_genai._api_client")
_genai_logger.addHandler(_GeminiRetryNotifier(level=logging.INFO))


# Define which tools require Admin privileges
ADMIN_ONLY_TOOLS = ["delete_all_user_data"]

# Initialize PostgreSQL connection pool
# Using the CHECKPOINT_DATABASE_URL which defaults to psycopg style dsns
pg_pool = AsyncConnectionPool(
    config.CHECKPOINT_DATABASE_URL,
    min_size=1,
    max_size=10,
    kwargs={
        "autocommit": True,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    },
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
# Auth is handled via Vertex AI env vars (GOOGLE_GENAI_USE_VERTEXAI, GOOGLE_APPLICATION_CREDENTIALS)
def create_model(agent_name: str) -> ChatGoogleGenerativeAI:
    """Create a model instance for a specific agent, using per-agent model overrides."""
    return ChatGoogleGenerativeAI(
        model=config.get_agent_model(agent_name),
        temperature=config.DEFAULT_TEMPERATURE,
        max_output_tokens=config.DEFAULT_MAX_TOKENS,
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
    intent_context: NotRequired[str]  # Procurement intent context from action buttons



# Add PostgreSQL-based memory to persist state across interactions and restarts
checkpointer = None

# Compile graph with both checkpointer (thread state) and store (cross-thread memory)
graph = None

# System Admin graph — single-agent for admin script/job management
sysadmin_graph = None

# Research graph — single-agent with Google Search grounding
research_graph = None

# Background job runner for admin script execution
job_runner = JobRunner()

globals_initialized = False

async def setup_globals():
    """Initialize async-dependent global variables."""
    global store, mcp_client, browser_agent, general_agent, supervisor_node, checkpointer, graph, sysadmin_graph, research_graph, globals_initialized
    
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
    # NOTE: BrowserAgent is NOT USED in the Eagle graph — disabled to avoid misrouting
    browser_agent = BrowserAgent(model=create_model("BrowserAgent"), store=store)
    procurement_agent = ProcurementAgent(model=create_model("ProcurementAgent"), store=store)
    general_agent = GeneralAgent(model=create_model("GeneralAgent"), store=store, mcp_client=mcp_client, admin_only_tools=ADMIN_ONLY_TOOLS)
    supervisor_node = Supervisor(model=create_model("Supervisor"))
    
    # Build the graph inside setup_globals where agents are initialized
    from includes.agents import ResearchAgent
    research_agent = ResearchAgent(
        model=create_model("ResearchAgent"), store=store,
        include_rfq_tools=True,  # Has RFQ tools when used inside Eagle Agent graph
    )

    builder = StateGraph(SupervisorState)

    async def run_supervisor(state, config):
        return await supervisor_node(state, config)
    
    async def run_general(state, config):
        return await general_agent(state, config)

    async def run_procurement(state, config):
        return await procurement_agent(state, config)

    async def run_research(state, config):
        return await research_agent(state, config)

    # Add nodes
    builder.add_node("Supervisor", run_supervisor)
    builder.add_node("GeneralAgent", run_general)
    builder.add_node("ProcurementAgent", run_procurement)
    builder.add_node("ResearchAgent", run_research)

    # Add edges
    builder.add_edge(START, "Supervisor")

    # Conditional routing from Supervisor
    def router(state: SupervisorState) -> Literal["GeneralAgent", "ProcurementAgent", "ResearchAgent", "__end__"]:
        next_agent = state.get("next_agent", "FINISH")
        if next_agent == "GeneralAgent":
            return "GeneralAgent"
        elif next_agent == "ProcurementAgent":
            return "ProcurementAgent"
        elif next_agent == "ResearchAgent":
            return "ResearchAgent"
        else:
            return END

    builder.add_conditional_edges("Supervisor", router)

    # Agents always route back to Supervisor
    builder.add_edge("GeneralAgent", "Supervisor")
    builder.add_edge("ProcurementAgent", "Supervisor")
    builder.add_edge("ResearchAgent", "Supervisor")

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

    # Build Research graph — standalone single-agent profile (no RFQ tools)
    standalone_research_agent = ResearchAgent(
        model=create_model("ResearchAgent"), store=store,
        include_rfq_tools=False,
    )

    async def run_standalone_research(state, config):
        return await standalone_research_agent(state, config)

    ra_builder = StateGraph(SupervisorState)
    ra_builder.add_node("ResearchAgent", run_standalone_research)
    ra_builder.add_edge(START, "ResearchAgent")
    ra_builder.add_edge("ResearchAgent", END)
    research_graph = ra_builder.compile(checkpointer=checkpointer, store=store)

    globals_initialized = True

@cl.header_auth_callback
async def header_auth_callback(headers) -> Optional[cl.User]:
    """Authenticate users via headers injected by the FastAPI middleware.

    The FastAPI app handles Google OAuth and stores user info in the session.
    For requests to /chat, the middleware injects X-Chainlit-User-* headers.
    This callback reads those headers and returns a Chainlit User object.

    Falls back to the legacy @cl.oauth_callback flow if no header is present
    (e.g. when running Chainlit standalone during development).
    """
    email = headers.get("x-chainlit-user-email", "").strip()
    if not email:
        # No header auth — user hasn't logged in via FastAPI yet
        return None

    user = cl.User(identifier=email)
    user.metadata = {
        "name": headers.get("x-chainlit-user-name", ""),
        "given_name": headers.get("x-chainlit-user-given-name", ""),
        "family_name": headers.get("x-chainlit-user-family-name", ""),
        "email": email,
        "picture": headers.get("x-chainlit-user-picture", ""),
        "hd": headers.get("x-chainlit-user-hd", ""),
    }
    return user

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
    
    return FixedSQLAlchemyDataLayer(
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


# ---------------------------------------------------------------------------
# Command helpers — map INTENTS to Chainlit CommandDicts
# ---------------------------------------------------------------------------

# Map emoji icons from INTENTS to Lucide icon names used by Commands
_LUCIDE_ICONS = {
    "🏭": "factory",
    "📦": "package",
    "🔍": "search",
    "🏷️": "tag",
    "📋": "clipboard-list",
    "🔎": "search",
    "🌐": "globe",
}


def _intents_to_commands(intents: dict) -> list[dict]:
    """Convert an INTENTS dict to a list of Chainlit CommandDicts."""
    return [
        {
            "id": intent["label"],
            "description": intent["description"],
            "icon": _LUCIDE_ICONS.get(intent["icon"], "circle"),
            "button": True,
            "persistent": False,
        }
        for name, intent in intents.items()
    ]


# Reverse lookup: command label → intent key
def _command_to_intent_name(command_label: str) -> str | None:
    """Map a command label back to an intent key."""
    from includes.prompts import INTENTS, RESEARCH_INTENTS
    for name, intent in {**INTENTS, **RESEARCH_INTENTS}.items():
        if intent["label"] == command_label:
            return name
    return None





@cl.set_chat_profiles
async def chat_profile(current_user: cl.User):
    """Define available chat profiles. Admin users see the Internal Agent profile."""
    is_admin = (
        current_user
        and current_user.identifier.lower() in config.get_admin_emails()
    )

    profiles = [
        cl.ChatProfile(
            name="Eagle Agent",
            markdown_description="Supplier lookup agent — search our supplier database by name, brand, or description.",
            icon="/public/avatars/EagleAgent.png",
            default=True,
        ),
        cl.ChatProfile(
            name="Research Agent",
            markdown_description="Search the web for information and research topics.",
            icon="/public/avatars/EagleAgent.png",
        ),
    ]

    if is_admin:
        profiles.append(
            cl.ChatProfile(
                name="Internal Agent",
                markdown_description="General assistant for product procurement, supplier information, and RFQs.",
                icon="/public/avatars/EagleAgent.png",
            )
        )

    return profiles


@cl.on_chat_start
async def start():
    import uuid
    
    # Immediately clear stale commands from the previous chat profile so
    # the user never sees the old profile's buttons during initialisation.
    await cl.context.emitter.set_commands([])
    
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
    if chat_profile_name == "Research Agent":
        cl.user_session.set("active_graph", research_graph)
    else:
        cl.user_session.set("active_graph", graph)

    # Personalized welcome message
    if chat_profile_name == "Research Agent":
        if is_first_visit and user_name:
            welcome_msg = f"Welcome to Research Agent, {user_name}! I can help you search the web for information about products and their suppliers."
        elif is_first_visit:
            welcome_msg = "Welcome to Research Agent! I can help you search the web for information about products and their suppliers."
        elif user_name:
            welcome_msg = f"Hello {user_name}! I can help you search the web for information about products and their suppliers."
        else:
            welcome_msg = "Hello! I can help you search the web for information about products and their suppliers."

        from includes.prompts import RESEARCH_INTENTS
        await cl.context.emitter.set_commands(_intents_to_commands(RESEARCH_INTENTS))
        await cl.Message(content=welcome_msg).send()
    elif chat_profile_name == "Internal Agent":
        if is_first_visit and user_name:
            welcome_msg = f"Welcome to Internal Agent, {user_name}! I don't think we've met before. Is it OK to call you {user_name} or do you have a preferred name?"
        elif is_first_visit:
            welcome_msg = "Welcome to Internal Agent! I don't think we've met before. What is your preferred name?"
        elif user_name:
            welcome_msg = f"Hello {user_name}! I can help you search our internal database for historical records about products, brands and suppliers."
        else:
            welcome_msg = "Hello! I can help you search our internal database for historical records about products, brands and suppliers."

        # Set procurement intent commands next to the chat input box
        from includes.prompts import INTENTS
        await cl.context.emitter.set_commands(_intents_to_commands(INTENTS))
        await cl.Message(content=welcome_msg).send()
    else:
        # Eagle Agent — default supplier lookup profile with command buttons
        if is_first_visit and user_name:
            welcome_msg = f"Welcome to Eagle Agent, {user_name}! I don't think we've met before. Is it OK to call you {user_name} or do you have a preferred name?"
        elif is_first_visit:
            welcome_msg = "Welcome to Eagle Agent! I don't think we've met before. What is your preferred name?"
        elif user_name:
            welcome_msg = f"Hello {user_name}! I can help you find suppliers. Give me a part number, brand name, supplier name, or description and I'll search our database."
        else:
            welcome_msg = "Hello! I can help you find suppliers. Give me a part number, brand name, supplier name, or description and I'll search our database."

        from includes.prompts import INTENTS
        eagle_commands = {k: v for k, v in INTENTS.items() if k == "new_rfq"}
        await cl.context.emitter.set_commands(_intents_to_commands(eagle_commands))
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

    # Normalize legacy chat profile names (EagleAgent → Eagle Agent, System Admin → Eagle Agent)
    chat_profile_name = cl.user_session.get("chat_profile")
    if chat_profile_name in ("EagleAgent", "System Admin"):
        chat_profile_name = "Eagle Agent"
        cl.user_session.set("chat_profile", chat_profile_name)

    # Select graph based on chat profile (persisted with thread)
    chat_profile_name = cl.user_session.get("chat_profile")
    if chat_profile_name == "Research Agent":
        cl.user_session.set("active_graph", research_graph)
    else:
        cl.user_session.set("active_graph", graph)
    
    # Log for debugging
    print(f"Resuming conversation with thread_id: {thread_id} (profile: {chat_profile_name})")
    
    # Load/create user profile and resolve display name
    user_name = None
    if user:
        user_name, _ = await _ensure_user_profile(user)
    
    # Restore commands and send a transient welcome-back message
    # (skip DB persistence so resumed threads don't accumulate duplicates)
    if user_name:
        if chat_profile_name == "Research Agent":
            from includes.prompts import RESEARCH_INTENTS
            await cl.context.emitter.set_commands(_intents_to_commands(RESEARCH_INTENTS))
            msg = cl.Message(
                content=f"Welcome back, {user_name}! Continuing your research session.",
                author="EagleAgent",
            )
        elif chat_profile_name == "Internal Agent":
            from includes.prompts import INTENTS
            await cl.context.emitter.set_commands(_intents_to_commands(INTENTS))
            msg = cl.Message(
                content=f"Welcome back, {user_name}! Continuing our previous conversation.",
                author="EagleAgent",
            )
        else:
            # Eagle Agent — only New RFQ command button
            from includes.prompts import INTENTS
            eagle_commands = {k: v for k, v in INTENTS.items() if k == "new_rfq"}
            await cl.context.emitter.set_commands(_intents_to_commands(eagle_commands))
            msg = cl.Message(
                content=f"Welcome back, {user_name}! Continuing our previous conversation.",
                author="EagleAgent",
            )
        msg.persisted = True  # skip DB write — display only
        await msg.send()


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


@cl.action_callback("rfq_refresh")
async def on_rfq_refresh(action: cl.Action):
    """Refresh the dashboard RFQ view with latest data."""
    from includes.agent_bridge import notify_dashboard

    payload = action.payload or {}
    rfq_id = payload.get("rfq_id")
    if not rfq_id or not store:
        return

    await notify_dashboard("dashboard_refresh")


@cl.action_callback("rfq_update_supplier")
async def on_rfq_update_supplier(action: cl.Action):
    """Handle supplier status change from dashboard."""
    from includes.agent_bridge import notify_dashboard
    from includes.tools.quote_tools import NAMESPACE

    payload = action.payload or {}
    rfq_id = payload.get("rfq_id")
    line = payload.get("line")
    supplier_name = payload.get("supplier_name")
    new_status = payload.get("status")

    if not all([rfq_id, line, supplier_name, new_status, store]):
        return

    async with _get_rfq_lock(rfq_id):
        item = await store.aget(NAMESPACE, rfq_id)
        if not item:
            return
        rfq = item.value

        line_item = next((i for i in rfq.get("items", []) if i["line"] == line), None)
        if not line_item:
            return

        supplier = next(
            (s for s in line_item.get("suppliers", []) if s["name"] == supplier_name),
            None,
        )
        if not supplier:
            return

        old_status = supplier.get("status", "candidate")
        supplier["status"] = new_status

        user_id = cl.user_session.get("user_id", "unknown")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rfq.setdefault("history", []).append({
            "date": now, "user": user_id,
            "action": f"Changed supplier '{supplier_name}' on line {line} from {old_status} to {new_status}",
        })

        await store.aput(NAMESPACE, rfq_id, rfq)

    # Refresh the dashboard view
    await notify_dashboard("dashboard_refresh")

    status_label = new_status.replace("_", " ")
    await cl.Message(
        content=f"Updated **{supplier_name}** on line {line} of {rfq_id} → *{status_label}*",
        author="EagleAgent",
    ).send()


@cl.action_callback("rfq_identify_items")
async def on_rfq_identify_items(action: cl.Action):
    """Handle Identify Items button from RFQ custom element.

    Phase 1: Search internal product DB by part number, supplier code,
             and description for each unidentified item.
             Update the RFQ directly for any exact matches (with product_id).
    Phase 2: Route unmatched items to ResearchAgent for web-based
             identification (must be 100% positive match).
    """
    import asyncio
    from includes.agent_bridge import notify_dashboard
    from includes.tools.quote_tools import NAMESPACE
    from includes.tools.product_tools import _find_product_exact, _find_product_by_supplier_code

    payload = action.payload or {}
    rfq_id = payload.get("rfq_id", "???")
    unidentified_items = payload.get("items", [])

    if not unidentified_items or not store:
        return

    await cl.Message(
        content=f"Identifying {len(unidentified_items)} item(s) in {rfq_id}...",
        author="EagleAgent",
    ).send()

    # ---- Phase 1: Internal DB search ----
    matched = []      # list of dicts: line, part_number, brand, product_id
    unmatched = []     # items that need web search

    for ui_item in unidentified_items:
        line = ui_item.get("line")
        description = ui_item.get("description", "")
        part_number = ui_item.get("part_number", "")
        brand = ui_item.get("brand", "")

        product = None
        # Try exact part number match first (most specific)
        if part_number:
            try:
                product = await asyncio.to_thread(
                    _find_product_exact, part_number, brand or None,
                )
            except Exception as e:
                logger.warning(f"Phase 1 product search failed for line {line}: {e}")

        # Try supplier code search if part number didn't match
        if not product and part_number:
            try:
                product = await asyncio.to_thread(
                    _find_product_by_supplier_code, part_number, brand or None,
                )
            except Exception as e:
                logger.warning(f"Phase 1 supplier code search failed for line {line}: {e}")

        if product:
            matched.append({
                "line": line,
                "part_number": product["part_number"],
                "brand": product["brand"],
                "product_id": product["id"],
            })
        else:
            unmatched.append(ui_item)

    # Update RFQ with matched items
    if matched and store:
        async with _get_rfq_lock(rfq_id):
            item = await store.aget(NAMESPACE, rfq_id)
            if item:
                rfq = item.value
                for m in matched:
                    line_item = next(
                        (i for i in rfq.get("items", []) if i["line"] == m["line"]), None,
                    )
                    if line_item:
                        line_item["part_number"] = m["part_number"]
                        line_item["brand"] = m["brand"]
                        line_item["product_id"] = m["product_id"]
                        line_item["status"] = "confirmed"
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                user_id = cl.user_session.get("user_id", "unknown")
                rfq.setdefault("history", []).append({
                    "date": now, "user": user_id,
                    "action": f"Auto-identified {len(matched)} item(s) from internal DB: lines {', '.join(str(m['line']) for m in matched)}",
                })
                await store.aput(NAMESPACE, rfq_id, rfq)
                await notify_dashboard("dashboard_refresh")

    # Notify user of Phase 1 results
    if matched:
        match_desc = ", ".join(f"line {m['line']} → {m['part_number']} ({m['brand']})" for m in matched)
        msg = f"Identified {len(matched)} item(s) from our product database: {match_desc}."
        if unmatched:
            msg += f" Searching the web for {len(unmatched)} remaining item(s)..."
        await cl.Message(content=msg, author="EagleAgent").send()
    elif unmatched:
        await cl.Message(
            content=f"No exact matches found in our product database for {len(unmatched)} item(s). Searching the web...",
            author="EagleAgent",
        ).send()

    # ---- Phase 2: Route unmatched items to ResearchAgent for web search ----
    if unmatched:
        parts = ["web_research"]
        parts.append(f"Identify the following unidentified product(s) from {rfq_id}.")
        parts.append("For each item, search the web to verify the part number and find a positive product match.")
        parts.append("")
        for ui_item in unmatched:
            line = ui_item.get("line")
            desc = ui_item.get("description", "")
            pn = ui_item.get("part_number", "")
            br = ui_item.get("brand", "")
            item_parts = [f"Line {line}: {desc}"]
            if pn:
                item_parts.append(f"  Code/Part number: {pn}")
            if br:
                item_parts.append(f"  Brand: {br}")
            parts.append("\n".join(item_parts))
        parts.append("")
        parts.append("IMPORTANT — Part number validation:")
        parts.append("For each item, search the web to verify BOTH that:")
        parts.append("  1. The part number actually exists as a real product")
        parts.append("  2. The product that part number refers to matches the given description")
        parts.append("For example, if the description says 'Hydraulic Return Filter' but the part number")
        parts.append("resolves to an oil filter or a completely different product, that is a mismatch.")
        parts.append("")
        parts.append("Flag an item for review (status='review') if ANY of these are true:")
        parts.append("- The exact part number cannot be found online")
        parts.append("- The part number exists but refers to a different product than the description")
        parts.append("- Similar/close part numbers exist that better match the description (possible typo)")
        parts.append("In review cases, add a notes field explaining the issue")
        parts.append("(e.g. 'Part number not found. Closest matches: 201-60-71180, 201-01-71110'")
        parts.append(" or 'Part number 600-211-2110 resolves to a fuel filter, not an oil filter as described').")
        parts.append("")
        parts.append("For each item:")
        parts.append("- EXACT match AND description matches: set part_number, brand, status='confirmed'")
        parts.append("- Part number wrong, missing, or mismatched to description: set status='review' and notes='...' explaining the issue. Do NOT clear or remove the existing part_number or brand — keep them as-is so the user can see what was originally provided.")
        parts.append("- Cannot identify at all: leave unchanged")
        parts.append("Do NOT set status='confirmed' unless you are 100% certain the part number is correct AND matches the description.")

        rich_prompt = "\n".join(parts)

        short_label = f"Identify {len(unmatched)} unmatched item(s) in {rfq_id} via web search"
        synthetic = cl.Message(content=short_label)
        synthetic.author = "User"
        synthetic.intent_context = rich_prompt  # carry context per-message to avoid race
        await main(synthetic)
    elif not matched:
        await cl.Message(
            content="All items could not be identified. Try adding more details (part numbers, brands) to help.",
            author="EagleAgent",
        ).send()


@cl.action_callback("rfq_find_suppliers")
async def on_rfq_find_suppliers(action: cl.Action):
    """Handle Find Suppliers button from RFQ custom element.

    Phase 1: Search internal DB for suppliers (purchase history + supplier DB).
             Add any found directly to the RFQ with supplier_id and purchase refs.
    Phase 2: Route to ResearchAgent for web-based supplier discovery,
             with full context of what was already found internally.
    """
    import asyncio
    from includes.agent_bridge import notify_dashboard
    from includes.tools.quote_tools import NAMESPACE
    from includes.tools.product_tools import (
        _find_purchase_history_for_part,
    )

    payload = action.payload or {}
    rfq_id = payload.get("rfq_id", "???")
    line = payload.get("line")
    description = payload.get("description", "")
    part_number = payload.get("part_number", "")
    brand = payload.get("brand", "")
    quantity = payload.get("quantity", "")
    uom = payload.get("uom", "ea")
    existing = payload.get("existing_suppliers", [])

    # ---- Phase 1: Internal DB search ----
    existing_names_lower = {n.lower() for n in existing}
    internal_suppliers = []  # list of dicts to add to RFQ
    internal_summary_lines = []  # for the ResearchAgent prompt

    # 1a) Check purchase history if we have a part number
    if part_number:
        try:
            ph_rows = await asyncio.to_thread(_find_purchase_history_for_part, part_number, 20)
            for row in ph_rows:
                if row["name"].lower() not in existing_names_lower:
                    sup_entry = {
                        "supplier_id": row["supplier_id"],
                        "name": row["name"],
                        "contacts": row["contacts"],
                        "status": "candidate",
                        "price_type": "previous_purchase",
                        "price": row["price"],
                        "purchase_ref": {
                            "doc_number": row["doc_number"],
                            "date": row["date"],
                            "order_count": row["order_count"],
                        },
                    }
                    internal_suppliers.append(sup_entry)
                    existing_names_lower.add(row["name"].lower())
                    price_str = f"${row['price']:,.2f}" if row["price"] else "N/A"
                    internal_summary_lines.append(
                        f"- {row['name']} (previous purchase, price: {price_str}, orders: {row['order_count']})"
                    )
        except Exception as e:
            logger.warning(f"Phase 1 purchase history search failed: {e}")

    # 1b) Brand-based search removed — too broad (returns suppliers for the
    #     brand, not this specific part). The web search in Phase 2 handles
    #     finding alternative suppliers.

    # Add internal suppliers to the RFQ (with dedup)
    if internal_suppliers and store:
        async with _get_rfq_lock(rfq_id):
            item = await store.aget(NAMESPACE, rfq_id)
            if item:
                rfq = item.value
                line_item = next((i for i in rfq.get("items", []) if i["line"] == line), None)
                if line_item:
                    existing_by_name = {
                        s["name"].lower(): s for s in line_item.get("suppliers", [])
                    }
                    added = 0
                    updated = 0
                    for sup in internal_suppliers:
                        existing = existing_by_name.get(sup["name"].lower())
                        if existing:
                            # Merge — update fields that have new data
                            for key in ["supplier_id", "contacts", "price", "price_type",
                                        "lead_time", "notes", "purchase_ref"]:
                                val = sup.get(key)
                                if val is not None and val != "" and val != []:
                                    existing[key] = val
                            updated += 1
                        else:
                            line_item["suppliers"].append(sup)
                            existing_by_name[sup["name"].lower()] = sup
                            added += 1
                    action_parts = []
                    if added:
                        action_parts.append(f"Added {added} supplier(s)")
                    if updated:
                        action_parts.append(f"Updated {updated} existing supplier(s)")
                    rfq.setdefault("history", []).append({
                        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "user": cl.user_session.get("user_id", "unknown"),
                        "action": f"Internal DB search on line {line}: {' | '.join(action_parts)}" if action_parts else f"No changes to line {line}",
                    })
                    await store.aput(NAMESPACE, rfq_id, rfq)
                    await notify_dashboard("dashboard_refresh")

    # Notify user of Phase 1 results
    if internal_suppliers:
        names = ", ".join(s["name"] for s in internal_suppliers)
        await cl.Message(
            content=f"Added {len(internal_suppliers)} supplier(s) from our records to line {line}: {names}. Now searching the web for more options...",
            author="EagleAgent",
        ).send()
    else:
        await cl.Message(
            content=f"No matching suppliers found in our records for line {line}. Searching the web...",
            author="EagleAgent",
        ).send()

    # ---- Phase 2: Route to ResearchAgent for web search ----
    all_existing = list(existing or []) + [s["name"] for s in internal_suppliers]

    parts = [f"research_suppliers"]
    parts.append(f"Find external suppliers for line {line} of {rfq_id}.")
    parts.append(f"Product description: {description}")
    if part_number:
        parts.append(f"Part number: {part_number}")
    if brand:
        parts.append(f"Brand: {brand}")
    if quantity:
        parts.append(f"Quantity needed: {quantity} {uom}")
    if all_existing:
        parts.append(f"Already have these suppliers (do NOT repeat them): {', '.join(all_existing)}")
    if internal_summary_lines:
        parts.append("Internal DB results:\n" + "\n".join(internal_summary_lines))
    parts.append("")
    parts.append("Search the web for distributors and wholesalers who can supply this product.")
    parts.append("Prioritise authorised distributors and industrial wholesalers over retail sources.")
    parts.append("If distributors are scarce, include reputable retailers as fallback options.")
    parts.append("Aim for 3-5 good supplier options but more is fine if they look like strong matches.")
    parts.append("")
    parts.append("CRITICAL: After researching, you MUST call manage_rfq(action='add_supplier') to add each supplier you find to the RFQ.")
    parts.append(f"Use rfq_id='{rfq_id}' and data={{line: {line}, suppliers: [...]}} with a list of all suppliers found.")
    parts.append("Each supplier dict must include: name, contacts (list with at least one of email/phone/url), and optionally price, price_type, lead_time.")
    parts.append("If you do NOT call add_supplier, the suppliers will NOT appear on the RFQ. The user is counting on you to update the RFQ directly.")
    parts.append("Include any pricing, lead time, or contact information you can find.")

    rich_prompt = "\n".join(parts)

    short_label = f"Search the web for suppliers for line {line}"
    if description:
        short_label += f" ({description[:60]})"

    synthetic = cl.Message(content=short_label)
    synthetic.author = "User"
    synthetic.intent_context = rich_prompt  # carry context per-message to avoid race
    await main(synthetic)


@cl.on_message
async def main(message: cl.Message):
    # Gemini requires at least one content part. If a command button was
    # clicked without text, use the command label as the prompt so the LLM
    # can infer intent from conversation history + command name.
    if not message.content or not message.content.strip():
        if message.command:
            message.content = message.command
        else:
            await cl.Message(content="Please enter some text to get started.").send()
            return

    # Use the session ID as the thread ID to maintain conversation history
    thread_id = cl.user_session.get("thread_id")
    user_id = cl.user_session.get("user_id", "")

    # Show action buttons when the user asks for help / actions
    if is_help_request(message.content):
        await send_action_buttons(user_id)
        return

    # Direct RFQ loading — intercept "load/show/open RFQ" before the graph runs
    import re
    _rfq_load_keywords = ["load", "show", "open", "display", "get", "view", "pull up", "see"]
    msg_lower = message.content.lower()

    # Flexible RFQ ID matching: "RFQ-2026-0001", "RFQ 0001", "RFQ-0001", "rfq 2026-0001"
    rfq_match = re.search(r'\bRFQ[-\s]?(\d{4}[-\s]\d{4,}|\d{4,})\b', message.content, re.IGNORECASE)

    def _resolve_rfq_id(match_str: str) -> str:
        """Normalise a matched RFQ fragment to full RFQ-YYYY-NNNN format."""
        digits = re.sub(r'[-\s]', '', match_str)
        if len(digits) >= 8:  # e.g. "20260001"
            return f"RFQ-{digits[:4]}-{digits[4:]}"
        else:  # short form e.g. "0001" — assume current year
            from datetime import datetime
            return f"RFQ-{datetime.now().year}-{digits.zfill(4)}"

    # If no explicit RFQ ID but message looks like a load request, scan chat history
    if not rfq_match and any(kw in msg_lower for kw in _rfq_load_keywords) and "rfq" in msg_lower:
        history = cl.chat_context.to_openai()
        for hist_msg in reversed(history[:-1]):
            content = hist_msg.get("content", "") or ""
            hist_match = re.search(r'\bRFQ[-\s]?(\d{4}[-\s]\d{4,}|\d{4,})\b', content, re.IGNORECASE)
            if hist_match:
                rfq_match = hist_match
                break

    if rfq_match and any(kw in msg_lower for kw in _rfq_load_keywords):
        from includes.agent_bridge import notify_dashboard
        from includes.tools.quote_tools import NAMESPACE, _render_rfq_summary
        rfq_id = _resolve_rfq_id(rfq_match.group(1))
        if store:
            item = await store.aget(NAMESPACE, rfq_id)
            if item:
                await notify_dashboard("agent_navigate", {"url": f"/rfqs/{rfq_id}"})
                await cl.Message(content=f"Loaded **{rfq_id}** in the dashboard.", author="EagleAgent").send()
                return
            else:
                await cl.Message(content=f"RFQ **{rfq_id}** not found.", author="EagleAgent").send()
                return

    # Direct supplier clearing — intercept "clear/strip/remove suppliers" requests
    _clear_keywords = ["clear", "strip", "remove", "delete", "reset", "wipe"]
    _clear_targets = ["supplier", "suppliers", "quote", "quotes"]
    if any(kw in msg_lower for kw in _clear_keywords) and any(t in msg_lower for t in _clear_targets):
        # Resolve RFQ ID from message or chat history
        clear_rfq_match = rfq_match  # may already be set from above
        if not clear_rfq_match:
            history = cl.chat_context.to_openai()
            for hist_msg in reversed(history[:-1]):
                content = hist_msg.get("content", "") or ""
                hist_match = re.search(r'\bRFQ[-\s]?(\d{4}[-\s]\d{4,}|\d{4,})\b', content, re.IGNORECASE)
                if hist_match:
                    clear_rfq_match = hist_match
                    break
        if clear_rfq_match and store:
            from includes.agent_bridge import notify_dashboard
            from includes.tools.quote_tools import NAMESPACE
            from datetime import datetime, timezone
            clear_rfq_id = _resolve_rfq_id(clear_rfq_match.group(1))
            async with _get_rfq_lock(clear_rfq_id):
                item = await store.aget(NAMESPACE, clear_rfq_id)
                if item:
                    rfq = item.value
                    # Check if a specific line number was mentioned
                    line_match = re.search(r'\bline\s+(\d+)\b', msg_lower)
                    target_line = int(line_match.group(1)) if line_match else None

                    # Safety: require a specific line number or explicit "all" to clear everything
                    if target_line is None and "all" not in msg_lower:
                        lines_with_suppliers = sum(
                            1 for it in rfq.get("items", []) if it.get("suppliers")
                        )
                        await cl.Message(
                            content=(
                                f"**{clear_rfq_id}** has suppliers on {lines_with_suppliers} line(s). "
                                "Please specify a line number (e.g. \"clear suppliers from line 3\") "
                                "or say \"clear **all** suppliers\" to confirm."
                            ),
                            author="EagleAgent",
                        ).send()
                        return

                    cleared = []
                    for line_item in rfq.get("items", []):
                        if target_line is not None and line_item["line"] != target_line:
                            continue
                        count = len(line_item.get("suppliers", []))
                        if count:
                            line_item["suppliers"] = []
                            cleared.append(f"line {line_item['line']} ({count})")
                    if cleared:
                        rfq.setdefault("history", []).append({
                            "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "user": user_id,
                            "action": f"Cleared suppliers from {', '.join(cleared)}",
                        })
                        await store.aput(NAMESPACE, clear_rfq_id, rfq)
                        await notify_dashboard("dashboard_refresh")
                        scope = f"line {target_line}" if target_line else f"all {len(cleared)} line(s)"
                        await cl.Message(
                            content=f"Cleared suppliers from **{clear_rfq_id}** ({scope}).",
                            author="EagleAgent",
                        ).send()
                    else:
                        scope = f"line {target_line}" if target_line else "any line"
                        await cl.Message(content=f"No suppliers to clear on {scope} of **{clear_rfq_id}**.", author="EagleAgent").send()
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
    
    # Inject dashboard context so the agent knows what the user is viewing.
    # Prepend to message_content (before HumanMessage is created) so it
    # travels with the user turn and is visible to the supervisor and agents.
    from includes.dashboard.context import format_context_for_prompt
    dashboard_ctx = format_context_for_prompt(user_id)
    if dashboard_ctx:
        logger.info(f"Dashboard context for {user_id}: {dashboard_ctx}")
        if isinstance(message_content, list):
            # Multimodal: prepend as a text block
            message_content = [{"type": "text", "text": dashboard_ctx + "\n\n"}] + message_content
        else:
            message_content = dashboard_ctx + "\n\n" + message_content

    # Run the graph with the new user message and user_id
    inputs = {
        "messages": [HumanMessage(content=message_content)],
        "user_id": user_id
    }
    # Always include the key so the checkpointed graph state is overwritten
    # (otherwise a stale intent from a previous turn persists in the checkpoint).
    from includes.prompts import get_intent_context
    intent_context = None
    if message.command:
        intent_name = _command_to_intent_name(message.command) or message.command
        intent_context = get_intent_context(intent_name)
    if not intent_context:
        intent_context = getattr(message, "intent_context", None)
    if not intent_context:
        intent_context = cl.user_session.get("intent_context")
    # Eagle Agent profile defaults to supplier lookup behavior
    if not intent_context and cl.user_session.get("chat_profile") == "Eagle Agent":
        intent_context = get_intent_context("find_supplier")
    inputs["intent_context"] = intent_context or ""
    
    if file_metadata:
        inputs["file_attachments"] = file_metadata
    
    # Invoke the graph and stream the response
    msg = cl.Message(content="")
    await msg.send()
    
    import time
    request_start = time.monotonic()
    active_agent = "GeneralAgent"
    supervisor_done_at = None
    
    # Accumulate token usage across all model calls for a single footer
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_all_tokens = 0
    
    # Track active cl.Step for tool progress display
    active_step = None
    # Collapse repeated tool calls into a single step with a counter
    last_tool_name = None
    tool_call_count = 0
    
    # Fallback: capture last AI response text for non-streaming model calls
    last_ai_text = ""
    
    active_graph = cl.user_session.get("active_graph", graph)
    last_event_time = request_start
    try:
      async for event in active_graph.astream_events(inputs, config=graph_config, version="v1"):
        kind = event["event"]
        name = event.get("name", "")
        tags = event.get("tags", [])
        
        # Log significant graph lifecycle events to trace checkpoint overhead
        if kind in ("on_chain_start", "on_chain_end", "on_tool_start", "on_tool_end", "on_chat_model_start", "on_chat_model_end"):
            now = time.monotonic()
            gap = now - last_event_time
            if gap > 0.5:  # Only log gaps > 500ms to reduce noise
                logger.info(f"[TIMING] {kind} '{name}' at T+{now - request_start:.1f}s (gap: {gap:.1f}s)")
            last_event_time = now
        
        # Log tool invocations to trace ReAct agent loop behavior
        if kind == "on_tool_start":
            tool_input = event.get("data", {}).get("input", "")
            logger.info(f"[TOOL] calling '{name}' with: {str(tool_input)[:200]}")
        
        if kind == "on_chain_start" and name in ["GeneralAgent", "ProcurementAgent", "SysAdminAgent", "ResearchAgent"]:
            active_agent = name
            if supervisor_done_at is None:
                supervisor_done_at = time.monotonic()
                routing_time = supervisor_done_at - request_start
                logger.info(f"Supervisor routing took {routing_time:.1f}s → {name}")
            
        # Skip streaming internal routing decisions
        if "supervisor_routing" in tags:
            continue
            
        if kind == "on_chat_model_stream":
            # Tool sequence is over — reset tracking
            if active_step:
                active_step = None
                last_tool_name = None
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

        elif kind == "on_tool_start":
            # Collapse consecutive calls to the same tool into one step
            friendly = name.replace("_", " ").title()
            if name == last_tool_name and active_step:
                # Same tool again — increment counter and update label
                tool_call_count += 1
                active_step.name = f"{friendly} (x{tool_call_count})"
                await active_step.update()
            else:
                # Different tool — start a new step
                last_tool_name = name
                tool_call_count = 1
                active_step = cl.Step(name=friendly, type="tool")
                await active_step.send()

        elif kind == "on_tool_end":
            # Close the progress step (only if next event is a different tool)
            if active_step:
                data = event.get("data", {})
                output = data.get("output")
                output_str = ""
                if isinstance(output, str):
                    output_str = output
                elif hasattr(output, "content"):
                    output_str = str(output.content)
                elif hasattr(output, "get") and "output" in output:
                    output_str = str(output["output"])
                else:
                    output_str = str(output)
                active_step.output = output_str[:2000] if len(output_str) > 2000 else output_str
                await active_step.update()
                # Don't clear active_step yet — next on_tool_start
                # will reuse it if it's the same tool

        elif kind == "on_chat_model_end":
            # Accumulate token usage — footer is emitted once after the stream
            output = event.get("data", {}).get("output")

            # Capture text content as fallback for non-streaming model calls.
            # When _should_stream() returns False (e.g. callbacks don't propagate
            # the streaming handler into sub-graphs), on_chat_model_stream events
            # never fire.  Grab the text from the final model output so we can
            # display it after the event loop if nothing was streamed.
            _ai_text = ""
            if hasattr(output, "content"):
                _c = output.content
                if isinstance(_c, str):
                    _ai_text = _c
                elif isinstance(_c, list):
                    _ai_text = "".join(
                        p.get("text", "") if isinstance(p, dict) and p.get("type") == "text"
                        else p if isinstance(p, str) else ""
                        for p in _c
                    )
            # Also try ChatResult / LLMResult format (generations list)
            if not _ai_text and hasattr(output, "generations"):
                for gen_list in output.generations:
                    for gen in (gen_list if isinstance(gen_list, list) else [gen_list]):
                        gen_msg = getattr(gen, "message", None)
                        if gen_msg and hasattr(gen_msg, "content"):
                            gc = gen_msg.content
                            if isinstance(gc, str) and gc.strip():
                                _ai_text = gc
                            elif isinstance(gc, list):
                                _ai_text = "".join(
                                    p.get("text", "") if isinstance(p, dict) and p.get("type") == "text"
                                    else p if isinstance(p, str) else ""
                                    for p in gc
                                )
                        if _ai_text:
                            break
                    if _ai_text:
                        break
            if _ai_text:
                last_ai_text = _ai_text

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
                total_prompt_tokens += usage.get("input_tokens", usage.get("prompt_tokens", 0))
                total_completion_tokens += usage.get("output_tokens", usage.get("completion_tokens", 0))
                total_all_tokens += usage.get("total_tokens", 0)
                current_total = cl.user_session.get("total_tokens_used", 0)
                cl.user_session.set("total_tokens_used", current_total + usage.get("total_tokens", 0))
    except Exception as e:
        logger.error(f"Graph execution error: {e}", exc_info=True)
        error_text = str(e)
        if any(code in error_text for code in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED")):
            await msg.stream_token("\n\nSorry, the AI model is temporarily overloaded. Please try again in a moment.")
        else:
            await msg.stream_token("\n\nSorry, an unexpected error occurred. Please try again.")

    # Fallback: if no text was streamed but the model DID produce a response,
    # display it now.  This covers cases where on_chat_model_stream events
    # weren't emitted (e.g. non-streaming model call path).
    if not msg.content.strip() and last_ai_text.strip():
        logger.warning("No streaming output captured — using fallback response text")
        await msg.stream_token(last_ai_text)

    # Second fallback: if still empty, read the last message from checkpointed state
    if not msg.content.strip():
        try:
            final_state = await active_graph.aget_state(graph_config)
            if final_state and final_state.values.get("messages"):
                last_msg = final_state.values["messages"][-1]
                if hasattr(last_msg, "content") and last_msg.content:
                    _fc = last_msg.content
                    _fallback2 = ""
                    if isinstance(_fc, str):
                        _fallback2 = _fc
                    elif isinstance(_fc, list):
                        _fallback2 = "".join(
                            p.get("text", "") if isinstance(p, dict) and p.get("type") == "text"
                            else p if isinstance(p, str) else ""
                            for p in _fc
                        )
                    if _fallback2.strip():
                        logger.warning(f"No streaming output — using fallback from graph state (last msg type={type(last_msg).__name__})")
                        await msg.stream_token(_fallback2)
        except Exception as fb_err:
            logger.debug(f"State fallback failed: {fb_err}")

    # Emit a single token-usage footer after the full response
    if total_all_tokens > 0:
        total_elapsed = time.monotonic() - request_start
        routing_part = ""
        if supervisor_done_at is not None:
            routing_s = supervisor_done_at - request_start
            routing_part = f" | Routing: {routing_s:.1f}s"
        token_info = f"\n\n<div style='margin-top:20px; font-size:0.8em; color:#a1a1aa; font-style:italic;'>Agent: {active_agent} | Tokens: {total_all_tokens:,} (Context: {total_prompt_tokens:,}, Generated: {total_completion_tokens:,}){routing_part} | Total: {total_elapsed:.1f}s</div>\n\n"
        await msg.stream_token(token_info)

    await msg.update()

    # Clear single-use intent so the next message isn't influenced by the old button
    cl.user_session.set("intent_context", None)
