"""Tests for includes/tools/rfq_creation_pipeline.py — RFQ creation from emails."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from includes.dashboard.models import Base, EmailTracking, Customer, RFQ


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session():
    """Create a test DB session using the project's PostgreSQL database.

    Uses a SAVEPOINT so that session.commit() inside sync helpers doesn't
    end the outer transaction — everything rolls back at the end.
    """
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


def _create_test_customer(session) -> Customer:
    """Create a minimal Customer record for testing."""
    import uuid
    cust = Customer(
        id=uuid.uuid4(),
        netsuite_id=f"TEST-{uuid.uuid4().hex[:8]}",
        companyname="Test Customer Corp",
        email="test@test.com",
    )
    session.add(cust)
    session.flush()
    return cust


def _create_test_email_tracking(session, customer_id=None, **overrides) -> EmailTracking:
    """Create a minimal EmailTracking record for testing."""
    import uuid
    defaults = {
        "gmail_thread_id": f"thread-{uuid.uuid4().hex[:8]}",
        "gmail_message_id": f"msg-{uuid.uuid4().hex[:8]}",
        "user_email": "test@eagle-exports.com",
        "direction": "received",
        "subject": "Test RFQ request",
        "sender_email": "customer@test.com",
    }
    defaults.update(overrides)
    tracking = EmailTracking(
        customer_id=customer_id,
        **defaults,
    )
    session.add(tracking)
    session.flush()
    return tracking


# ---------------------------------------------------------------------------
# _deduplicate_items
# ---------------------------------------------------------------------------

class TestDeduplicateItems:
    def test_empty_list(self):
        from includes.tools.rfq_creation_pipeline import _deduplicate_items
        assert _deduplicate_items([]) == []

    def test_no_duplicates(self):
        from includes.tools.rfq_creation_pipeline import _deduplicate_items
        items = [
            {"input_description": "M16 bolt", "input_code": "M16"},
            {"input_description": "M12 washer", "input_code": "M12"},
        ]
        result = _deduplicate_items(items)
        assert len(result) == 2

    def test_removes_duplicates(self):
        from includes.tools.rfq_creation_pipeline import _deduplicate_items
        items = [
            {"input_description": "M16 bolt", "input_code": "M16", "brand": ""},
            {"input_description": "M16 bolt", "input_code": "M16", "brand": "Grade 8.8"},
            {"input_description": "M12 washer", "input_code": "M12"},
        ]
        result = _deduplicate_items(items)
        assert len(result) == 2
        assert result[0]["input_description"] == "M16 bolt"
        assert result[1]["input_description"] == "M12 washer"

    def test_case_insensitive(self):
        from includes.tools.rfq_creation_pipeline import _deduplicate_items
        items = [
            {"input_description": "M16 BOLT", "input_code": "m16"},
            {"input_description": "m16 bolt", "input_code": "M16"},
        ]
        result = _deduplicate_items(items)
        assert len(result) == 1

    def test_whitespace_insensitive(self):
        from includes.tools.rfq_creation_pipeline import _deduplicate_items
        items = [
            {"input_description": "  M16 bolt  ", "input_code": "M16"},
            {"input_description": "M16 bolt", "input_code": "M16"},
        ]
        result = _deduplicate_items(items)
        assert len(result) == 1

    def test_different_code_same_desc_keeps_both(self):
        from includes.tools.rfq_creation_pipeline import _deduplicate_items
        items = [
            {"input_description": "M16 bolt", "input_code": "M16-8.8"},
            {"input_description": "M16 bolt", "input_code": "M16-10.9"},
        ]
        result = _deduplicate_items(items)
        assert len(result) == 2

    def test_same_code_different_desc_keeps_both(self):
        from includes.tools.rfq_creation_pipeline import _deduplicate_items
        items = [
            {"input_description": "M16 bolt grade 8.8", "input_code": "M16"},
            {"input_description": "M16 bolt grade 10.9", "input_code": "M16"},
        ]
        result = _deduplicate_items(items)
        assert len(result) == 2

    def test_missing_code_uses_brand_fallback(self):
        from includes.tools.rfq_creation_pipeline import _deduplicate_items
        items = [
            {"input_description": "Komatsu filter", "input_code": "", "brand": "Komatsu"},
            {"input_description": "Komatsu filter", "input_code": "", "brand": "Komatsu"},
        ]
        result = _deduplicate_items(items)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# _save_error / _save_rfq_creation_result
# ---------------------------------------------------------------------------

class TestSaveResult:
    def test_save_error_persists(self, db_session):
        """_save_error should write an error result to the DB."""
        cust = _create_test_customer(db_session)
        tracking = _create_test_email_tracking(db_session, customer_id=cust.id)

        from includes.tools.rfq_creation_pipeline import _save_error
        with patch("includes.tools.rfq_creation_pipeline._get_session", return_value=db_session):
            _save_error(tracking.id, "Test error message")

        db_session.expire_all()
        tracking = db_session.query(EmailTracking).filter(EmailTracking.id == tracking.id).first()
        assert tracking.rfq_creation_result is not None
        assert tracking.rfq_creation_result["status"] == "error"
        assert tracking.rfq_creation_result["error"] == "Test error message"
        assert "processed_at" in tracking.rfq_creation_result

    def test_save_rfq_creation_result_persists(self, db_session):
        """_save_rfq_creation_result should write a result to the DB."""
        cust = _create_test_customer(db_session)
        tracking = _create_test_email_tracking(db_session, customer_id=cust.id)

        result = {
            "rfq_number": "RFQ-2026-0999",
            "items_extracted": 3,
            "customer": "Test Customer Corp",
            "status": "complete",
            "extraction_method": "gemini_llm",
            "title": "Test RFQ",
            "customer_notes": "Urgent delivery",
            "raw_items": [{"input_description": "Bolt", "quantity": 10}],
            "warnings": [],
            "actions": ["Created RFQ with 3 items"],
            "processed_at": datetime.now(timezone.utc).isoformat(),
        }

        from includes.tools.rfq_creation_pipeline import _save_rfq_creation_result
        with patch("includes.tools.rfq_creation_pipeline._get_session", return_value=db_session):
            _save_rfq_creation_result(tracking.id, result)

        db_session.expire_all()
        tracking = db_session.query(EmailTracking).filter(EmailTracking.id == tracking.id).first()
        assert tracking.rfq_creation_result is not None
        assert tracking.rfq_creation_result["status"] == "complete"
        assert tracking.rfq_creation_result["rfq_number"] == "RFQ-2026-0999"
        assert tracking.rfq_creation_result["items_extracted"] == 3
        assert tracking.rfq_creation_result["title"] == "Test RFQ"

    def test_save_error_nonexistent_email(self, db_session):
        """_save_error should not crash for nonexistent email ID."""
        from includes.tools.rfq_creation_pipeline import _save_error
        # Should not raise — just log a warning
        with patch("includes.tools.rfq_creation_pipeline._get_session", return_value=db_session):
            _save_error(999999, "Test")


# ---------------------------------------------------------------------------
# Guard checks (via _run_rfq_creation_pipeline)
# ---------------------------------------------------------------------------

class TestGuardChecks:
    def test_no_customer_linked(self, db_session):
        """Pipeline should error if no customer is linked."""
        tracking = _create_test_email_tracking(db_session, customer_id=None)

        with patch("includes.tools.rfq_creation_pipeline._get_session", return_value=db_session):
            from includes.tools.rfq_creation_pipeline import _run_rfq_creation_pipeline
            _run_rfq_creation_pipeline(tracking.id, "test-user")

        db_session.expire_all()
        tracking = db_session.query(EmailTracking).filter(EmailTracking.id == tracking.id).first()
        assert tracking.rfq_creation_result is not None
        assert tracking.rfq_creation_result["status"] == "error"
        assert "No customer linked" in tracking.rfq_creation_result["error"]

    def test_already_linked_to_rfq(self, db_session):
        """Pipeline should error if email is already linked to an RFQ."""
        cust = _create_test_customer(db_session)
        tracking = _create_test_email_tracking(db_session, customer_id=cust.id,
                                                rfq_token="RFQ-2026-0001")

        with patch("includes.tools.rfq_creation_pipeline._get_session", return_value=db_session):
            from includes.tools.rfq_creation_pipeline import _run_rfq_creation_pipeline
            _run_rfq_creation_pipeline(tracking.id, "test-user")

        db_session.expire_all()
        tracking = db_session.query(EmailTracking).filter(EmailTracking.id == tracking.id).first()
        assert tracking.rfq_creation_result["status"] == "error"
        assert "already linked" in tracking.rfq_creation_result["error"].lower()

    def test_idempotency_already_processed(self, db_session):
        """Pipeline should skip if already processed."""
        cust = _create_test_customer(db_session)
        tracking = _create_test_email_tracking(db_session, customer_id=cust.id)
        tracking.rfq_creation_result = {
            "status": "complete",
            "rfq_number": "RFQ-2026-0001",
            "items_extracted": 2,
        }
        db_session.flush()

        with patch("includes.tools.rfq_creation_pipeline._get_session", return_value=db_session):
            from includes.tools.rfq_creation_pipeline import _run_rfq_creation_pipeline
            _run_rfq_creation_pipeline(tracking.id, "test-user")

        # Result should be unchanged (not overwritten)
        db_session.expire_all()
        tracking = db_session.query(EmailTracking).filter(EmailTracking.id == tracking.id).first()
        assert tracking.rfq_creation_result["status"] == "complete"
        assert tracking.rfq_creation_result["rfq_number"] == "RFQ-2026-0001"
        # Should NOT have created an RFQ
        rfqs = db_session.query(RFQ).filter(RFQ.customer == "Test Customer Corp").all()
        assert len(rfqs) == 0

    def test_proceeds_with_valid_conditions(self, db_session):
        """Pipeline should proceed when customer is linked and no existing RFQ."""
        cust = _create_test_customer(db_session)
        tracking = _create_test_email_tracking(db_session, customer_id=cust.id,
                                                gmail_thread_id="test-thread-001")

        # Mock _create_rfq_sync and _extract_rfq_items_sync to avoid heavy setup
        with patch("includes.tools.rfq_creation_pipeline._get_session", return_value=db_session), \
             patch("includes.tools.rfq_crud._create_rfq_sync") as mock_create, \
             patch("includes.tools.rfq_creation_pipeline._extract_rfq_items_sync") as mock_extract:

            mock_create.return_value = {"rfq_number": "RFQ-2026-0099"}
            mock_extract.return_value = ([], None)

            from includes.tools.rfq_creation_pipeline import _run_rfq_creation_pipeline
            _run_rfq_creation_pipeline(tracking.id, "test-user")

        # Should have called _create_rfq_sync (guard checks passed)
        mock_create.assert_called_once()
        # Should have attempted extraction
        mock_extract.assert_called_once_with(tracking.id)


# ---------------------------------------------------------------------------
# _now_iso / _now_dt
# ---------------------------------------------------------------------------

class TestTimeHelpers:
    def test_now_iso_returns_string(self):
        from includes.tools.rfq_creation_pipeline import _now_iso
        result = _now_iso()
        assert isinstance(result, str)
        assert "T" in result  # ISO format

    def test_now_dt_returns_datetime(self):
        from includes.tools.rfq_creation_pipeline import _now_dt
        result = _now_dt()
        assert isinstance(result, datetime)
