"""Chainlit must not leak back out of the adapter layer.

Phase 1 pushed `import chainlit` out of `includes/tools/`, `includes/agents/`
and most of `includes/chat/`. This is the ratchet that keeps it out: without it
the next person to need a message box will reach for `cl.Message` and quietly
undo the phase.

Catches local imports inside functions too — `quote_tools.py` had four of those
before Step 4, and `base.py` and `agent_bridge.py` one each.

Also guards the import direction: `includes/` must never import `app`. That
cycle (`rfq_actions` -> `app.main`) existed before Step 6 and is easy to
recreate by accident.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The adapter layer: the only place allowed to know Chainlit exists.
ALLOWED = {
    "app.py",
    "main.py",  # ASGI entry point — mounts Chainlit at /chat
    "includes/chat/context_chainlit.py",
    "includes/chat/data_layer.py",
    "includes/chat/local_storage_client.py",
    # Track A transcript adapter — reads/writes Chainlit's threads/steps
    # tables via the configured data layer (parent plan §11).
    "includes/chat/transcript.py",
    # Removed from this list in Phase 5, when the bridge is deleted.
    "includes/agent_bridge.py",
}

# Directories that must be completely clean.
SCANNED_DIRS = ("includes", "scripts", "config")


def _python_files():
    for directory in SCANNED_DIRS:
        root = REPO_ROOT / directory
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path
    yield REPO_ROOT / "app.py"
    yield REPO_ROOT / "main.py"


def _imports_module(path: Path, target: str) -> list[int]:
    """Line numbers importing *target*, including inside functions."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
        return []

    prefix = target + "."
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == target or a.name.startswith(prefix) for a in node.names):
                hits.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level == 0 and (mod == target or mod.startswith(prefix)):
                hits.append(node.lineno)
    return hits


def _imports_chainlit(path: Path) -> list[int]:
    return _imports_module(path, "chainlit")


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def test_chainlit_is_confined_to_the_adapter_layer():
    offenders = {}
    for path in _python_files():
        if not path.exists():
            continue
        rel = _rel(path)
        if rel in ALLOWED:
            continue
        lines = _imports_chainlit(path)
        if lines:
            offenders[rel] = lines

    assert not offenders, (
        "chainlit imported outside the adapter layer:\n"
        + "\n".join(f"  {f} (lines {ls})" for f, ls in sorted(offenders.items()))
    )


def test_the_detector_catches_a_local_import():
    """The rule is worthless if it only looks at the top of the file."""
    import tempfile

    src = "def f():\n    import chainlit as cl\n    return cl\n"
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(src)
        tmp = Path(fh.name)
    try:
        assert _imports_chainlit(tmp) == [2]
    finally:
        tmp.unlink()


@pytest.mark.parametrize(
    "src,expected",
    [
        ("import chainlit\n", [1]),
        ("import chainlit.server as s\n", [1]),
        ("from chainlit.types import ThreadDict\n", [1]),
        ("from chainlit import Message\n", [1]),
        ("import chainlit_extras\n", []),
        ("from chainlitish import thing\n", []),
        ("import logging\n", []),
    ],
)
def test_detector_shapes(src, expected, tmp_path):
    path = tmp_path / "sample.py"
    path.write_text(src)
    assert _imports_chainlit(path) == expected


def test_the_allowlist_does_not_rot():
    """Every allowed file must exist, and must actually import chainlit."""
    for rel in sorted(ALLOWED):
        path = REPO_ROOT / rel
        assert path.exists(), f"{rel} is allow-listed but does not exist"
        assert _imports_chainlit(path), (
            f"{rel} no longer imports chainlit — remove it from ALLOWED"
        )


def test_tools_and_agents_are_completely_clean():
    """The two packages Phase 1 set out to free, asserted explicitly."""
    for directory in ("includes/tools", "includes/agents"):
        for path in (REPO_ROOT / directory).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            assert not _imports_chainlit(path), f"{_rel(path)} imports chainlit"


# ---------------------------------------------------------------------------
# Import direction
# ---------------------------------------------------------------------------

def test_includes_never_imports_app():
    """`app.py` is the adapter; it depends on `includes/`, never the reverse.

    `rfq_actions` used to do `from app import main` inside a function, which made
    the two mutually dependent and blocked the whole extraction.
    """
    offenders = {}
    for path in (REPO_ROOT / "includes").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        lines = _imports_module(path, "app")
        if lines:
            offenders[_rel(path)] = lines

    assert not offenders, (
        "includes/ must not import app:\n"
        + "\n".join(f"  {f} (lines {ls})" for f, ls in sorted(offenders.items()))
    )


@pytest.mark.parametrize(
    "src,expected",
    [
        ("from app import main\n", [1]),
        ("def f():\n    from app import main\n", [2]),
        ("import app\n", [1]),
        # Not the entry point — a package that merely starts with "app".
        ("import application\n", []),
        ("from apple import pie\n", []),
        # Relative imports are within-package, not the root app.py.
        ("from . import app\n", []),
    ],
)
def test_app_import_detector_shapes(src, expected, tmp_path):
    path = tmp_path / "sample.py"
    path.write_text(src)
    assert _imports_module(path, "app") == expected

