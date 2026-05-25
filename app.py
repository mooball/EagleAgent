import chainlit as cl
import uuid
import urllib.parse
from chainlit.types import ThreadDict
from datetime import datetime, timezone
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage
from typing import Optional, Any
import os
import logging
from dotenv import load_dotenv
from config import config
from includes.chat.commands import handle_deleteall_command
from includes.chat.actions import dispatch_action, get_actions_for_user, is_help_request, send_action_buttons
from includes.chat.document_processing import process_file, create_multimodal_content
from includes.chat.local_storage_client import LocalStorageClient
from includes.chat.data_layer import FixedSQLAlchemyDataLayer
from includes.chat.middleware import OAuthErrorRedirectMiddleware, GeminiRetryNotifier
from includes.graph import setup_globals
import includes.graph as _graph_module  # for live access to mutable globals
import asyncio

# SQL-based RFQ helpers (BaseStore lock no longer needed — PostgreSQL handles concurrency)
from includes.tools.quote_tools import (
    _clear_suppliers_sync, _get_rfq_dict_sync,
)

# Import RFQ action callbacks so Chainlit registers them
import includes.chat.rfq_actions  # noqa: F401

# Set up Chainlit server reference for middleware patching
import chainlit.server as cl_server

# Tune Engine.IO ping settings for Railway's proxy (which routes WebSocket
# upgrades through different edge nodes, causing intermittent 403s when
# sessions expire during the handoff).  Chainlit doesn't expose these
# settings, so we patch the Socket.IO server's underlying Engine.IO instance.
if hasattr(cl_server, 'sio') and hasattr(cl_server.sio, 'eio'):
    cl_server.sio.eio.ping_interval = 30      # default 25 — interval between pings
    cl_server.sio.eio.ping_timeout = 60        # default 20 — time to wait for pong
    cl_server.sio.eio.ping_interval_grace_period = 5  # default 0
    logging.getLogger(__name__).info(
        "Patched Engine.IO: ping_interval=30, ping_timeout=60, grace=5"
    )

# Create the data directory if it doesn't exist
os.makedirs(os.path.join(config.DATA_DIR, "attachments"), exist_ok=True)

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

# Surface Google API retries to Chainlit UI in real-time
_genai_logger = logging.getLogger("google_genai._api_client")
_genai_logger.addHandler(GeminiRetryNotifier(level=logging.INFO))


# ---------------------------------------------------------------------------
# Convenience accessors for graph module globals (they mutate after setup)
# ---------------------------------------------------------------------------
def _store():
    return _graph_module.store

def _graph():
    return _graph_module.graph

def _research_graph():
    return _graph_module.research_graph

def _internal_graph():
    return _graph_module.internal_graph


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
    user_profile = await _store().aget(("users",), user.identifier)
    is_new_user = False
    
    if not user_profile or not user_profile.value:
        is_new_user = True
        profile_data = {
            "first_name": user.metadata.get("given_name", "") if user.metadata else "",
            "last_name": user.metadata.get("family_name", "") if user.metadata else "",
            "full_name": user.metadata.get("name", "") if user.metadata else "",
            "email": user.metadata.get("email", user.identifier) if user.metadata else user.identifier
        }
        await _store().aput(("users",), user.identifier, profile_data)
        user_profile = await _store().aget(("users",), user.identifier)

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
    """Define available chat profiles."""
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
        cl.ChatProfile(
            name="Internal Agent",
            markdown_description="Search the internal database for products, suppliers, and purchase history.",
            icon="/public/avatars/EagleAgent.png",
        ),
    ]

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
        cl.user_session.set("active_graph", _research_graph())
    elif chat_profile_name == "Internal Agent":
        cl.user_session.set("active_graph", _internal_graph())
    else:
        cl.user_session.set("active_graph", _graph())
    
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
        await cl.context.emitter.set_commands([])
        await cl.Message(content=welcome_msg).send()

    # Notify the parent frame of the Chainlit thread id so it can track it
    try:
        chainlit_thread_id = cl.context.session.thread_id
        await cl.send_window_message({"type": "thread_id", "threadId": chainlit_thread_id})
    except Exception:
        pass

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
        cl.user_session.set("active_graph", _research_graph())
    elif chat_profile_name == "Internal Agent":
        cl.user_session.set("active_graph", _internal_graph())
    else:
        cl.user_session.set("active_graph", _graph())
    
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
            # Eagle Agent — no command buttons (RFQ creation via dashboard only)
            await cl.context.emitter.set_commands([])
            msg = cl.Message(
                content=f"Welcome back, {user_name}! Continuing our previous conversation.",
                author="EagleAgent",
            )
        msg.persisted = True  # skip DB write — display only
        await msg.send()

    # Notify the parent frame of this thread's id so it can track it
    try:
        await cl.send_window_message({"type": "thread_id", "threadId": thread_id})
    except Exception:
        pass

    # If this thread is bound to an RFQ, ensure it's named after the RFQ
    try:
        from includes.dashboard.database import get_session
        from includes.dashboard.models import RFQThread
        user_email = user.identifier if user else None
        if user_email:
            session = get_session()
            try:
                binding = session.query(RFQThread).filter(
                    RFQThread.thread_id == thread_id,
                    RFQThread.user_email == user_email,
                ).first()
                if binding:
                    import asyncio
                    from includes.tools.quote_tools import _get_rfq_dict_sync
                    rfq = await asyncio.to_thread(_get_rfq_dict_sync, binding.rfq_number)
                    customer = rfq.get("customer", "") if rfq else ""
                    thread_name = f"{binding.rfq_number} — {customer}" if customer else binding.rfq_number
                    data_layer = cl.data._data_layer
                    if data_layer:
                        await data_layer.update_thread(thread_id=thread_id, name=thread_name)
            finally:
                session.close()
    except Exception as e:
        logger.warning(f"Failed to name RFQ thread on resume: {e}")


# ---------------------------------------------------------------------------
# Shutdown hook — kill background jobs on app teardown
# ---------------------------------------------------------------------------

@cl.on_stop
async def on_stop():
    """Gracefully shut down the job runner when the app stops."""
    await _graph_module.job_runner.shutdown()


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
        await handle_deleteall_command(user_id, _store(), _graph_module.pg_pool)

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
        job = await _graph_module.job_runner.cancel(job_id)
        await cl.Message(
            content=f"Cancelled job `{job.id[:8]}` ({job.script_name}).",
            author="EagleAgent",
        ).send()
    except ValueError as e:
        await cl.Message(
            content=f"Could not cancel: {e}",
            author="EagleAgent",
        ).send()


# RFQ action callbacks are registered via @cl.action_callback decorators
# in rfq_actions — importing the module is enough.
import includes.chat.rfq_actions  # noqa: F401 — registers Chainlit callbacks


@cl.on_message
async def main(message: cl.Message):
    # Gemini requires at least one content part. If a command button was
    # clicked without text, use the command label as the prompt so the LLM
    # can infer intent from conversation history + command name.
    has_files = bool(message.elements)
    if not message.content or not message.content.strip():
        if message.command:
            message.content = message.command
        elif has_files:
            # User uploaded file(s) without text — let processing continue
            message.content = ""
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

    import re
    msg_lower = message.content.lower()

    # Helper to normalise matched RFQ fragments to full RFQ-YYYY-NNNN format
    def _resolve_rfq_id(match_str: str) -> str:
        digits = re.sub(r'[-\s]', '', match_str)
        if len(digits) >= 8:  # e.g. "20260001"
            return f"RFQ-{digits[:4]}-{digits[4:]}"
        else:  # short form e.g. "0001" — assume current year
            from datetime import datetime
            return f"RFQ-{datetime.now().year}-{digits.zfill(4)}"

    # Direct supplier clearing — intercept "clear/strip/remove suppliers" requests
    _clear_keywords = ["clear", "strip", "remove", "delete", "reset", "wipe"]
    _clear_targets = ["supplier", "suppliers", "quote", "quotes"]
    if any(kw in msg_lower for kw in _clear_keywords) and any(t in msg_lower for t in _clear_targets):
        # Resolve RFQ ID from message or chat history
        clear_rfq_match = re.search(r'\bRFQ[-\s]?(\d{4}[-\s]\d{4,}|\d{4,})\b', message.content, re.IGNORECASE)
        if not clear_rfq_match:
            history = cl.chat_context.to_openai()
            for hist_msg in reversed(history[:-1]):
                content = hist_msg.get("content", "") or ""
                hist_match = re.search(r'\bRFQ[-\s]?(\d{4}[-\s]\d{4,}|\d{4,})\b', content, re.IGNORECASE)
                if hist_match:
                    clear_rfq_match = hist_match
                    break
        if clear_rfq_match:
            from includes.agent_bridge import notify_dashboard
            from datetime import datetime, timezone
            clear_rfq_id = _resolve_rfq_id(clear_rfq_match.group(1))

            # Get the current RFQ to check state
            rfq = await asyncio.to_thread(_get_rfq_dict_sync, clear_rfq_id)
            if rfq:
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

                # Use the SQL clear_suppliers helper
                await asyncio.to_thread(
                    _clear_suppliers_sync, clear_rfq_id,
                    {"line": target_line} if target_line else {},
                    user_id,
                )
                await notify_dashboard("dashboard_refresh")
                scope = f"line {target_line}" if target_line else "all lines"
                await cl.Message(
                    content=f"Cleared suppliers from **{clear_rfq_id}** ({scope}).",
                    author="EagleAgent",
                ).send()
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
    from includes.agent_bridge import notify_dashboard
    await notify_dashboard("agent_working", {"label": "Agent working..."})

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
    # Track all tool names used for the compact footer summary
    tool_names_used = []
    
    # Fallback: capture last AI response text for non-streaming model calls
    last_ai_text = ""
    
    active_graph = cl.user_session.get("active_graph", _graph())
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
            # Tool sequence is over — clean up status indicator
            if active_step:
                await active_step.remove()
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
            # Show a compact, transient status message while tools run
            friendly = name.replace("_", " ").title()
            if friendly not in tool_names_used:
                tool_names_used.append(friendly)
            if name == last_tool_name and active_step:
                # Same tool again — update counter in existing status
                tool_call_count += 1
                active_step.content = f"⏳ Using {friendly} (x{tool_call_count})…"
                await active_step.update()
            else:
                # Different tool — replace the status message
                if active_step:
                    await active_step.remove()
                last_tool_name = name
                tool_call_count = 1
                active_step = cl.Message(
                    content=f"⏳ Using {friendly}…",
                    author="EagleAgent",
                )
                await active_step.send()

        elif kind == "on_tool_end":
            # Keep the status visible until the model starts streaming
            pass

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

    # Clean up any lingering tool status message
    if active_step:
        await active_step.remove()
        active_step = None

    # Emit a single token-usage footer after the full response
    if total_all_tokens > 0:
        total_elapsed = time.monotonic() - request_start
        routing_part = ""
        if supervisor_done_at is not None:
            routing_s = supervisor_done_at - request_start
            routing_part = f" | Routing: {routing_s:.1f}s"
        tools_part = ""
        if tool_names_used:
            tools_part = " | Used " + ", ".join(tool_names_used)
        token_info = f"\n\n<div style='margin-top:20px; font-size:0.8em; color:#a1a1aa; font-style:italic;'>Agent: {active_agent} | Tokens: {total_all_tokens:,} (Context: {total_prompt_tokens:,}, Generated: {total_completion_tokens:,}){routing_part} | Total: {total_elapsed:.1f}s{tools_part}</div>\n\n"
        await msg.stream_token(token_info)

    await msg.update()

    # Clear single-use intent so the next message isn't influenced by the old button
    cl.user_session.set("intent_context", None)

    await notify_dashboard("agent_done")
