"""The agent registry is the only place an agent is defined."""

import pytest

from includes.agents.registry import (
    AGENTS,
    LEGACY_NAMES,
    AgentSpec,
    default_agent,
    resolve,
)


def test_three_agents():
    assert set(AGENTS) == {"eagle", "research", "internal"}


def test_keys_match_their_dict_entry():
    for key, spec in AGENTS.items():
        assert spec.key == key


def test_exactly_one_default():
    defaults = [s for s in AGENTS.values() if s.is_default]
    assert len(defaults) == 1
    assert default_agent().key == "eagle"


def test_labels_are_unique():
    labels = [s.label for s in AGENTS.values()]
    assert len(set(labels)) == len(labels)


def test_graph_attrs_are_unique_and_named_as_expected():
    attrs = {s.key: s.graph_attr for s in AGENTS.values()}
    assert attrs == {
        "eagle": "graph",
        "research": "research_graph",
        "internal": "internal_graph",
    }
    assert len(set(attrs.values())) == 3


def test_only_eagle_allows_rfq_binding():
    binding = {s.key for s in AGENTS.values() if s.allows_rfq_binding}
    assert binding == {"eagle"}


# --- resolve --------------------------------------------------------------

@pytest.mark.parametrize("key", ["eagle", "research", "internal"])
def test_resolve_by_key(key):
    assert resolve(key).key == key


@pytest.mark.parametrize(
    "label,expected",
    [
        ("Eagle Agent", "eagle"),
        ("Research Agent", "research"),
        ("Internal Agent", "internal"),
    ],
)
def test_resolve_by_display_label(label, expected):
    assert resolve(label).key == expected


@pytest.mark.parametrize("legacy", ["EagleAgent", "System Admin"])
def test_legacy_names_resolve_to_eagle(legacy):
    """Threads persisted under the old profile names must still resume."""
    assert resolve(legacy).key == "eagle"


def test_every_legacy_name_points_at_a_real_agent():
    assert set(LEGACY_NAMES.values()) <= set(AGENTS)


@pytest.mark.parametrize("name", [None, "", "Something Else"])
def test_unknown_falls_back_to_the_default(name):
    """Matches the previous `else: graph()` branches."""
    assert resolve(name).key == "eagle"


# --- graph + intents ------------------------------------------------------

def test_graph_reads_the_module_attribute_late(monkeypatch):
    """The graph globals mutate after setup_globals(), so binding must be late."""
    import includes.graph as graph_module

    monkeypatch.setattr(graph_module, "research_graph", "the-research-graph", raising=False)
    assert AGENTS["research"].graph() == "the-research-graph"

    monkeypatch.setattr(graph_module, "research_graph", "replaced", raising=False)
    assert AGENTS["research"].graph() == "replaced"


def test_eagle_has_no_composer_commands():
    """RFQ creation is dashboard-only, so Eagle deliberately shows none."""
    assert AGENTS["eagle"].intents == []
    assert AGENTS["eagle"].command_intents() == {}


@pytest.mark.parametrize("key", ["research", "internal"])
def test_command_intents_resolve_against_the_prompt_definitions(key):
    resolved = AGENTS[key].command_intents()
    assert set(resolved) == set(AGENTS[key].intents), "an intent name does not exist"
    for intent in resolved.values():
        assert "label" in intent and "context" in intent


def test_specs_are_immutable():
    with pytest.raises(Exception):
        AGENTS["eagle"].label = "Nope"


def test_agent_spec_is_hashable_and_comparable():
    a = AgentSpec(key="x", label="X", description="", icon="", graph_attr="graph")
    b = AgentSpec(key="x", label="X", description="", icon="", graph_attr="graph")
    assert a == b
