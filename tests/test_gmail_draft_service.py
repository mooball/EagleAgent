"""Tests for includes/gmail/draft_service.py — draft creation and email sending."""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from includes.gmail.draft_service import (
    generate_compose_url,
)


class TestGenerateComposeUrl:
    def test_basic_url(self):
        url = generate_compose_url("msg-abc123", "user@example.com")
        assert "msg-abc123" in url
        # email is URL-encoded in authuser param
        assert "user%40example.com" in url or "user@example.com" in url
        assert url.startswith("https://mail.google.com/mail/u/")

    def test_gmail_com_primary(self):
        url = generate_compose_url("msg-xyz", "test@gmail.com")
        assert "msg-xyz" in url
        assert "/mail/u/" in url

    def test_workspace_user(self):
        url = generate_compose_url("msg-456", "user@eagle-exports.com")
        assert url.startswith("https://")
        assert "msg-456" in url


class TestCreateDraftEmail:
    @patch("includes.gmail.draft_service.get_gmail_client")
    @patch("includes.gmail.draft_service.check_recipient_allowed")
    @patch("includes.gmail.draft_service._save_draft_to_tracking")
    def test_creates_draft_successfully(self, mock_save, mock_check, mock_get_client):
        """Verify draft creation returns expected success structure."""
        mock_check.return_value = None  # allowed
        mock_service = MagicMock()
        mock_drafts = MagicMock()
        mock_create = MagicMock()
        mock_create.execute.return_value = {
            "id": "draft-123",
            "message": {"id": "msg-456", "threadId": "thread-789"},
        }
        mock_drafts.create.return_value = mock_create
        mock_service.users.return_value.drafts.return_value = mock_drafts
        mock_get_client.return_value = mock_service

        from includes.gmail.draft_service import create_draft_email

        result = create_draft_email(
            user_email="staff@eagle.com",
            recipient_email="supplier@acme.com",
            subject="Quote Request [RFQ-2026-0042]",
            body_html="<p>Please quote</p>",
            rfq_id="RFQ-2026-0042",
        )

        assert result["status"] == "ok"
        assert result["draft_id"] == "draft-123"
        assert result["thread_id"] == "thread-789"
        assert "compose_url" in result

    @patch("includes.gmail.draft_service.check_recipient_allowed")
    def test_recipient_blocked(self, mock_check):
        """Verify blocked recipients are rejected before API call."""
        from includes.gmail.draft_service import RecipientBlockedError, create_draft_email

        mock_check.side_effect = RecipientBlockedError("Blocked domain")

        result = create_draft_email(
            user_email="staff@eagle.com",
            recipient_email="bad@blocked.com",
            subject="Test",
            body_html="<p>Test</p>",
            rfq_id="RFQ-2026-0042",
        )

        assert result["status"] == "error"
        assert "Blocked domain" in result["message"]

    @patch("includes.gmail.draft_service.get_gmail_client")
    @patch("includes.gmail.draft_service.check_recipient_allowed")
    def test_api_error_handled(self, mock_check, mock_get_client):
        """Verify Gmail API errors return error dict, not raise."""
        from googleapiclient.errors import HttpError

        mock_check.return_value = None
        mock_service = MagicMock()
        mock_service.users.side_effect = HttpError(
            MagicMock(status=403), b'{"error": "forbidden"}'
        )
        mock_get_client.return_value = mock_service

        from includes.gmail.draft_service import create_draft_email

        result = create_draft_email(
            user_email="staff@eagle.com",
            recipient_email="supplier@acme.com",
            subject="Test",
            body_html="<p>Test</p>",
            rfq_id="RFQ-2026-0042",
        )

        assert result["status"] == "error"
        assert "Failed to create draft" in result["message"]
