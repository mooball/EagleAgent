"""Attachment failure classification — codes, not marker strings.

Before this change a failed attachment put its error text INTO the content bundle
and fed it to the extraction LLM. So a caller could not tell a failed attachment
from one whose text legitimately contained those words, "9 of 10 attachments
read" was not representable, and an RFQ could come out one line short with
nothing anywhere saying why.

See `.github/prompts/plan-attachmentFailureCodes.prompt.md`.
"""

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from includes.dashboard.models import EmailTracking
from includes.email_pipeline import (
    AttachmentExtraction,
    AttachmentFailure,
    BundleFailure,
    ContentBundle,
    PLACEHOLDER_EMPTY,
    PLACEHOLDER_UNPARSED,
    PLACEHOLDER_UNREADABLE,
    extract_image_content,
    extract_pdf_content,
    extract_spreadsheet_content,
)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_MIME = "application/pdf"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session():
    """Test DB session with a SAVEPOINT so inner commits roll back at the end."""
    from includes.dashboard.database import _sync_url

    engine = create_engine(_sync_url(), pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection)
    session = Session(bind=connection)
    session.begin_nested()

    from sqlalchemy import event

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess, trans):
        if trans.nested and not trans._parent.nested:
            sess.begin_nested()

    session.close = lambda: None
    yield session
    transaction.rollback()
    connection.close()


def _tracking(session, attachments=None, body="Please quote the attached."):
    """A minimal EmailTracking row, optionally with attachments_json."""
    tracking = EmailTracking(
        gmail_thread_id=f"thread-{uuid.uuid4().hex[:8]}",
        gmail_message_id=f"msg-{uuid.uuid4().hex[:8]}",
        user_email="test@eagle-exports.com",
        direction="received",
        subject="Test",
        sender_email="customer@test.com",
        body_markdown=body,
        attachments_json=attachments,
    )
    session.add(tracking)
    session.flush()
    return tracking


def _att(filename, mime, att_id="att-1", size=1024):
    return {"filename": filename, "mime_type": mime, "size": size,
            "gmail_attachment_id": att_id}


def _bundle(session, tracking, *, pdf=None, image=None, sheet=None, raw=b"bytes",
            triage="quote_content", fetch_ok=True):
    """Run build_content_bundle with Gmail and the extractors stubbed out."""
    from includes.tools.supplier_quote_pipeline import build_content_bundle

    with patch("includes.tools.supplier_quote_pipeline._get_session", return_value=session), \
         patch("includes.tools.supplier_quote_pipeline.fetch_gmail_attachment_bytes",
               return_value=(raw if fetch_ok else None)), \
         patch("includes.tools.supplier_quote_pipeline.triage_image", return_value=triage), \
         patch("includes.tools.supplier_quote_pipeline.extract_pdf_content",
               side_effect=pdf or (lambda *a, **k: AttachmentExtraction("pdf text"))), \
         patch("includes.tools.supplier_quote_pipeline.extract_image_content",
               side_effect=image or (lambda *a, **k: AttachmentExtraction("image text"))), \
         patch("includes.tools.supplier_quote_pipeline.extract_spreadsheet_content",
               side_effect=sheet or (lambda *a, **k: AttachmentExtraction("sheet text"))):
        return build_content_bundle(tracking.id)


# ---------------------------------------------------------------------------
# Extractor failure codes
# ---------------------------------------------------------------------------

class TestExtractorFailureCodes:
    def test_pdf_model_error(self):
        with patch("includes.email_pipeline.llm_call_with_retry",
                   side_effect=RuntimeError("500 INTERNAL")):
            result = extract_pdf_content(b"x", "estimate.pdf")
        assert result.failure == AttachmentFailure.MODEL_ERROR
        assert "500 INTERNAL" in result.detail
        assert result.ok is False

    def test_pdf_empty(self):
        response = type("R", (), {"text": "", "candidates": None})()
        with patch("includes.email_pipeline.llm_call_with_retry", return_value=response):
            result = extract_pdf_content(b"x", "blank.pdf")
        assert result.failure == AttachmentFailure.EMPTY
        assert result.text == PLACEHOLDER_EMPTY

    def test_image_model_error(self):
        with patch("includes.email_pipeline.llm_call_with_retry",
                   side_effect=RuntimeError("timeout")):
            result = extract_image_content(b"x", "scan.png", "image/png")
        assert result.failure == AttachmentFailure.MODEL_ERROR

    def test_image_empty(self):
        response = type("R", (), {"text": ""})()
        with patch("includes.email_pipeline.llm_call_with_retry", return_value=response):
            result = extract_image_content(b"x", "blank.png", "image/png")
        assert result.failure == AttachmentFailure.EMPTY

    def test_spreadsheet_parse_error(self):
        result = extract_spreadsheet_content(b"not a real xlsx", "bad.xlsx", XLSX_MIME)
        assert result.failure == AttachmentFailure.PARSE_ERROR
        assert result.text == PLACEHOLDER_UNPARSED

    def test_success_passes_text_through(self):
        response = type("R", (), {"text": "Widget, 10.50", "candidates": None})()
        with patch("includes.email_pipeline.llm_call_with_retry", return_value=response):
            result = extract_pdf_content(b"x", "quote.pdf")
        assert result.ok and result.failure is None
        assert result.text == "Widget, 10.50"

    def test_placeholder_is_neutral_not_the_error(self):
        """Upstream error text must not leak into the prompt."""
        with patch("includes.email_pipeline.llm_call_with_retry",
                   side_effect=RuntimeError("500 INTERNAL secret-ish detail")):
            result = extract_pdf_content(b"x", "quote.pdf")
        assert result.text == PLACEHOLDER_UNREADABLE
        assert "500 INTERNAL" not in result.text
        assert "500 INTERNAL" in result.detail  # kept, for logs/records only

    def test_detail_is_truncated(self):
        with patch("includes.email_pipeline.llm_call_with_retry",
                   side_effect=RuntimeError("x" * 5000)):
            result = extract_pdf_content(b"x", "quote.pdf")
        assert len(result.detail) <= 200


# ---------------------------------------------------------------------------
# ContentBundle shape
# ---------------------------------------------------------------------------

class TestContentBundleShape:
    def test_to_dict_matches_stored_shape(self):
        bundle = ContentBundle(
            text="body",
            attachment_total=10,
            attachment_read=9,
            skipped_as_signature=0,
            failures=[{"filename": "a.pdf", "code": "model_error", "detail": "500"}],
        )
        assert bundle.to_dict() == {
            "attachment_total": 10,
            "attachment_read": 9,
            "skipped_as_signature": 0,
            "failures": [{"filename": "a.pdf", "code": "model_error", "detail": "500"}],
            "bundle_failure": None,
        }

    def test_complete_only_when_nothing_failed(self):
        assert ContentBundle(text="x").complete is True
        assert ContentBundle(text="x", failures=[{"filename": "a"}]).complete is False
        assert ContentBundle(bundle_failure=BundleFailure.NO_CONTENT).complete is False

    def test_bundle_failure_serialises_as_value(self):
        bundle = ContentBundle(bundle_failure=BundleFailure.EMAIL_NOT_FOUND)
        assert bundle.to_dict()["bundle_failure"] == "email_not_found"


# ---------------------------------------------------------------------------
# build_content_bundle — counts and recorded failures
# ---------------------------------------------------------------------------

class TestBuildContentBundle:
    def test_all_attachments_read(self, db_session):
        t = _tracking(db_session, [_att("a.pdf", PDF_MIME)])
        bundle = _bundle(db_session, t)
        assert (bundle.attachment_total, bundle.attachment_read) == (1, 1)
        assert bundle.failures == []
        assert bundle.complete is True
        assert "pdf text" in bundle.text

    def test_model_error_is_recorded_with_filename_and_code(self, db_session):
        t = _tracking(db_session, [_att("estimate QBRI1207.pdf", PDF_MIME)])
        failed = AttachmentExtraction(PLACEHOLDER_UNREADABLE,
                                      AttachmentFailure.MODEL_ERROR, "500 INTERNAL")
        bundle = _bundle(db_session, t, pdf=lambda *a, **k: failed)

        assert bundle.failures == [{
            "filename": "estimate QBRI1207.pdf",
            "code": "model_error",
            "detail": "500 INTERNAL",
        }]
        assert bundle.attachment_read == 0
        assert bundle.attachment_total == 1
        assert bundle.complete is False

    def test_failure_text_in_bundle_is_the_placeholder(self, db_session):
        t = _tracking(db_session, [_att("a.pdf", PDF_MIME)])
        failed = AttachmentExtraction(PLACEHOLDER_UNREADABLE,
                                      AttachmentFailure.MODEL_ERROR, "500 INTERNAL")
        bundle = _bundle(db_session, t, pdf=lambda *a, **k: failed)
        assert PLACEHOLDER_UNREADABLE in bundle.text
        assert "500 INTERNAL" not in bundle.text

    def test_signature_skip_is_not_a_failure(self, db_session):
        """A logo is expected, not a gap — 3 of 3 read, not 0 of 3."""
        t = _tracking(db_session, [_att("logo.png", "image/png")], body="body")
        bundle = _bundle(db_session, t, triage="signature")
        assert bundle.skipped_as_signature == 1
        assert bundle.failures == []
        assert bundle.attachment_read == 0
        assert bundle.attachment_total == 1
        assert bundle.complete is True   # skipped-by-design is not a failure

    def test_unsupported_type_is_recorded(self, db_session):
        t = _tracking(db_session, [_att("message.eml", "message/rfc822")])
        bundle = _bundle(db_session, t)
        assert [f["code"] for f in bundle.failures] == ["unsupported"]
        assert bundle.attachment_read == 0

    def test_fetch_failure_is_recorded(self, db_session):
        t = _tracking(db_session, [_att("a.pdf", PDF_MIME)])
        bundle = _bundle(db_session, t, fetch_ok=False)
        assert [f["code"] for f in bundle.failures] == ["fetch_failed"]
        assert PLACEHOLDER_UNREADABLE in bundle.text

    def test_mixed_counts(self, db_session):
        """2 read, 1 model error, 1 signature, 1 unsupported."""
        t = _tracking(db_session, [
            _att("good.pdf", PDF_MIME, att_id="a"),
            _att("good2.xlsx", XLSX_MIME, att_id="b"),
            _att("bad.pdf", PDF_MIME, att_id="c"),
            _att("logo.png", "image/png", att_id="d"),
            _att("mail.eml", "message/rfc822", att_id="e"),
        ], body="body")

        def pdf(raw, filename, **k):
            if filename == "bad.pdf":
                return AttachmentExtraction(PLACEHOLDER_UNREADABLE,
                                            AttachmentFailure.MODEL_ERROR, "500")
            return AttachmentExtraction("pdf text")

        # Only logo.png is triaged as a signature.
        def triage(raw, mime, filename, *a, **k):
            return "signature" if filename == "logo.png" else "quote_content"

        from includes.tools.supplier_quote_pipeline import build_content_bundle
        with patch("includes.tools.supplier_quote_pipeline._get_session", return_value=db_session), \
             patch("includes.tools.supplier_quote_pipeline.fetch_gmail_attachment_bytes", return_value=b"x"), \
             patch("includes.tools.supplier_quote_pipeline.triage_image", side_effect=triage), \
             patch("includes.tools.supplier_quote_pipeline.extract_pdf_content", side_effect=pdf), \
             patch("includes.tools.supplier_quote_pipeline.extract_spreadsheet_content",
                   return_value=AttachmentExtraction("sheet text")):
            bundle = build_content_bundle(t.id)

        assert bundle.attachment_total == 5
        assert bundle.attachment_read == 2
        assert bundle.skipped_as_signature == 1
        assert sorted(f["code"] for f in bundle.failures) == ["model_error", "unsupported"]

    def test_missing_email_gives_bundle_failure(self, db_session):
        from includes.tools.supplier_quote_pipeline import build_content_bundle
        with patch("includes.tools.supplier_quote_pipeline._get_session", return_value=db_session):
            bundle = build_content_bundle(99_999_999)
        assert bundle.bundle_failure == BundleFailure.EMAIL_NOT_FOUND
        assert bundle.text == ""

    def test_no_content_gives_bundle_failure(self, db_session):
        t = _tracking(db_session, attachments=None, body=None)
        from includes.tools.supplier_quote_pipeline import build_content_bundle
        with patch("includes.tools.supplier_quote_pipeline._get_session", return_value=db_session), \
             patch("includes.tools.supplier_quote_pipeline._backfill_email_content_from_gmail",
                   return_value=False):
            bundle = build_content_bundle(t.id)
        assert bundle.bundle_failure == BundleFailure.NO_CONTENT


# ---------------------------------------------------------------------------
# Backward compatibility — the legacy wrapper
# ---------------------------------------------------------------------------

class TestLegacyWrapper:
    def test_returns_plain_string_on_success(self, db_session):
        from includes.tools.supplier_quote_pipeline import _extract_email_content_sync
        t = _tracking(db_session, [_att("a.pdf", PDF_MIME)])
        with patch("includes.tools.supplier_quote_pipeline._get_session", return_value=db_session), \
             patch("includes.tools.supplier_quote_pipeline.fetch_gmail_attachment_bytes", return_value=b"x"), \
             patch("includes.tools.supplier_quote_pipeline.extract_pdf_content",
                   return_value=AttachmentExtraction("pdf text")):
            out = _extract_email_content_sync(t.id)
        assert isinstance(out, str)
        assert "pdf text" in out

    def test_legacy_error_sentinels_preserved(self, db_session):
        """Existing callers still prefix-sniff 'Error:'."""
        from includes.tools.supplier_quote_pipeline import _extract_email_content_sync
        t = _tracking(db_session, attachments=None, body=None)
        with patch("includes.tools.supplier_quote_pipeline._get_session", return_value=db_session), \
             patch("includes.tools.supplier_quote_pipeline._backfill_email_content_from_gmail",
                   return_value=False):
            out = _extract_email_content_sync(t.id)
        assert out.startswith("Error: No content found")

        with patch("includes.tools.supplier_quote_pipeline._get_session", return_value=db_session):
            out = _extract_email_content_sync(99_999_999)
        assert out == "Error: Email not found"
