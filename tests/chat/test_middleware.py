"""Tests for includes/chat/middleware.py — stale Chainlit session handling.

Regression: after a deploy, browser tabs hold dead Chainlit session ids.
Clicking a chat action POSTs /chat/project/action with the stale id and
Chainlit raises ValueError("Session not found"), producing a 500 with a
full traceback. The handler maps that to a quiet 410 Gone.
"""

import pytest
from starlette.responses import JSONResponse

from includes.chat.middleware import stale_chainlit_session_handler


class _FakeRequest:
    def __init__(self, path: str):
        self.url = type("URL", (), {"path": path})()


class TestStaleChainlitSessionHandler:
    def test_session_not_found_on_action_returns_410(self):
        response = stale_chainlit_session_handler(
            _FakeRequest("/chat/project/action"),
            ValueError("Session not found"),
        )
        assert isinstance(response, JSONResponse)
        assert response.status_code == 410

    def test_other_value_errors_are_reraised(self):
        with pytest.raises(ValueError, match="Something else"):
            stale_chainlit_session_handler(
                _FakeRequest("/chat/project/action"),
                ValueError("Something else"),
            )

    def test_session_not_found_on_other_path_reraised(self):
        with pytest.raises(ValueError, match="Session not found"):
            stale_chainlit_session_handler(
                _FakeRequest("/chat/message"),
                ValueError("Session not found"),
            )

    def test_non_value_errors_are_reraised(self):
        with pytest.raises(RuntimeError, match="boom"):
            stale_chainlit_session_handler(
                _FakeRequest("/chat/project/action"),
                RuntimeError("boom"),
            )
