"""The three agents, defined once.

Before this, an agent's identity was spread across `@cl.set_chat_profiles`,
two graph-selection if/elif chains in `app.py`, a legacy-name fixup on resume,
and a hardcoded profile string in `embedded.js`. Adding a fourth agent meant
finding all of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["AgentSpec", "AGENTS", "LEGACY_NAMES", "resolve", "default_agent"]


@dataclass(frozen=True)
class AgentSpec:
    key: str
    label: str
    description: str
    icon: str
    graph_attr: str  # attribute on includes.graph holding the compiled graph
    intents: list[str] = field(default_factory=list)
    allows_rfq_binding: bool = False
    admin_only: bool = False
    is_default: bool = False

    def graph(self) -> Any:
        """The compiled graph. Read late — the globals mutate after setup."""
        import includes.graph as graph_module

        return getattr(graph_module, self.graph_attr)

    def command_intents(self) -> dict:
        """The intent definitions backing this agent's composer commands."""
        from includes.prompts import INTENTS, RESEARCH_INTENTS

        source = {**INTENTS, **RESEARCH_INTENTS}
        return {name: source[name] for name in self.intents if name in source}


AGENTS: dict[str, AgentSpec] = {
    "eagle": AgentSpec(
        key="eagle",
        label="Eagle Agent",
        description=(
            "Supplier lookup agent — search our supplier database by name, "
            "brand, or description."
        ),
        icon="/public/avatars/EagleAgent.png",
        graph_attr="graph",
        intents=[],  # RFQ creation is dashboard-only, so no composer commands
        allows_rfq_binding=True,
        is_default=True,
    ),
    "research": AgentSpec(
        key="research",
        label="Research Agent",
        description="Search the web for information and research topics.",
        icon="/public/avatars/EagleAgent.png",
        graph_attr="research_graph",
        intents=["research_product_info", "research_supply_chain"],
    ),
    "internal": AgentSpec(
        key="internal",
        label="Internal Agent",
        description=(
            "Search the internal database for products, suppliers, and "
            "purchase history."
        ),
        icon="/public/avatars/EagleAgent.png",
        graph_attr="internal_graph",
        intents=["find_product", "find_supplier", "check_purchase_history"],
    ),
}

# Profile names that existed before the labels settled. Threads persisted under
# these still resume.
LEGACY_NAMES = {
    "EagleAgent": "eagle",
    "System Admin": "eagle",
}

_BY_LABEL = {spec.label: spec for spec in AGENTS.values()}


def default_agent() -> AgentSpec:
    return next(spec for spec in AGENTS.values() if spec.is_default)


def resolve(name: str | None) -> AgentSpec:
    """Map a key, a display label, or a legacy name onto a spec.

    Unknown names fall back to the default agent, matching the previous
    `else: graph()` branches.
    """
    if not name:
        return default_agent()
    if name in AGENTS:
        return AGENTS[name]
    if name in _BY_LABEL:
        return _BY_LABEL[name]
    if name in LEGACY_NAMES:
        return AGENTS[LEGACY_NAMES[name]]
    return default_agent()
