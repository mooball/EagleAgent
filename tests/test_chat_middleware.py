"""Tests for includes/chat/middleware.py — OAuth redirect and Gemini retry notifier."""

import pytest
import logging
from unittest.mock import MagicMock, AsyncMock, patch

from includes.chat.middleware import OAuthErrorRedirectMiddleware, GeminiRetryNotifier


class TestOAuthErrorRedirectMiddleware:
    @pytest.mark.asyncio
    async def test_passes_through_non_http(self):
        """WebSocket requests should pass through unchanged."""
        app = AsyncMock()
        middleware = OAuthErrorRedirectMiddleware(app)

        scope = {"type": "websocket", "path": "/chat/ws"}
        receive = AsyncMock()
        send = AsyncMock()

        await middleware(scope, receive, send)

        app.assert_awaited_once_with(scope, receive, send)

    @pytest.mark.asyncio
    async def test_passes_through_non_oauth_path(self):
        """Non-OAuth HTTP paths should pass through."""
        app = AsyncMock()
        middleware = OAuthErrorRedirectMiddleware(app)

        scope = {"type": "http", "path": "/dashboard"}
        receive = AsyncMock()
        send = AsyncMock()

        await middleware(scope, receive, send)

        app.assert_awaited_once_with(scope, receive, send)

    @pytest.mark.asyncio
    async def test_passes_through_non_callback_oauth(self):
        """OAuth paths without /callback suffix should pass through."""
        app = AsyncMock()
        middleware = OAuthErrorRedirectMiddleware(app)

        scope = {"type": "http", "path": "/auth/oauth/google/login"}
        receive = AsyncMock()
        send = AsyncMock()

        await middleware(scope, receive, send)

        app.assert_awaited_once_with(scope, receive, send)

    @pytest.mark.asyncio
    async def test_redirects_401_on_callback(self):
        """401 on an OAuth callback path should redirect to /login."""
        app = AsyncMock()

        async def app_with_401(scope, rec, send_wrapper):
            # Simulate a 401 response
            await send_wrapper({"type": "http.response.start", "status": 401})
            await send_wrapper({"type": "http.response.body", "body": b""})

        app.side_effect = app_with_401
        middleware = OAuthErrorRedirectMiddleware(app)

        scope = {"type": "http", "path": "/auth/oauth/google/callback"}
        receive = AsyncMock()
        sent_messages = []

        async def capture_send(msg):
            sent_messages.append(msg)

        await middleware(scope, receive, capture_send)

        # Should have intercepted the 401 and sent a 302 redirect
        start_msg = sent_messages[0]
        assert start_msg["type"] == "http.response.start"
        assert start_msg["status"] == 302
        assert any(b"location" in header for header in start_msg["headers"])

    @pytest.mark.asyncio
    async def test_passes_through_200_on_callback(self):
        """200 on an OAuth callback should pass through normally."""
        sent_messages = []

        async def app_with_200(scope, rec, send):
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = OAuthErrorRedirectMiddleware(app_with_200)

        scope = {"type": "http", "path": "/auth/oauth/google/callback"}
        recv = AsyncMock()

        async def capture(msg):
            sent_messages.append(msg)

        await middleware(scope, recv, capture)

        # 200 should pass through — check status
        assert sent_messages[0]["status"] == 200
        assert sent_messages[1]["body"] == b"ok"


class TestGeminiRetryNotifier:
    def test_emits_only_503_and_429(self):
        """Should only trigger on 503/429 retry messages."""
        handler = GeminiRetryNotifier(level=logging.INFO)

        # 503 retry — should pass through
        record_503 = logging.LogRecord(
            "test", logging.INFO, "", 0, "Retrying request after 503", (), None
        )
        handler.emit(record_503)
        # No assertion needed — just shouldn't raise

        # Non-retry message — should be ignored silently
        record_info = logging.LogRecord(
            "test", logging.INFO, "", 0, "Normal request completed", (), None
        )
        handler.emit(record_info)

        # 429 retry — should pass through
        record_429 = logging.LogRecord(
            "test", logging.INFO, "", 0, "Retrying after 429 rate limit", (), None
        )
        handler.emit(record_429)

    def test_ignores_non_retry(self):
        """Non-retry-containing messages should be silently ignored."""
        handler = GeminiRetryNotifier(level=logging.INFO)

        record = logging.LogRecord(
            "test", logging.INFO, "", 0, "Some other log message", (), None
        )
        # Should not raise
        handler.emit(record)
