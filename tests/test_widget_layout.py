"""Static guard over the widget form's narrow-panel layout.

A widget form is two columns when there is room and one when there is not. The
reference is the **card's own measured width**, recorded by ``wireWidget()`` in
``templates/chat_ui/embed.html`` as ``data-widget-narrow``:

* a ``@media`` rule cannot work — the chat panel can be 380px wide on a 1920px
  screen, so the viewport says nothing about the space available;
* the *panel* width is not the right question either: the card is what has to
  fit two inputs, and a card can also be rendered somewhere wider;
* this was a ``@container (max-width: 400px)`` rule, which measured the panel
  while the card sat at its own max-content width (a shrink-to-fit flex item),
  so the form never reached two columns even when there was room.

Three places have to agree, and each fails silently at wide widths — invisible
to the Python suite, to ``node --check``, and to anyone looking at a wide
window:

1. the client sets ``data-widget-narrow``,
2. ``input.css`` collapses ``.widget-form-grid`` off that attribute and resets
   ``grid-column`` (a ``col-span-2`` field would otherwise force an implicit
   second column and undo the stack),
3. the template keeps the ``widget-form-grid`` hook.
"""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
INPUT_CSS = ROOT / "input.css"
EMBED_HTML = ROOT / "templates" / "chat_ui" / "embed.html"
WIDGET_TEMPLATE = ROOT / "templates" / "chat_ui" / "widgets" / "_add_supplier.html"

HOOK = "widget-form-grid"

_NARROW_GRID = re.compile(r'\[data-widget-narrow="1"\]\.widget-form-grid\{([^}]*)\}')
_NARROW_CHILD = re.compile(r'\[data-widget-narrow="1"\]\.widget-form-grid>\*\{([^}]*)\}')


def _compact(text: str) -> str:
    """Whitespace-insensitive, so the assertions survive reformatting."""
    return re.sub(r"\s+", "", text)


def test_the_client_records_the_card_width():
    html = EMBED_HTML.read_text()
    assert "widgetNarrow" in html, (
        "input.css collapses on [data-widget-narrow]; if the client stops setting "
        "it (dataset.widgetNarrow) the form silently never stacks"
    )
    assert "NARROW_CARD_PX" in html, "the threshold should stay a named constant"


def test_narrow_cards_stack_to_one_column():
    css = _compact(INPUT_CSS.read_text())
    grid = _NARROW_GRID.search(css)
    assert grid, "no [data-widget-narrow] rule collapsing .widget-form-grid"
    assert "grid-template-columns:1fr" in grid.group(1), (
        "the narrow rule must set a single column: " + grid.group(1)
    )


def test_spanning_fields_do_not_force_a_second_column():
    """Without the reset, ``col-span-2`` keeps an implicit second column alive
    and the stack looks like it did nothing."""
    css = _compact(INPUT_CSS.read_text())
    child = _NARROW_CHILD.search(css)
    assert child, "no child reset rule for the narrow state"
    assert "grid-column:auto" in child.group(1), child.group(1)


def test_no_field_spans_both_columns():
    """Every field is one grid cell.

    Spanning the important fields (Company name, Contact email) made them twice
    the width of their neighbours and stopped the layout ever reading as the two
    columns it was meant to be — reported as a bug twice before it was removed.
    The macro still supports spanning for a wide form; the widget must not use
    it. Reintroducing it here should be a deliberate decision, so it trips this.
    """
    html = WIDGET_TEMPLATE.read_text()
    assert "full_width" not in html, (
        "the widget must not pass full_width — see this test's docstring"
    )


def test_the_widget_template_carries_the_hook():
    html = WIDGET_TEMPLATE.read_text()
    # Two grids: the main fields and the optional "more details" block.
    assert html.count(HOOK) == 2, (
        f"expected the {HOOK} hook on both field grids, found {html.count(HOOK)}"
    )


def test_message_area_is_still_a_size_container():
    """Unrelated to widgets now, but the 85% bubble cap at wide panels queries
    it: drop the container-type and that rule goes inert with no other symptom."""
    assert re.search(
        r"#embed-messages\s*\{[^}]*container-type:\s*inline-size",
        INPUT_CSS.read_text(),
    ), "#embed-messages must stay a size container for the bubble-width rule"
