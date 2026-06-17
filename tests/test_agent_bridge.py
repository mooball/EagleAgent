"""Tests for includes/agent_bridge.py — dashboard↔Chainlit communication."""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from includes.agent_bridge import notify_dashboard, handle_bridge_request


class TestNotifyDashboard:
    @pytest.mark.asyncio
    @patch("chainlit.send_window_message", new_callable=AsyncMock)
    async def test_sends_window_message(self, mock_send):
        """notify_dashboard should call cl.send_window_message with the command."""
        await notify_dashboard("dashboard_refresh")

        mock_send.assert_awaited_once_with({"type": "dashboard_refresh"})

    @pytest.mark.asyncio
    @patch("chainlit.send_window_message", new_callable=AsyncMock)
    async def test_sends_with_payload(self, mock_send):
        """Should include payload dict when provided."""
        await notify_dashboard("agent_navigate", {"url": "/rfqs/RFQ-123"})

        expected = {"type": "agent_navigate", "payload": {"url": "/rfqs/RFQ-123"}}
        mock_send.assert_awaited_once_with(expected)

    @pytest.mark.asyncio
    @patch("chainlit.send_window_message", new_callable=AsyncMock)
    async def test_handles_missing_chainlit_context(self, mock_send):
        """Should not raise when called outside Chainlit context."""
        mock_send.side_effect = RuntimeError("no context")

        # Should not raise
        await notify_dashboard("dashboard_refresh")


class TestHandleBridgeRequest:
    @pytest.mark.asyncio
    @patch("main.get_current_user", return_value=None)
    async def test_requires_authentication(self, mock_user):
        """Unauthenticated requests should return 401."""
        request = AsyncMock()
        request.cookies = {}
        request.session = {}

        response = await handle_bridge_request(request)

        assert response.status_code == 401

    @pytest.mark.asyncio
    @patch("main.get_current_user", return_value={"email": "user@eagle.com"})
    async def test_requires_chainlit_session(self, mock_user):
        """Missing Chainlit session cookie should return 400."""
        request = AsyncMock()
        request.cookies = {}

        response = await handle_bridge_request(request)

        assert response.status_code == 400
        assert "Chainlit session" in response.body.decode()

    @pytest.mark.asyncio
    @patch("main.get_current_user", return_value={"email": "user@eagle.com"})
    async def test_requires_action_name(self, mock_user):
        """Missing action name in body should return 400."""
        request = AsyncMock()
        request.cookies = {"X-Chainlit-Session-id": "session-123"}
        request.json = AsyncMock(return_value={"action": {}})

        response = await handle_bridge_request(request)

        assert response.status_code == 400

    @pytest.mark.asyncio
    @patch("main.get_current_user", return_value={"email": "user@eagle.com"})
    async def test_invalid_json_body(self, mock_user):
        """Malformed JSON should return 400."""
        request = AsyncMock()
        request.cookies = {"X-Chainlit-Session-id": "session-123"}
        request.json = AsyncMock(side_effect=ValueError("bad json"))

        response = await handle_bridge_request(request)

        assert response.status_code == 400

    @pytest.mark.asyncio
    @patch("includes.agent_bridge.dispatch_action")
    @patch("main.get_current_user", return_value={"email": "user@eagle.com"})
    async def test_dispatches_valid_action(self, mock_user, mock_dispatch):
        """Valid request should dispatch the action and return result."""
        mock_dispatch.return_value = {"success": True}

        request = AsyncMock()
        request.cookies = {"X-Chainlit-Session-id": "session-123"}
        request.json = AsyncMock(return_value={
            "action": {"name": "rfq_find_suppliers", "payload": {"rfq_id": "RFQ-123"}}
        })

        response = await handle_bridge_request(request)

        mock_dispatch.assert_awaited_once_with("session-123", "rfq_find_suppliers", {"rfq_id": "RFQ-123"})
        assert response.status_code == 200

    @pytest.mark.asyncio
    @patch("includes.agent_bridge.dispatch_action")
    @patch("main.get_current_user", return_value={"email": "user@eagle.com"})
    async def test_returns_error_from_dispatcher(self, mock_user, mock_dispatch):
        """Error from dispatch_action should be returned as 422."""
        mock_dispatch.return_value = {"error": "Action failed"}

        request = AsyncMock()
        request.cookies = {"X-Chainlit-Session-id": "session-123"}
        request.json = AsyncMock(return_value={
            "action": {"name": "bad_action", "payload": {}}
        })

        response = await handle_bridge_request(request)

        assert response.status_code == 422
