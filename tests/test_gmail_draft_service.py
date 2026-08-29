"""Tests for includes/gmail/draft_service.py — draft creation and email sending."""

import base64

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

    @patch("includes.gmail.draft_service.get_gmail_client")
    @patch("includes.gmail.draft_service.check_recipient_allowed")
    @patch("includes.gmail.draft_service._save_draft_to_tracking")
    def test_cc_header_included_in_draft(self, mock_save, mock_check, mock_get_client):
        """CC list lands as a Cc: MIME header on the draft."""
        import base64 as _b64
        from email import message_from_bytes

        mock_check.return_value = None
        captured = {}
        mock_service = MagicMock()
        mock_create = MagicMock()
        mock_create.execute.return_value = {
            "id": "draft-cc", "message": {"id": "msg-cc", "threadId": "thread-cc"},
        }

        def _create(userId, body):
            captured["raw"] = body["message"]["raw"]
            return mock_create

        mock_service.users.return_value.drafts.return_value.create.side_effect = _create
        mock_get_client.return_value = mock_service

        from includes.gmail.draft_service import create_draft_email
        result = create_draft_email(
            user_email="staff@eagle.com",
            recipient_email="supplier@acme.com",
            subject="Quote Request",
            body_html="<p>Please quote</p>",
            rfq_id="RFQ-2026-0042",
            cc="ops@acme.com, manager@acme.com",
        )
        assert result["status"] == "ok"

        msg = message_from_bytes(_b64.urlsafe_b64decode(captured["raw"]))
        assert msg["Cc"] == "ops@acme.com, manager@acme.com"
        # To + both CC addresses all validated
        assert mock_check.call_count == 3

    @patch("includes.gmail.draft_service.check_recipient_allowed")
    def test_invalid_cc_rejected(self, mock_check):
        """Malformed CC addresses fail before the Gmail API is called."""
        from includes.gmail.draft_service import create_draft_email
        mock_check.return_value = None
        result = create_draft_email(
            user_email="staff@eagle.com",
            recipient_email="supplier@acme.com",
            subject="Test",
            body_html="<p>Test</p>",
            rfq_id="RFQ-2026-0042",
            cc="not-an-email",
        )
        assert result["status"] == "error"


class TestNormalizeCc:
    def test_empty(self):
        from includes.gmail.draft_service import _normalize_cc
        assert _normalize_cc(None) == ""
        assert _normalize_cc("") == ""
        assert _normalize_cc("  , , ") == ""

    @patch("includes.gmail.draft_service.check_recipient_allowed")
    def test_comma_list_normalised(self, mock_check):
        from includes.gmail.draft_service import _normalize_cc
        mock_check.return_value = None
        assert _normalize_cc(" a@x.com , b@y.com ") == "a@x.com, b@y.com"

    @patch("includes.gmail.draft_service.check_recipient_allowed")
    def test_invalid_raises(self, mock_check):
        from includes.gmail.draft_service import _normalize_cc
        mock_check.return_value = None
        with pytest.raises(ValueError):
            _normalize_cc("a@x.com, not-an-email")

    @patch("includes.gmail.draft_service.check_recipient_allowed")
    def test_blocked_domain_propagates(self, mock_check):
        from includes.gmail.draft_service import _normalize_cc, RecipientBlockedError
        mock_check.side_effect = RecipientBlockedError("Blocked")
        with pytest.raises(RecipientBlockedError):
            _normalize_cc("bad@blocked.com")


# ---------------------------------------------------------------------------
# MIME builder: cid conversion, nesting, header injection
# ---------------------------------------------------------------------------

def _tiny_png_b64() -> str:
    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 16).decode()


class TestInlineImagesToCid:
    def test_data_uri_converted_to_cid(self):
        from includes.gmail.draft_service import _inline_images_to_cid

        html = f'<p>Hi</p><img src="data:image/png;base64,{_tiny_png_b64()}">'
        out, parts = _inline_images_to_cid(html)
        assert "data:image" not in out
        assert "cid:" in out
        assert len(parts) == 1
        assert parts[0]["Content-ID"].startswith("<img")
        assert parts[0]["Content-Disposition"].startswith("inline")

    def test_malformed_data_uri_left_untouched(self):
        from includes.gmail.draft_service import _inline_images_to_cid

        html = '<img src="data:image/png;base64,!!!not-base64!!!">'
        out, parts = _inline_images_to_cid(html)
        assert "!!!not-base64!!!" in out
        assert parts == []


class TestBuildMimeMessage:
    def _build(self, **overrides):
        from includes.gmail.draft_service import _build_mime_message

        defaults = dict(
            user_email="staff@eagle.com",
            recipient_email="supplier@acme.com",
            subject="Quote Request",
            body_html="<p>Hello</p>",
            headers={"X-Eagle-OP": "RFQ-2026-0042"},
        )
        defaults.update(overrides)
        return _build_mime_message(**defaults)

    def test_basic_nesting_mixed_to_alternative(self):
        msg = self._build()
        assert msg.get_content_type() == "multipart/mixed"
        children = msg.get_payload()
        assert len(children) == 1
        alternative = children[0]
        assert alternative.get_content_type() == "multipart/alternative"
        alts = alternative.get_payload()
        assert [p.get_content_type() for p in alts] == ["text/plain", "text/html"]

    def test_attachment_is_direct_child_of_mixed(self):
        msg = self._build(
            attachments=[{"filename": "quote.pdf", "mime_type": "application/pdf", "data": b"%PDF fake"}]
        )
        children = msg.get_payload()
        assert len(children) == 2
        pdf = children[1]
        assert pdf.get_content_type() == "application/pdf"
        assert pdf["Content-Disposition"].startswith("attachment")
        assert "quote.pdf" in pdf["Content-Disposition"]

    def test_inline_image_nesting(self):
        html = f'<img src="data:image/png;base64,{_tiny_png_b64()}">'
        msg = self._build(body_html=html)
        assert msg.get_content_type() == "multipart/mixed"
        related = msg.get_payload()[0]
        assert related.get_content_type() == "multipart/related"
        parts = related.get_payload()
        assert parts[0].get_content_type() == "multipart/alternative"
        assert parts[1].get_content_type().startswith("image/")
        assert parts[1]["Content-Disposition"].startswith("inline")
        html_part = parts[0].get_payload()[1]
        html_text = html_part.get_payload(decode=True).decode("utf-8")
        assert "cid:" in html_text
        assert "data:image" not in html_text

    def test_mixed_case_pasted_image_plus_attachment(self):
        html = f'<p>Hi</p><img src="data:image/png;base64,{_tiny_png_b64()}">'
        msg = self._build(
            body_html=html,
            attachments=[{"filename": "f.pdf", "mime_type": "application/pdf", "data": b"x"}],
        )
        children = msg.get_payload()
        assert len(children) == 2
        assert children[0].get_content_type() == "multipart/related"
        assert children[1].get_content_type() == "application/pdf"

    def test_header_injection_stripped(self):
        msg = self._build(
            subject="Bad\r\nBcc: evil@x.com",
            attachments=[{"filename": "a\r\nb.pdf", "mime_type": "application/pdf", "data": b"x"}],
        )
        assert "\r" not in msg["subject"]
        assert "\n" not in msg["subject"]
        attachment = msg.get_payload()[1]
        assert "\r" not in attachment["Content-Disposition"]
        assert "\n" not in attachment["Content-Disposition"]

    def test_body_plain_override(self):
        msg = self._build(body_plain="custom plain text")
        plain = msg.get_payload()[0].get_payload()[0]
        assert plain.get_content_type() == "text/plain"
        assert plain.get_payload(decode=True).decode("utf-8") == "custom plain text"


class TestGmailTransport:
    def _msg(self, attachment: bytes | None = None):
        from includes.gmail.draft_service import _build_mime_message

        atts = (
            [{"filename": "big.bin", "mime_type": "application/octet-stream", "data": attachment}]
            if attachment
            else None
        )
        return _build_mime_message(
            user_email="a@b.com",
            recipient_email="c@d.com",
            subject="S",
            body_html="<p>body</p>",
            headers={},
            attachments=atts,
        )

    def test_small_send_uses_json_raw(self):
        from includes.gmail.draft_service import _gmail_send

        service = MagicMock()
        _gmail_send(service, self._msg())
        service.users().messages().send.assert_called_once()
        kwargs = service.users().messages().send.call_args.kwargs
        assert "raw" in kwargs["body"]
        assert "media_body" not in kwargs

    def test_large_send_uses_media_upload(self):
        from includes.gmail.draft_service import _gmail_send

        msg = self._msg(b"Z" * (5 * 1024 * 1024))  # 5 MB raw -> MIME > 4 MB
        assert len(msg.as_bytes()) > 4 * 1024 * 1024
        service = MagicMock()
        _gmail_send(service, msg)
        kwargs = service.users().messages().send.call_args.kwargs
        assert kwargs["body"] == {}
        assert "media_body" in kwargs

    def test_small_draft_uses_json_raw(self):
        from includes.gmail.draft_service import _gmail_create_draft

        service = MagicMock()
        _gmail_create_draft(service, self._msg())
        service.users().drafts().create.assert_called_once()
        kwargs = service.users().drafts().create.call_args.kwargs
        assert "raw" in kwargs["body"]["message"]
        assert "media_body" not in kwargs

    def test_large_draft_uses_media_upload(self):
        from includes.gmail.draft_service import _gmail_create_draft

        msg = self._msg(b"Z" * (5 * 1024 * 1024))
        service = MagicMock()
        _gmail_create_draft(service, msg)
        kwargs = service.users().drafts().create.call_args.kwargs
        assert kwargs["body"]["message"] == {}
        assert "media_body" in kwargs
