"""
Action registry and dispatcher for EagleAgent.

Replaces slash commands with action buttons. Each action has metadata
(name, label, description, icon, admin_only) and maps to an async handler
function. The dispatcher checks the user's role before executing admin-only
actions.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Awaitable, Optional

from config import config
from includes.chat.context import ActionSpec, ChatContext, get_chat_context
from includes.prompts import INTENTS, RESEARCH_INTENTS

logger = logging.getLogger(__name__)


@dataclass
class Action:
    """Metadata for a registered action."""
    name: str
    label: str
    description: str
    icon: str
    admin_only: bool
    handler: Callable[..., Awaitable[None]]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_registry: dict[str, Action] = {}


def register_action(
    name: str,
    label: str,
    description: str,
    icon: str = "",
    admin_only: bool = False,
) -> Callable:
    """Decorator to register an action handler."""

    def decorator(fn: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
        _registry[name] = Action(
            name=name,
            label=label,
            description=description,
            icon=icon,
            admin_only=admin_only,
            handler=fn,
        )
        return fn

    return decorator


def get_actions_for_user(user_id: str) -> list[Action]:
    """Return actions visible to the given user (filters admin-only for non-admins)."""
    is_admin = user_id.lower() in config.get_admin_emails() if user_id else False
    return [
        a for a in _registry.values()
        if not a.admin_only or is_admin
    ]


def get_action(name: str) -> Optional[Action]:
    """Look up an action by name."""
    return _registry.get(name)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

async def dispatch_action(
    action_name: str,
    ctx: ChatContext | None = None,
    **kwargs: Any,
) -> None:
    """Dispatch an action by name after checking role permissions.

    Raises ValueError if the action is unknown.
    Sends a permission-denied message if the user lacks access.
    """
    action = get_action(action_name)
    if action is None:
        raise ValueError(f"Unknown action: {action_name}")

    ctx = ctx or get_chat_context()

    if action.admin_only:
        user_id = ctx.user_email
        is_admin = user_id.lower() in config.get_admin_emails() if user_id else False
        if not is_admin:
            await ctx.say(
                "⛔ You do not have permission to perform this action.",
                author="EagleAgent",
            )
            return

    await action.handler(ctx, **kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Phrases that trigger showing action buttons mid-conversation
_HELP_PHRASES = {
    "help", "show actions", "actions", "commands", "menu",
    "what can i do",
}


def is_help_request(text: str) -> bool:
    """Return True if *text* looks like a request to see available actions."""
    return text.strip().lower().rstrip("?!.") in _HELP_PHRASES


async def send_action_buttons(user_id: str, ctx: ChatContext | None = None) -> None:
    """Send a message listing available actions with clickable buttons."""
    ctx = ctx or get_chat_context()
    visible = get_actions_for_user(user_id)
    if not visible:
        await ctx.say("No actions available.", author="EagleAgent")
        return

    lines = ["Here are the actions you can use:\n"]
    buttons = []
    for a in visible:
        label_suffix = " *(admin)*" if a.admin_only else ""
        lines.append(f"- **{a.label}**{label_suffix} — {a.description}")
        buttons.append(
            ActionSpec(name=a.name, payload={}, label=a.label, tooltip=a.description)
        )

    await ctx.say("\n".join(lines), author="EagleAgent", actions=buttons)


# ---------------------------------------------------------------------------
# Built-in action handlers
# ---------------------------------------------------------------------------

@register_action(
    name="new_conversation",
    label="New Conversation",
    description="Start a fresh conversation thread",
    icon="refresh",
    admin_only=False,
)
async def handle_new_conversation(ctx: ChatContext, **_kwargs: Any) -> None:
    """Start a new conversation thread."""
    new_thread = str(uuid.uuid4())
    ctx.set("thread_id", new_thread)
    await ctx.say(
        "🔄 Conversation context has been reset — I won't remember anything "
        "from earlier in this thread.\n\n"
        "To start with a clean chat window, click **New Chat** in the sidebar "
        "or refresh your browser.",
        author="EagleAgent",
    )


# ---------------------------------------------------------------------------
# Procurement intent action handlers
# ---------------------------------------------------------------------------

async def _handle_intent(intent_name: str, ctx: ChatContext) -> None:
    """Common handler for intent buttons (procurement and research)."""
    intent = INTENTS.get(intent_name) or RESEARCH_INTENTS.get(intent_name)
    if not intent:
        return
    ctx.set("intent_context", intent["context"])
    await ctx.say(f"{intent['icon']} {intent['follow_up']}", author="EagleAgent")


@register_action(
    name="find_product",
    label=INTENTS["find_product"]["label"],
    description=INTENTS["find_product"]["description"],
    icon=INTENTS["find_product"]["icon"],
    admin_only=False,
)
async def handle_find_product(ctx: ChatContext, **_kwargs: Any) -> None:
    await _handle_intent("find_product", ctx)


@register_action(
    name="find_supplier",
    label=INTENTS["find_supplier"]["label"],
    description=INTENTS["find_supplier"]["description"],
    icon=INTENTS["find_supplier"]["icon"],
    admin_only=False,
)
async def handle_find_supplier(ctx: ChatContext, **_kwargs: Any) -> None:
    await _handle_intent("find_supplier", ctx)


@register_action(
    name="check_purchase_history",
    label=INTENTS["check_purchase_history"]["label"],
    description=INTENTS["check_purchase_history"]["description"],
    icon=INTENTS["check_purchase_history"]["icon"],
    admin_only=False,
)
async def handle_check_purchase_history(ctx: ChatContext, **_kwargs: Any) -> None:
    await _handle_intent("check_purchase_history", ctx)


# ---------------------------------------------------------------------------
# Research intent action handlers
# ---------------------------------------------------------------------------

@register_action(
    name="research_product_info",
    label=RESEARCH_INTENTS["research_product_info"]["label"],
    description=RESEARCH_INTENTS["research_product_info"]["description"],
    icon=RESEARCH_INTENTS["research_product_info"]["icon"],
    admin_only=True,
)
async def handle_research_product_info(ctx: ChatContext, **_kwargs: Any) -> None:
    await _handle_intent("research_product_info", ctx)


@register_action(
    name="research_supply_chain",
    label=RESEARCH_INTENTS["research_supply_chain"]["label"],
    description=RESEARCH_INTENTS["research_supply_chain"]["description"],
    icon=RESEARCH_INTENTS["research_supply_chain"]["icon"],
    admin_only=True,
)
async def handle_research_supply_chain(ctx: ChatContext, **_kwargs: Any) -> None:
    await _handle_intent("research_supply_chain", ctx)
