"""The list search form must push a URL that mirrors the submitted query.

The old macro accumulated query params with ``{% set %}`` inside a for loop,
which Jinja scopes to the loop — so the pushed URL silently dropped every
hidden filter *and* ``q``, leaving the address bar and Back/Forward stale
even though the server applied the search.
"""

import re

from includes.dashboard.routes._helpers import templates

_COMPONENTS = templates.get_template("components.html").module


def _push_url(html: str) -> str:
    match = re.search(r'hx-push-url="([^"]*)"', html)
    assert match, f"no hx-push-url in:\n{html}"
    return match.group(1)


def test_push_url_includes_q_and_every_hidden_filter():
    html = _COMPONENTS.search_form(
        "/partial/rfqs", "Search…", "widget",
        {"mine": "all", "status": "quoted", "sort": "customer", "order": "asc"},
    )
    url = _push_url(html)
    assert url.startswith("/rfqs?")
    for fragment in ("q=widget", "mine=all", "status=quoted", "sort=customer", "order=asc"):
        assert fragment in url, f"{fragment!r} missing from {url!r}"


def test_push_url_is_bare_when_no_params():
    html = _COMPONENTS.search_form("/partial/suppliers", "Search…", "", {})
    assert _push_url(html) == "/suppliers"


def test_push_url_encodes_values():
    html = _COMPONENTS.search_form("/partial/rfqs", "Search…", "a&b=c", {})
    assert "q=a%26b%3Dc" in _push_url(html)
