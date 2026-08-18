"""Tests for RFQ write serialization in includes/tools/rfq_crud.py.

Regression: concurrent tool calls in one agent turn (LangGraph gathers them)
ran RFQ CRUD helpers in parallel threads, deadlocking on Postgres row locks
and silently losing read-modify-write history updates. Writes are now
serialized per RFQ.
"""

import threading
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from includes.dashboard.models import Brand, RFQ
from includes.tools.rfq_crud import _rfq_write_lock, _serialized_rfq_write


class TestRfqWriteLock:
    def test_same_rfq_shares_lock(self):
        assert _rfq_write_lock("RFQ-1") is _rfq_write_lock("RFQ-1")

    def test_different_rfqs_have_different_locks(self):
        assert _rfq_write_lock("RFQ-A") is not _rfq_write_lock("RFQ-B")


class TestSerializedRfqWrite:
    def test_serializes_writes_for_same_rfq_but_not_different(self):
        active = {"RFQ-1": 0, "RFQ-2": 0}
        max_active = {"RFQ-1": 0, "RFQ-2": 0}
        guard = threading.Lock()

        @_serialized_rfq_write
        def fake_write(rfq_number, tag):
            with guard:
                active[rfq_number] += 1
                max_active[rfq_number] = max(max_active[rfq_number], active[rfq_number])
            time.sleep(0.03)
            with guard:
                active[rfq_number] -= 1
            return tag

        results = []
        errors = []

        def run(rfq_number, tag):
            try:
                results.append(fake_write(rfq_number, tag))
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=run, args=("RFQ-1", i)) for i in range(6)]
        threads += [threading.Thread(target=run, args=("RFQ-2", i)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # Same RFQ: never two concurrent writers
        assert max_active["RFQ-1"] == 1
        assert max_active["RFQ-2"] == 1
        assert sorted(results) == [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5]

    def test_decorator_returns_function_result(self):
        @_serialized_rfq_write
        def fake_write(rfq_number):
            return f"done-{rfq_number}"

        assert fake_write("RFQ-9") == "done-RFQ-9"


# ---------------------------------------------------------------------------
# Quote brand (custbodyquote_brand on the future NetSuite Quote)
# ---------------------------------------------------------------------------

class TestQuoteBrand:
    @pytest.fixture
    def db_session(self):
        """DB session with SAVEPOINT so commits inside helpers don't end the
        outer transaction — everything rolls back at the end."""
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

    def _make_brand(self, session) -> Brand:
        brand = Brand(
            netsuite_id=f"NS-{uuid.uuid4().hex[:8]}",
            name=f"Test Brand {uuid.uuid4().hex[:6]}",
        )
        session.add(brand)
        session.flush()
        return brand

    def _make_rfq(self, session) -> RFQ:
        rfq = RFQ(
            rfq_number=f"RFQ-2026-{uuid.uuid4().hex[:4].upper()}",
            customer="Test Customer",
            created_by="tester",
            created_date=datetime.now(timezone.utc),
        )
        session.add(rfq)
        session.flush()
        return rfq

    def test_set_quote_brand(self, db_session):
        brand = self._make_brand(db_session)
        rfq = self._make_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            from includes.tools.rfq_crud import _update_rfq_sync
            result = _update_rfq_sync(
                rfq.rfq_number,
                {"quote_brand_id": str(brand.id)},
                "tester",
            )

        assert not isinstance(result, str)
        assert result["quote_brand_id"] == str(brand.id)
        assert result["quote_brand"] == brand.name

        db_session.expire_all()
        stored = db_session.query(RFQ).filter(RFQ.rfq_number == rfq.rfq_number).first()
        assert stored.quote_brand_id == brand.id
        assert stored.quote_brand == brand.name

    def test_clear_quote_brand(self, db_session):
        brand = self._make_brand(db_session)
        rfq = self._make_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            from includes.tools.rfq_crud import _update_rfq_sync
            _update_rfq_sync(rfq.rfq_number, {"quote_brand_id": str(brand.id)}, "tester")
            result = _update_rfq_sync(rfq.rfq_number, {"quote_brand_id": ""}, "tester")

        assert result["quote_brand_id"] is None
        assert result["quote_brand"] == ""

        db_session.expire_all()
        stored = db_session.query(RFQ).filter(RFQ.rfq_number == rfq.rfq_number).first()
        assert stored.quote_brand_id is None
        assert stored.quote_brand is None

    def test_set_quote_brand_by_netsuite_id(self, db_session):
        brand = self._make_brand(db_session)
        rfq = self._make_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            from includes.tools.rfq_crud import _update_rfq_sync
            result = _update_rfq_sync(
                rfq.rfq_number,
                {"quote_brand_id": brand.netsuite_id},
                "tester",
            )

        assert not isinstance(result, str)
        assert result["quote_brand_id"] == str(brand.id)
        assert result["quote_brand"] == brand.name

    def test_set_quote_brand_by_exact_name_case_insensitive(self, db_session):
        brand = self._make_brand(db_session)
        rfq = self._make_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            from includes.tools.rfq_crud import _update_rfq_sync
            result = _update_rfq_sync(
                rfq.rfq_number,
                {"quote_brand": brand.name.lower()},
                "tester",
            )

        assert not isinstance(result, str)
        assert result["quote_brand_id"] == str(brand.id)
        assert result["quote_brand"] == brand.name  # canonical case

    def test_brand_name_requires_exact_match(self, db_session):
        brand = self._make_brand(db_session)
        rfq = self._make_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            from includes.tools.rfq_crud import _update_rfq_sync
            result = _update_rfq_sync(
                rfq.rfq_number,
                {"quote_brand": brand.name[:6]},  # partial — must not match
                "tester",
            )

        assert isinstance(result, str)
        assert "exact match is required" in result

    def test_unknown_netsuite_id_rejected(self, db_session):
        rfq = self._make_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            from includes.tools.rfq_crud import _update_rfq_sync
            result = _update_rfq_sync(
                rfq.rfq_number,
                {"quote_brand_id": "NS-DOES-NOT-EXIST"},
                "tester",
            )

        assert isinstance(result, str)
        assert "not found" in result

        db_session.expire_all()
        stored = db_session.query(RFQ).filter(RFQ.rfq_number == rfq.rfq_number).first()
        assert stored.quote_brand_id is None
        assert stored.quote_brand is None

    def test_unknown_brand_rejected(self, db_session):
        rfq = self._make_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            from includes.tools.rfq_crud import _update_rfq_sync
            result = _update_rfq_sync(
                rfq.rfq_number,
                {"quote_brand_id": str(uuid.uuid4())},
                "tester",
            )

        assert isinstance(result, str)
        assert "not found" in result

        db_session.expire_all()
        stored = db_session.query(RFQ).filter(RFQ.rfq_number == rfq.rfq_number).first()
        assert stored.quote_brand_id is None
        assert stored.quote_brand is None

    def test_unknown_netsuite_id_rejected_legacy_invalid_uuid(self, db_session):
        rfq = self._make_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            from includes.tools.rfq_crud import _update_rfq_sync
            result = _update_rfq_sync(
                rfq.rfq_number,
                {"quote_brand_id": "not-a-uuid"},
                "tester",
            )

        assert isinstance(result, str)
        assert "not found" in result
