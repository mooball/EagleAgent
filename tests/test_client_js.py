"""Behavioural checks for client-side JS that the Python suite cannot see.

Each bug these cover was invisible to BOTH ``pytest`` and ``node --check``:

* ``rowEl`` returning two different shapes (a bare element for transient rows),
* an ``EventSource`` attached before its run was registered, so the run's
  ``dashboard_refresh`` and ``agent_done`` were stranded,
* a redirected non-JSON response read as success, so the NetSuite modal closed
  claiming the supplier had been added,
* an Alpine binding evaluating to ``0`` instead of ``false`` — which *sets* a
  boolean attribute, because Alpine only removes it for null/undefined/false.

The checks live in ``tests/client/`` and extract the real function source from
the templates by brace-matching, driving it with stubs. They therefore fail when
a template changes shape, unlike a copy of the function which would drift.

Requires ``node``; skipped when it is not on PATH so the Python suite still runs
in containers and CI without it. Run one on its own with::

    node tests/client/check_rowel.js
"""

import shutil
import subprocess
from pathlib import Path

import pytest

CLIENT_DIR = Path(__file__).parent / "client"

CHECKS = [
    "check_rowel.js",
    "check_sendaction.js",
    "check_ns_supplier.js",
    "check_widget_submit.js",
    "check_widget_lookup.js",
]

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


@pytest.mark.parametrize("check", CHECKS)
def test_client_check(check):
    result = subprocess.run(
        ["node", str(CLIENT_DIR / check)],
        capture_output=True,
        text=True,
        # Under the suite-wide pytest-timeout of 30s (pyproject).
        timeout=25,
    )
    assert result.returncode == 0, (
        f"{check} reported failures\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
