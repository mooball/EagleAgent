"""Every action button the code emits must have a handler.

`confirm_run_script` shipped for months with no handler at all: `job_tools.py`
built the button, nothing dispatched it, so the Run button on the run_script
confirmation silently did nothing (todo.vu #32818). This finds that shape of
bug by construction rather than by someone noticing.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNED_DIRS = ("includes", "app.py")

# Known-broken buttons, deliberately not fixed during the chat migration.
# Remove an entry when its ticket lands — test_known_orphans_do_not_rot will
# tell you if you forget.
KNOWN_ORPHANS = {
    "confirm_run_script": "todo.vu #32818 — Run button on the run_script confirmation",
}


def _all_handled_names() -> set[str]:
    """Every action name something is prepared to dispatch."""
    from includes.chat.actions import _registry
    from includes.chat.rfq_actions import RFQ_ACTIONS

    names = set(RFQ_ACTIONS) | set(_registry)

    # Lifecycle actions still registered directly in app.py.
    import app  # noqa: F401 — importing runs the registration
    from chainlit.config import config as cl_config

    names |= set(cl_config.code.action_callbacks)
    return names


def _emitted_action_names() -> dict[str, list[str]]:
    """Action names passed as `name=...` to ActionSpec(...) or cl.Action(...).

    Only literal strings — a computed name is out of scope for a static check.
    """
    emitted: dict[str, list[str]] = {}

    paths = [REPO_ROOT / "app.py"]
    paths += [
        p for p in (REPO_ROOT / "includes").rglob("*.py")
        if "__pycache__" not in p.parts
    ]

    for path in paths:
        if not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            label = getattr(func, "id", None) or getattr(func, "attr", None)
            if label not in ("ActionSpec", "Action"):
                continue
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str):
                        rel = path.relative_to(REPO_ROOT).as_posix()
                        emitted.setdefault(kw.value.value, []).append(
                            f"{rel}:{node.lineno}"
                        )
    return emitted


def test_the_scanner_finds_the_known_buttons():
    """Guard against the scan silently matching nothing."""
    emitted = _emitted_action_names()
    assert "rfq_dismiss" in emitted
    assert "cancel_job" in emitted
    assert "rfq_pipeline_previous_suppliers" in emitted


def test_every_emitted_button_has_a_handler():
    emitted = _emitted_action_names()
    handled = _all_handled_names()

    orphans = {
        name: sites
        for name, sites in emitted.items()
        if name not in handled and name not in KNOWN_ORPHANS
    }
    assert not orphans, (
        "action buttons are emitted but nothing dispatches them:\n"
        + "\n".join(f"  {n} \u2014 emitted at {', '.join(s)}" for n, s in sorted(orphans.items()))
    )


def test_known_orphans_do_not_rot():
    """When a known orphan gets a handler, take it off the list."""
    handled = _all_handled_names()
    fixed = {name for name in KNOWN_ORPHANS if name in handled}
    assert not fixed, (
        "these now have handlers — remove them from KNOWN_ORPHANS: " + ", ".join(sorted(fixed))
    )


def test_known_orphans_are_still_emitted():
    """And if the button itself is gone, take it off the list too."""
    emitted = _emitted_action_names()
    stale = {name for name in KNOWN_ORPHANS if name not in emitted}
    assert not stale, (
        "these are no longer emitted — remove them from KNOWN_ORPHANS: " + ", ".join(sorted(stale))
    )


@pytest.mark.parametrize("name", ["rfq_refresh", "rfq_find_all_suppliers"])
def test_known_names_resolve(name):
    assert name in _all_handled_names()
