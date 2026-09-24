"""Static guards over Alpine bindings in the templates.

Alpine removes a boolean attribute (``disabled``, ``checked``, …) only when the
bound value is exactly ``null``, ``undefined`` or ``false``. Verified in the
build the app loads::

    function Ht(e,t,r){ [null,void 0,!1].includes(r) && Ki(t)
        ? e.removeAttribute(t) : (…setAttribute…) }

Every other falsy value falls through and **sets** the attribute. In particular
``A.length && B`` evaluates to the number ``0`` when the collection is empty, so
``:disabled="items.length && !confirmed"`` disables the control precisely when it
should be enabled.

That is not hypothetical: it disabled "Add to NetSuite" for every supplier
without a flagged duplicate, and enabled it only when one *was* flagged and
confirmed — exactly inverted. No Python test could see it, because the failure
is in the expression's *type*, not its logic.

These checks are regex-level, need no node, and always run.
"""

import re
from pathlib import Path

TEMPLATES = Path(__file__).parent.parent / "templates"

BOOLEAN_ATTRS = (
    "disabled checked required readonly multiple hidden open selected autofocus"
).split()

_BINDING = re.compile(r":(" + "|".join(BOOLEAN_ATTRS) + r')="([^"]*)"')

# `X.length && …` yields 0 when empty — a number, not `false`, so Alpine SETS it.
_LENGTH_AND = re.compile(r"\.length\s*&&")


def _bindings():
    """Yield (path, lineno, attribute, expression) for every boolean binding."""
    for path in sorted(TEMPLATES.rglob("*.html")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for match in _BINDING.finditer(line):
                yield path, lineno, match.group(1), match.group(2).strip()


def test_scan_finds_bindings():
    """Guard against the regex silently breaking and making the audit vacuous."""
    found = list(_bindings())
    assert len(found) > 20, f"expected many boolean bindings, found {len(found)}"


def test_no_bare_length_and_in_boolean_bindings():
    """`A.length && B` yields 0, which SETS the attribute instead of removing it.

    Use ``A.length > 0 && B``, or wrap the whole expression in ``!!(…)`` so the
    binding always yields a real boolean.
    """
    offenders = [
        f"{path.relative_to(TEMPLATES.parent)}:{lineno}  :{attr}=\"{expr}\""
        for path, lineno, attr, expr in _bindings()
        if _LENGTH_AND.search(expr)
    ]
    assert not offenders, (
        "boolean-attribute binding evaluates to 0 when the length is 0, so Alpine "
        "SETS the attribute (disabling the control) rather than removing it.\n"
        "Use `A.length > 0 && B`, or wrap in !!(…):\n  " + "\n  ".join(offenders)
    )


def test_ns_submit_button_is_not_gated_on_duplicates():
    """Regression: the NetSuite submit button must not be gated on the duplicate
    confirm. `duplicates.length` made it 0 and disabled Add for every supplier
    without a flagged duplicate. The confirm is enforced in submitNsSupplier(),
    where the refusal can be explained, instead of leaving a dead control."""
    src = (TEMPLATES / "partials" / "_rfq_quotation_final.html").read_text()
    index = src.index('type="submit"')
    match = re.search(r':disabled="([^"]*)"', src[index:index + 400])
    assert match, "the NetSuite submit button lost its :disabled binding"
    expr = match.group(1).strip()
    assert expr == "nsSup.submitting", (
        f"NetSuite submit :disabled should be exactly 'nsSup.submitting', got {expr!r}. "
        "Anything involving `.length &&` yields 0 and disables the button."
    )
