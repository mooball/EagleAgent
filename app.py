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
from includes.chat.actions import dispatch_action, get_actions_for_user, is_help_request, send_action_buttons
from includes.chat.context import bind_chat_context
from includes.chat.context_chainlit import ChainlitChatContext
from includes.chat.document_processing import process_file
from includes.chat.local_storage_client import LocalStorageClient
from includes.chat.data_layer import FixedSQLAlchemyDataLayer
from includes.chat.middleware import OAuthErrorRedirectMiddleware, GeminiRetryNotifier
from includes.chat.runner import RunInProgress, run_turn
from includes.chat.streaming_logic import (
    extract_ai_text as _extract_ai_text,
    plan_resume_backfill,
)
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

# Suppress noisy schema warnings from Gemini function utils (harmless — unsupported JSON Schema keys)
logging.getLogger("langchain_google_genai._function_utils").setLevel(logging.ERROR)
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

# Suppress "Gemini produced an empty response" warnings — these flood the logs
# when LangGraph's ReAct loop retries after receiving empty model outputs.
# The recursion limit will safely stop the loop; no need to log every iteration.
logging.getLogger("langchain_google_genai.chat_models").setLevel(logging.ERROR)

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


# ---------------------------------------------------------------------------
# Helper: extract plain text from an AIMessage's content field
# ---------------------------------------------------------------------------
# AIMessage.content can be a plain string OR a list of content parts (e.g.
# [{"type": "text", "text": "..."}, {"type": "tool_use", ...}]). Normalising
# both forms lives in includes/chat/streaming_logic.py and is imported above
# as _extract_ai_text. Used by the checkpoint-to-UI reconciliation and the
# streaming fallback paths.
# ---------------------------------------------------------------------------


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
    
    # FIX: Ensure session.user is a PersistedUser so that flush_thread_queues
    # (called by the emitter on first message) correctly sets userId on the
    # thread. The upstream Chainlit auth flow can sometimes leave a plain User
    # on the session if get_user/create_user fails during WebSocket auth.
    from chainlit.user import PersistedUser
    from chainlit.data import get_data_layer
    if user and not isinstance(user, PersistedUser):
        try:
            data_layer = get_data_layer()
            if data_layer:
                persisted = await data_layer.get_user(user.identifier)
                if not persisted:
                    persisted = await data_layer.create_user(user)
                if persisted:
                    cl.context.session.user = persisted
                    user = persisted
        except Exception as e:
            logger.warning(f"Failed to upgrade session user to PersistedUser: {e}")
    
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
        # Also send the session_id so the parent can reclaim the cookie on tab focus
        await cl.send_window_message({"type": "session_id", "sessionId": cl.context.session.id})
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

    # FIX: Ensure session.user is a PersistedUser (same fix as on_chat_start)
    from chainlit.user import PersistedUser
    from chainlit.data import get_data_layer
    if user and not isinstance(user, PersistedUser):
        try:
            data_layer = get_data_layer()
            if data_layer:
                persisted = await data_layer.get_user(user.identifier)
                if not persisted:
                    persisted = await data_layer.create_user(user)
                if persisted:
                    cl.context.session.user = persisted
                    user = persisted
        except Exception as e:
            logger.warning(f"Failed to upgrade session user to PersistedUser: {e}")

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

    # ---------------------------------------------------------------------------
    # Checkpoint-to-UI reconciliation
    # ---------------------------------------------------------------------------
    # LangGraph and Chainlit maintain SEPARATE message stores:
    #   - LangGraph checkpoint: the authoritative graph state (HumanMessage,
    #     AIMessage, ToolMessage). Persisted to PostgreSQL by AsyncPostgresSaver
    #     after every node execution. This is what the LLM sees as context.
    #   - Chainlit steps: the UI thread history. Persisted via the data layer
    #     when cl.Message.send()/update() succeeds.
    #
    # If the user navigates away mid-execution, the graph continues running and
    # checkpoints correctly, but Chainlit's msg.update() may fail (dead socket),
    # leaving the UI thread missing messages. The user returns and sees gaps.
    #
    # FIX: On resume, compare the LangGraph checkpoint against Chainlit's stored
    # steps. Any AI responses in the checkpoint that aren't in the steps get
    # back-filled into the data layer — so they appear in the UI immediately.
    #
    # We identify "missing" messages by comparing the count of AIMessage entries
    # in the checkpoint against assistant_message steps in the thread. If the
    # checkpoint has more, we take the newest N (the gap) and persist them.
    # ---------------------------------------------------------------------------
    try:
        active_graph = cl.user_session.get("active_graph", _graph())
        graph_config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": config.GRAPH_RECURSION_LIMIT,
        }
        checkpoint_state = await active_graph.aget_state(graph_config)

        if checkpoint_state and checkpoint_state.values.get("messages"):
            from chainlit.data import get_data_layer as _get_dl_resume

            missing = plan_resume_backfill(
                checkpoint_state.values["messages"],
                thread.get("steps", []),
            )

            if missing:
                logger.info(
                    f"[checkpoint-reconcile] Thread {thread_id[:8]}... has {len(missing)} "
                    f"AI response(s) in checkpoint not in UI — back-filling"
                )
                data_layer = _get_dl_resume()
                if data_layer:
                    import uuid as _uuid
                    from datetime import datetime as _dt, timezone as _tz

                    for ai_msg in missing:
                        text = _extract_ai_text(ai_msg)
                        if not text:
                            continue
                        _now = _dt.now(_tz.utc).isoformat()
                        step_dict = {
                            "id": str(_uuid.uuid4()),
                            "threadId": thread_id,
                            "name": "EagleAgent",
                            "type": "assistant_message",
                            "output": text,
                            "createdAt": _now,
                            "start": _now,
                            "end": _now,
                            "streaming": False,
                            "metadata": {"recovered_from_checkpoint": True},
                            "tags": None,
                            "input": "",
                            "isError": False,
                            "parentId": None,
                            "language": None,
                            "showInput": None,
                            "generation": None,
                            "defaultOpen": None,
                            "autoCollapse": None,
                        }
                        await data_layer.create_step(step_dict)
                    logger.info(f"[checkpoint-reconcile] Back-filled {len(missing)} message(s)")
    except Exception as reconcile_err:
        # Reconciliation is best-effort — never block thread resume
        logger.warning(f"[checkpoint-reconcile] Failed: {reconcile_err}")

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
        await cl.send_window_message({"type": "session_id", "sessionId": cl.context.session.id})
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


@cl.action_callback("stop_agent")
async def on_action_stop_agent(action: cl.Action):
    """Stop all running agent tasks for the current session.

    This is dispatched by the blue badge click via /api/stop-agent (which
    bypasses the bridge lock), but also registered as a Chainlit action
    callback for fallback use.
    """
    from includes.agent_bridge import request_stop
    session_id = cl.context.session.id
    cancelled = await request_stop(session_id)
    if cancelled:
        await cl.Message(
            content=f"⏹ Stopped {cancelled} running task(s).",
            author="EagleAgent",
        ).send()
    else:
        await cl.Message(
            content="⏹ Stop requested — finishing current operation...",
            author="EagleAgent",
        ).send()
    await notify_dashboard("agent_done")


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
    user_id = cl.user_session.get("user_id", "")

    # Bind the ChatContext for this turn. Chainlit runs each on_message in its
    # own task, so the ContextVar is scoped to this turn without needing a reset.
    ctx = ChainlitChatContext.from_session()
    bind_chat_context(ctx)

    # Show action buttons when the user asks for help / actions
    if is_help_request(message.content):
        await send_action_buttons(user_id)
        return

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

    # Resolve the single-use intent for this turn
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

    from includes.dashboard.context import format_context_for_prompt
    dashboard_ctx = format_context_for_prompt(user_id)

    active_graph = cl.user_session.get("active_graph", _graph())

    # Register this task for cancellation via stop-agent
    from includes.agent_bridge import register_task, unregister_task, clear_stop
    session_id = cl.context.session.id
    current_task = asyncio.current_task()
    if current_task:
        clear_stop(session_id)  # Reset any stale cancel flag from previous run
        register_task(current_task, session_id)

    try:
        await run_turn(
            message.content,
            ctx,
            graph=active_graph,
            files=processed_files,
            file_metadata=file_metadata,
            intent_context=intent_context or "",
            dashboard_context=dashboard_ctx,
            on_busy="reject",
        )
    except RunInProgress:
        await cl.Message(
            content="Still working on the previous message — one moment.",
            author="EagleAgent",
        ).send()
    finally:
        if current_task:
            unregister_task(current_task, session_id)
