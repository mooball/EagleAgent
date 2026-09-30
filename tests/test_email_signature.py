"""Tests for the outbound email signature (``includes/gmail/signature.py``)."""

from email import message_from_bytes
import base64

from config import config
from includes.gmail.signature import build_email_signature, logo_url


class TestBuildEmailSignature:
    def test_includes_logo_company_and_sender(self):
        sig = build_email_signature("Tom Cameron", "tom@eagle-exports.com")

        assert config.PUBLIC_BASE_URL in sig
        assert config.EMAIL_SIGNATURE_LOGO in sig
        assert "Tom Cameron" in sig
        assert "tom@eagle-exports.com" in sig
        assert config.EMAIL_SIGNATURE_COMPANY in sig
        assert config.EMAIL_SIGNATURE_SERVICE_LINE in sig
        assert config.EMAIL_SIGNATURE_ADDRESS in sig

    def test_address_is_an_explicit_black_maps_link(self):
        sig = build_email_signature("Tom", "tom@eagle-exports.com")
        # We author the Maps link ourselves (black, no underline) so Gmail's
        # Smart Links have nothing to add.
        assert "https://www.google.com/maps/search/" in sig
        assert "color:#111827;text-decoration:none" in sig
        assert "Gravel%20Pit%20Road" in sig
        assert "\u200b" not in sig

    def test_includes_regards_salutation(self):
        assert "Regards," in build_email_signature("Tom", "tom@eagle-exports.com")

    def test_uses_brand_colours(self):
        sig = build_email_signature("Tom", "tom@eagle-exports.com")
        assert config.EMAIL_SIGNATURE_GREEN in sig
        assert config.EMAIL_SIGNATURE_BLUE in sig

    def test_name_falls_back_to_email(self):
        sig = build_email_signature(None, "tom@eagle-exports.com")
        assert "tom@eagle-exports.com" in sig

    def test_html_is_escaped(self):
        sig = build_email_signature('<script>bad</script>', "a@b.com")
        assert "<script>" not in sig
        assert "&lt;script&gt;" in sig

    def test_logo_url_is_absolute(self):
        assert logo_url().startswith("http")


class TestSignatureInMimeMessage:
    def test_signature_appended_to_html_part(self):
        from includes.gmail.draft_service import _build_mime_message

        msg = _build_mime_message(
            user_email="staff@eagle-exports.com",
            recipient_email="supplier@acme.com",
            subject="Quote Request",
            body_html="<p>Please quote</p>",
            headers={},
            sender_name="Tom Cameron",
        )

        html_part = msg.get_payload()[0].get_payload()[1]
        html_text = html_part.get_payload(decode=True).decode("utf-8")

        assert "Please quote" in html_text
        assert "Tom Cameron" in html_text
        assert config.EMAIL_SIGNATURE_LOGO in html_text

    def test_plain_text_inherits_signature_without_logo(self):
        from includes.gmail.draft_service import _build_mime_message

        msg = _build_mime_message(
            user_email="staff@eagle-exports.com",
            recipient_email="supplier@acme.com",
            subject="Quote Request",
            body_html="<p>Please quote</p>",
            headers={},
            sender_name="Tom Cameron",
        )

        plain_part = msg.get_payload()[0].get_payload()[0]
        plain_text = plain_part.get_payload(decode=True).decode("utf-8")

        assert "Tom Cameron" in plain_text
        # The logo is an image, so it is dropped from the plain-text part.
        assert logo_url() not in plain_text


class TestPlainTextSignature:
    def test_signature_text_has_no_markup(self):
        from includes.gmail.signature import build_email_signature_text

        text = build_email_signature_text("Tom Cameron", "tom@eagle-exports.com")
        assert "<" not in text
        assert "Regards," in text
        assert "Tom Cameron" in text
        assert config.EMAIL_SIGNATURE_ADDRESS in text
        assert "tom@eagle-exports.com" in text

    def test_explicit_body_plain_is_signed(self):
        from includes.gmail.draft_service import _build_mime_message

        msg = _build_mime_message(
            user_email="staff@eagle-exports.com",
            recipient_email="supplier@acme.com",
            subject="Quote Request",
            body_html="<p>Please quote</p>",
            headers={},
            body_plain="Please quote",
            sender_name="Tom Cameron",
        )

        plain_part = msg.get_payload()[0].get_payload()[0]
        plain_text = plain_part.get_payload(decode=True).decode("utf-8")

        assert plain_text.startswith("Please quote")
        assert "Tom Cameron" in plain_text
        assert "Regards," in plain_text
        assert config.EMAIL_SIGNATURE_ADDRESS in plain_text
