"""
Action registry and dispatcher for EagleAgent.

Replaces slash commands with Chainlit-native action buttons.
Each action has metadata (name, label, description, icon, admin_only)
and maps to an async handler function. The dispatcher checks the
user's role before executing admin-only actions.
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Optional

import chainlit as cl

from config import config
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
# Dispatcher (called from @cl.action_callback)
# ---------------------------------------------------------------------------

async def dispatch_action(action_name: str, **kwargs: Any) -> None:
    """Dispatch an action by name after checking role permissions.

    Raises ValueError if the action is unknown.
    Sends a permission-denied message if the user lacks access.
    """
    action = get_action(action_name)
    if action is None:
        raise ValueError(f"Unknown action: {action_name}")

    user_id: str = cl.user_session.get("user_id", "")

    if action.admin_only:
        is_admin = user_id.lower() in config.get_admin_emails() if user_id else False
        if not is_admin:
            await cl.Message(
                content="⛔ You do not have permission to perform this action.",
                author="EagleAgent",
            ).send()
            return

    await action.handler(**kwargs)


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


async def send_action_buttons(user_id: str) -> None:
    """Send a message listing available actions with clickable buttons."""
    visible = get_actions_for_user(user_id)
    if not visible:
        await cl.Message(content="No actions available.", author="EagleAgent").send()
        return

    lines = ["Here are the actions you can use:\n"]
    buttons = []
    for a in visible:
        label_suffix = " *(admin)*" if a.admin_only else ""
        lines.append(f"- **{a.label}**{label_suffix} — {a.description}")
        buttons.append(
            cl.Action(name=a.name, payload={}, label=a.label, description=a.description)
        )

    await cl.Message(
        content="\n".join(lines),
        author="EagleAgent",
        actions=buttons,
    ).send()


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
async def handle_new_conversation(**_kwargs: Any) -> None:
    """Start a new conversation thread."""
    new_thread = str(uuid.uuid4())
    cl.user_session.set("thread_id", new_thread)
    await cl.Message(
        content=(
            "🔄 Conversation context has been reset — I won't remember anything "
            "from earlier in this thread.\n\n"
            "To start with a clean chat window, click **New Chat** in the sidebar "
            "or refresh your browser."
        ),
        author="EagleAgent",
    ).send()


@register_action(
    name="delete_all_data",
    label="Delete All My Data",
    description="Permanently erase all your data (admin only)",
    icon="trash",
    admin_only=True,
)
async def handle_delete_all_data(**_kwargs: Any) -> None:
    """Send a confirmation prompt with Yes / Cancel action buttons."""
    actions = [
        cl.Action(
            name="confirm_delete_all",
            payload={"confirm": True},
            label="Yes, delete everything",
        ),
        cl.Action(
            name="cancel_delete_all",
            payload={"confirm": False},
            label="Cancel",
        ),
    ]
    await cl.Message(
        content=(
            "⚠️ **Warning:** This will permanently delete all preferences, "
            "settings, and memories associated with your profile, and start "
            "a new blank conversation.\n\n"
            "**Do you really want me to delete all your data?**"
        ),
        author="EagleAgent",
        actions=actions,
    ).send()


# ---------------------------------------------------------------------------
# Procurement intent action handlers
# ---------------------------------------------------------------------------

async def _handle_intent(intent_name: str) -> None:
    """Common handler for intent buttons (procurement and research)."""
    intent = INTENTS.get(intent_name) or RESEARCH_INTENTS.get(intent_name)
    if not intent:
        return
    cl.user_session.set("intent_context", intent["context"])
    await cl.Message(
        content=f"{intent['icon']} {intent['follow_up']}",
        author="EagleAgent",
    ).send()


@register_action(
    name="find_product",
    label=INTENTS["find_product"]["label"],
    description=INTENTS["find_product"]["description"],
    icon=INTENTS["find_product"]["icon"],
    admin_only=False,
)
async def handle_find_product(**_kwargs: Any) -> None:
    await _handle_intent("find_product")


@register_action(
    name="find_supplier",
    label=INTENTS["find_supplier"]["label"],
    description=INTENTS["find_supplier"]["description"],
    icon=INTENTS["find_supplier"]["icon"],
    admin_only=False,
)
async def handle_find_supplier(**_kwargs: Any) -> None:
    await _handle_intent("find_supplier")


@register_action(
    name="check_purchase_history",
    label=INTENTS["check_purchase_history"]["label"],
    description=INTENTS["check_purchase_history"]["description"],
    icon=INTENTS["check_purchase_history"]["icon"],
    admin_only=False,
)
async def handle_check_purchase_history(**_kwargs: Any) -> None:
    await _handle_intent("check_purchase_history")


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
async def handle_research_product_info(**_kwargs: Any) -> None:
    await _handle_intent("research_product_info")


@register_action(
    name="research_supply_chain",
    label=RESEARCH_INTENTS["research_supply_chain"]["label"],
    description=RESEARCH_INTENTS["research_supply_chain"]["description"],
    icon=RESEARCH_INTENTS["research_supply_chain"]["icon"],
    admin_only=True,
)
async def handle_research_supply_chain(**_kwargs: Any) -> None:
    await _handle_intent("research_supply_chain")
