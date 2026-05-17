"""
Centralized prompt templates and agent configuration for EagleAgent.

This package re-exports every public name so that existing
``from includes.prompts import X`` statements continue to work.

Sub-modules
-----------
config   – AGENT_CONFIG, TOOL_INSTRUCTIONS, PROFILE_TEMPLATES
intents  – INTENTS, RESEARCH_INTENTS, get_intent_context()
builder  – build_system_prompt(), build_research_prompt(), build_sysadmin_prompt(), etc.

Skill prompts live as Markdown files in ``config/prompts/``.
Use ``load_prompt(name)`` to load them by stem name.
"""

from pathlib import Path
from functools import lru_cache

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "config" / "prompts"


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """Load a skill prompt from config/prompts/<name>.md.

    Args:
        name: Stem name without extension, e.g. 'rfq_workflow'.

    Returns:
        The prompt text.

    Raises:
        FileNotFoundError: If the prompt file does not exist.
    """
    path = _PROMPTS_DIR / f"{name}.md"
    return path.read_text()

# -- data -------------------------------------------------------------------
from includes.prompts.config import (  # noqa: F401
    AGENT_CONFIG,
    PROFILE_TEMPLATES,
    TOOL_INSTRUCTIONS,
)
from includes.prompts.intents import (  # noqa: F401
    INTENTS,
    RESEARCH_INTENTS,
    get_intent_context,
)

# -- functions --------------------------------------------------------------
from includes.prompts.builder import (  # noqa: F401
    _build_action_awareness,
    _build_admin_profile_hint,
    _build_script_awareness,
    build_profile_context,
    build_research_prompt,
    build_sysadmin_prompt,
    build_system_prompt,
    format_profile_section,
    get_agent_identity_prompt,
    validate_config,
)
