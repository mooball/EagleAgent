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

from includes.dashboard.models import Brand, Product, RFQ, RFQItem
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
# RFQ-local supplier merge (suppliers tab)
# ---------------------------------------------------------------------------

class TestMergeRfqSuppliers:
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

    def _make_rfq(self, session, items_suppliers) -> RFQ:
        """items_suppliers: list of list-of-supplier-dicts per line."""
        rfq = RFQ(
            rfq_number=f"RFQ-2026-{uuid.uuid4().hex[:4].upper()}",
            customer="Test Customer",
            created_by="tester",
            created_date=datetime.now(timezone.utc),
        )
        session.add(rfq)
        session.flush()
        for line, sups in enumerate(items_suppliers, start=1):
            session.add(RFQItem(
                rfq_id=rfq.id,
                line=line,
                input_description=f"desc {line}",
                suppliers=[dict(s) for s in sups],
            ))
        session.flush()
        return rfq

    def _merge(self, db_session, rfq, keep, drops):
        from includes.tools.rfq_crud import _merge_rfq_suppliers_sync
        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            return _merge_rfq_suppliers_sync(rfq.rfq_number, keep, drops, "tester")

    def _line_suppliers(self, db_session, rfq, line):
        db_session.expire_all()
        item = db_session.query(RFQItem).filter(
            RFQItem.rfq_id == rfq.id, RFQItem.line == line
        ).first()
        return item.suppliers

    def test_replaces_dropped_names_and_preserves_order(self, db_session):
        rfq = self._make_rfq(db_session, [
            [{"name": "ABC Pty Ltd", "status": "shortlisted",
              "contacts": [{"email": "a@x.com"}], "quote_cost": 120},
             {"name": "Other Co", "status": "shortlisted"}],
            [{"name": "A.B.C. Pty Ltd", "status": "shortlisted",
              "notes": "web sourced", "contacts": [{"email": "w@x.com"}]}],
            [{"name": "Other Co", "status": "shortlisted"}],
        ])
        result = self._merge(db_session, rfq, "ABC Pty Ltd", ["A.B.C. Pty Ltd"])
        assert isinstance(result, dict)

        line1 = self._line_suppliers(db_session, rfq, 1)
        assert [s["name"] for s in line1] == ["ABC Pty Ltd", "Other Co"]  # order kept
        line2 = self._line_suppliers(db_session, rfq, 2)
        assert len(line2) == 1
        merged = line2[0]
        assert merged["name"] == "ABC Pty Ltd"
        assert merged["status"] == "shortlisted"
        assert merged["notes"] == "web sourced"
        assert {c["email"] for c in merged["contacts"]} == {"w@x.com", "a@x.com"}
        line3 = self._line_suppliers(db_session, rfq, 3)
        assert [s["name"] for s in line3] == ["Other Co"]

        rfq_row = db_session.query(RFQ).filter(RFQ.rfq_number == rfq.rfq_number).first()
        assert any("Merged suppliers" in h["action"] for h in (rfq_row.history or []))

    def test_does_not_copy_selection_status_across_lines(self, db_session):
        rfq = self._make_rfq(db_session, [
            [{"name": "ABC", "status": "shortlisted", "quote_status": "selected",
              "quote_cost": 100}],
            [{"name": "A.B.C.", "status": "shortlisted", "lead_time": 5}],
        ])
        self._merge(db_session, rfq, "ABC", ["A.B.C."])

        line1 = self._line_suppliers(db_session, rfq, 1)
        assert line1[0]["quote_status"] == "selected"
        line2 = self._line_suppliers(db_session, rfq, 2)
        assert line2[0]["name"] == "ABC"
        # Line 2's own data is preserved — line 1's selection is NOT copied across.
        assert line2[0].get("quote_status") is None
        assert line2[0]["lead_time"] == 5
        assert "quote_cost" not in line2[0] or line2[0].get("quote_cost") is None

    def test_collapses_duplicates_on_same_item(self, db_session):
        rfq = self._make_rfq(db_session, [
            [{"name": "ABC", "status": "shortlisted", "quote_status": "quoted",
              "quote_cost": 120},
             {"name": "A.B.C.", "status": "shortlisted", "quote_status": "shortlisted"},
             {"name": "Other Co", "status": "shortlisted"}],
        ])
        self._merge(db_session, rfq, "ABC", ["A.B.C."])

        line1 = self._line_suppliers(db_session, rfq, 1)
        assert [s["name"] for s in line1] == ["ABC", "Other Co"]
        assert line1[0]["quote_status"] == "quoted"  # strongest kept
        assert line1[0]["quote_cost"] == 120

    def test_prefers_db_linked_identity(self, db_session):
        linked_id = str(uuid.uuid4())
        rfq = self._make_rfq(db_session, [
            [{"name": "ABC", "status": "shortlisted", "supplier_id": linked_id,
              "db_match": "exact"}],
            [{"name": "A.B.C.", "status": "shortlisted", "db_match": "new"}],
        ])
        self._merge(db_session, rfq, "ABC", ["A.B.C."])

        line2 = self._line_suppliers(db_session, rfq, 2)
        assert line2[0]["name"] == "ABC"
        assert line2[0]["supplier_id"] == linked_id
        assert line2[0]["db_match"] == "exact"

    def test_errors(self, db_session):
        rfq = self._make_rfq(db_session, [
            [{"name": "ABC", "status": "shortlisted"}],
        ])
        assert isinstance(self._merge(db_session, rfq, "ABC", []), str)
        assert isinstance(self._merge(db_session, rfq, "ABC", ["ABC"]), str)
        assert isinstance(self._merge(db_session, rfq, "XYZ", ["ABC"]), str)
        assert isinstance(self._merge(db_session, rfq, "ABC", ["Not Here"]), str)


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

    def test_apply_validation_results_multi_brand_keeps_specific(self, db_session):
        from includes.tools.rfq_crud import _apply_validation_results

        rfq = self._make_rfq(db_session)
        item = RFQItem(rfq_id=rfq.id, line=1, input_description="V-belt",
                       part_number="B82", match="specific")
        db_session.add(item)
        db_session.flush()

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            _apply_validation_results([
                {"line": 1, "status": "multi_brand",
                 "findings": "B82 is a standard belt size (Gates, Bando, ...)."},
            ], rfq.rfq_number)

        db_session.expire_all()
        stored = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).first()
        assert stored.match == "specific"
        assert "standard belt size" in (stored.notes or "")

    def test_apply_validation_results_discrepancy_blocks(self, db_session):
        from includes.tools.rfq_crud import _apply_validation_results

        rfq = self._make_rfq(db_session)
        item = RFQItem(rfq_id=rfq.id, line=1, input_description="V-belt",
                       part_number="B82", match="specific")
        db_session.add(item)
        db_session.flush()

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            _apply_validation_results([
                {"line": 1, "status": "discrepancy",
                 "findings": "Part number looks wrong.",
                 "correct_part_number": "B84"},
            ], rfq.rfq_number)

        db_session.expire_all()
        stored = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).first()
        assert stored.match == "discrepancy"
        assert "B84" in (stored.notes or "")

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


# ---------------------------------------------------------------------------
# Quote brand auto-set from item brands (classify step, Step 2)
# ---------------------------------------------------------------------------

class TestAutoQuoteBrand:
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

    def _make_brand(self, session, name: str) -> Brand:
        brand = Brand(
            netsuite_id=f"NS-{uuid.uuid4().hex[:8]}",
            name=name,
        )
        session.add(brand)
        session.flush()
        return brand

    def _make_rfq_with_items(self, session, brands: list[str], **rfq_overrides) -> RFQ:
        rfq = RFQ(
            rfq_number=f"RFQ-2026-{uuid.uuid4().hex[:4].upper()}",
            customer="Test Customer",
            created_by="tester",
            created_date=datetime.now(timezone.utc),
            **rfq_overrides,
        )
        session.add(rfq)
        session.flush()
        for line, brand in enumerate(brands, start=1):
            session.add(RFQItem(
                rfq_id=rfq.id,
                line=line,
                input_description=f"desc {line}",
                brand=brand,
            ))
        session.flush()
        return rfq

    def test_majority_sets_quote_brand(self, db_session):
        # Unique names — a real synced brand named "Komatsu" now exists in the
        # dev DB and would otherwise win the exact-name lookup.
        suffix = uuid.uuid4().hex[:6]
        komatsu_name = f"Komatsu Test {suffix}"
        hitachi_name = f"Hitachi Test {suffix}"
        komatsu = self._make_brand(db_session, komatsu_name)
        self._make_brand(db_session, hitachi_name)
        rfq = self._make_rfq_with_items(
            db_session, [komatsu_name, komatsu_name, hitachi_name],
        )

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            from includes.tools.rfq_crud import _set_quote_brand_from_items_sync
            result = _set_quote_brand_from_items_sync(rfq.rfq_number, "tester")

        assert result is not None
        assert "Auto-set quote brand to" in result

        db_session.expire_all()
        stored = db_session.query(RFQ).filter(RFQ.rfq_number == rfq.rfq_number).first()
        assert stored.quote_brand_id == komatsu.id
        assert stored.quote_brand == komatsu_name
        assert any(
            "Auto-set quote brand to" in h.get("action", "")
            for h in (stored.history or [])
        )

    def test_tie_skips(self, db_session):
        self._make_brand(db_session, "Komatsu")
        self._make_brand(db_session, "Hitachi")
        rfq = self._make_rfq_with_items(
            db_session, ["Komatsu", "Komatsu", "Hitachi", "Hitachi"],
        )

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            from includes.tools.rfq_crud import _set_quote_brand_from_items_sync
            result = _set_quote_brand_from_items_sync(rfq.rfq_number, "tester")

        assert "tied" in result
        db_session.expire_all()
        stored = db_session.query(RFQ).filter(RFQ.rfq_number == rfq.rfq_number).first()
        assert stored.quote_brand_id is None
        assert stored.quote_brand is None

    def test_majority_not_in_db_skips(self, db_session):
        self._make_brand(db_session, "Komatsu")
        rfq = self._make_rfq_with_items(
            db_session, ["Acme Widgets", "Acme Widgets", "Komatsu"],
        )

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            from includes.tools.rfq_crud import _set_quote_brand_from_items_sync
            result = _set_quote_brand_from_items_sync(rfq.rfq_number, "tester")

        assert "not in the brands database" in result
        db_session.expire_all()
        stored = db_session.query(RFQ).filter(RFQ.rfq_number == rfq.rfq_number).first()
        assert stored.quote_brand_id is None
        assert stored.quote_brand is None

    def test_already_set_untouched(self, db_session):
        komatsu = self._make_brand(db_session, "Komatsu")
        self._make_brand(db_session, "Hitachi")
        rfq = self._make_rfq_with_items(
            db_session, ["Hitachi", "Hitachi", "Komatsu"],
            quote_brand_id=komatsu.id,
            quote_brand="Komatsu",
        )

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            from includes.tools.rfq_crud import _set_quote_brand_from_items_sync
            result = _set_quote_brand_from_items_sync(rfq.rfq_number, "tester")

        assert result is None
        db_session.expire_all()
        stored = db_session.query(RFQ).filter(RFQ.rfq_number == rfq.rfq_number).first()
        assert stored.quote_brand == "Komatsu"  # unchanged despite Hitachi majority

    def test_no_item_brands_skips(self, db_session):
        rfq = self._make_rfq_with_items(db_session, ["", None, "Other"])

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            from includes.tools.rfq_crud import _set_quote_brand_from_items_sync
            result = _set_quote_brand_from_items_sync(rfq.rfq_number, "tester")

        assert result is None
        db_session.expire_all()
        stored = db_session.query(RFQ).filter(RFQ.rfq_number == rfq.rfq_number).first()
        assert stored.quote_brand_id is None

    def test_classify_invokes_quote_brand_step(self, db_session):
        rfq = self._make_rfq_with_items(db_session, [None])  # one unmatched item

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session), \
             patch("includes.tools.rfq_crud._set_quote_brand_from_items_sync",
                   return_value="wired") as mock_set_brand, \
             patch("includes.tools.rfq_crud._set_item_departments_sync",
                   return_value="depts wired") as mock_set_depts:

            from includes.tools.rfq_crud import _classify_rfq_items_sync
            result = _classify_rfq_items_sync(rfq.rfq_number, "tester", search_db=False)

        mock_set_brand.assert_called_once_with(rfq.rfq_number, "tester")
        mock_set_depts.assert_called_once_with(rfq.rfq_number, "tester")
        assert result["quote_brand_result"] == "wired"
        assert result["department_result"] == "depts wired"

    def test_classify_brand_near_alternatives_reported(self, db_session):
        suffix = uuid.uuid4().hex[:6]
        self._make_brand(db_session, f"Toyzz Parts {suffix}")
        self._make_brand(db_session, f"Toyzz Industrial {suffix}")
        rfq = self._make_rfq_with_items(db_session, ["toyzz"])

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session), \
             patch("includes.tools.product_tools.get_session", return_value=db_session), \
             patch("includes.tools.rfq_crud._set_quote_brand_from_items_sync", return_value="ok"), \
             patch("includes.tools.rfq_crud._set_item_departments_sync", return_value="ok"):
            from includes.tools.rfq_crud import _classify_rfq_items_sync
            result = _classify_rfq_items_sync(rfq.rfq_number, "tester", search_db=False)

        near = [b for b in result["brand_results"] if b["status"] == "near"]
        assert len(near) == 1
        assert near[0]["input"] == "toyzz"
        assert set(near[0]["alternatives"]) == {f"Toyzz Parts {suffix}", f"Toyzz Industrial {suffix}"}

        db_session.expire_all()
        stored = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).first()
        assert stored.brand == "toyzz"  # unchanged — no exact match

    def test_classify_brand_exact_canonicalised(self, db_session):
        suffix = uuid.uuid4().hex[:6]
        self._make_brand(db_session, f"Toyzz Parts {suffix}")
        self._make_brand(db_session, f"Toyzz Parts {suffix} Industrial")
        rfq = self._make_rfq_with_items(db_session, [f"Toyzz-Parts {suffix}"])

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session), \
             patch("includes.tools.product_tools.get_session", return_value=db_session), \
             patch("includes.tools.rfq_crud._set_quote_brand_from_items_sync", return_value="ok"), \
             patch("includes.tools.rfq_crud._set_item_departments_sync", return_value="ok"):
            from includes.tools.rfq_crud import _classify_rfq_items_sync
            result = _classify_rfq_items_sync(rfq.rfq_number, "tester", search_db=False)

        exact = [b for b in result["brand_results"] if b["status"] == "exact"]
        assert len(exact) == 1
        assert exact[0]["brand"] == f"Toyzz Parts {suffix}"
        assert f"Toyzz Parts {suffix} Industrial" in exact[0]["alternatives"]

        db_session.expire_all()
        stored = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).first()
        assert stored.brand == f"Toyzz Parts {suffix}"


# ---------------------------------------------------------------------------
# Item departments (Phase 3) — store / edit / validate against the enum
# ---------------------------------------------------------------------------

class TestItemDepartment:
    """Department on RFQ line items: dict plumbing and CRUD validation."""

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

    def _make_rfq(self, session, with_items=1) -> RFQ:
        rfq = RFQ(
            rfq_number=f"RFQ-2026-{uuid.uuid4().hex[:4].upper()}",
            customer="Test Customer",
            created_by="tester",
            created_date=datetime.now(timezone.utc),
        )
        session.add(rfq)
        session.flush()
        for line in range(1, with_items + 1):
            session.add(RFQItem(
                rfq_id=rfq.id, line=line,
                input_description=f"Item {line}",
            ))
        session.flush()
        return rfq

    # -- Dict plumbing --------------------------------------------------

    def test_item_to_dict_surfaces_department(self):
        from includes.tools.rfq_crud import _item_to_dict
        item = RFQItem(line=1, input_description="Filter", department_id="5")
        out = _item_to_dict(item)
        assert out["department_id"] == "5"
        assert out["department"] == "Truck Parts"

    def test_item_to_dict_unknown_id_keeps_id_but_no_label(self):
        from includes.tools.rfq_crud import _item_to_dict
        item = RFQItem(line=1, input_description="Filter", department_id="999")
        out = _item_to_dict(item)
        assert out["department_id"] == "999"
        assert out["department"] is None

    def test_item_to_dict_no_department(self):
        from includes.tools.rfq_crud import _item_to_dict
        item = RFQItem(line=1, input_description="Filter")
        out = _item_to_dict(item)
        assert out["department_id"] is None
        assert out["department"] is None

    # -- Department resolution helper -------------------------------------

    def test_resolve_department_by_id(self):
        from includes.tools.rfq_crud import _resolve_department
        assert _resolve_department({"department_id": "5"}) == "5"
        assert _resolve_department({"department_id": 11}) == "11"

    def test_resolve_department_by_label_case_insensitive(self):
        from includes.tools.rfq_crud import _resolve_department
        assert _resolve_department({"department": "truck parts"}) == "5"
        assert _resolve_department({"department": "Tyres"}) == "7"

    def test_resolve_department_empty_clears(self):
        from includes.tools.rfq_crud import _resolve_department
        assert _resolve_department({"department_id": ""}) is None
        assert _resolve_department({"department": ""}) is None
        assert _resolve_department({}) is None

    def test_resolve_department_unknown_label_raises(self):
        from includes.tools.rfq_crud import _resolve_department
        with pytest.raises(ValueError, match="Invalid department"):
            _resolve_department({"department": "Aerospace"})

    def test_resolve_department_conflict_raises(self):
        from includes.tools.rfq_crud import _resolve_department
        with pytest.raises(ValueError, match="Conflicting"):
            _resolve_department({"department_id": "5", "department": "Tyres"})

    def test_resolve_department_matching_id_and_label_ok(self):
        from includes.tools.rfq_crud import _resolve_department
        assert _resolve_department({"department_id": "5", "department": "Truck Parts"}) == "5"

    # -- Single item update --------------------------------------------

    def test_update_item_sets_department_by_label(self, db_session):
        from includes.tools.rfq_crud import _update_item_sync
        rfq = self._make_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = _update_item_sync(rfq.rfq_number, {"line": 1, "department": "Tyres"}, "tester")

        assert not isinstance(result, str)
        assert result["items"][0]["department"] == "Tyres"
        assert result["items"][0]["department_id"] == "7"

    def test_update_item_sets_valid_department(self, db_session):
        from includes.tools.rfq_crud import _update_item_sync
        rfq = self._make_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = _update_item_sync(rfq.rfq_number, {"line": 1, "department_id": "9"}, "tester")

        assert not isinstance(result, str)
        item = result["items"][0]
        assert item["department_id"] == "9"
        assert item["department"] == "4WD Parts"

        db_session.expire_all()
        stored = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id, RFQItem.line == 1).first()
        assert stored.department_id == "9"

    def test_update_item_clears_department(self, db_session):
        from includes.tools.rfq_crud import _update_item_sync
        rfq = self._make_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            _update_item_sync(rfq.rfq_number, {"line": 1, "department_id": "5"}, "tester")
            result = _update_item_sync(rfq.rfq_number, {"line": 1, "department_id": ""}, "tester")

        assert not isinstance(result, str)
        assert result["items"][0]["department_id"] is None

    def test_update_item_rejects_invalid_department(self, db_session):
        from includes.tools.rfq_crud import _update_item_sync
        rfq = self._make_rfq(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            # Commit a valid department first so we can prove the invalid
            # update leaves it untouched (the error path rolls back the
            # transaction, so an uncommitted baseline would be lost).
            _update_item_sync(rfq.rfq_number, {"line": 1, "department_id": "5"}, "tester")
            result = _update_item_sync(rfq.rfq_number, {"line": 1, "department_id": "999"}, "tester")

        assert isinstance(result, str)
        assert "Invalid department_id" in result
        db_session.expire_all()
        stored = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id, RFQItem.line == 1).first()
        assert stored.department_id == "5"  # unchanged

    # -- Bulk update ----------------------------------------------------

    def test_bulk_update_skips_invalid_department(self, db_session):
        from includes.tools.rfq_crud import _update_items_bulk_sync
        rfq = self._make_rfq(db_session, with_items=2)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = _update_items_bulk_sync(rfq.rfq_number, {
                "items": [
                    {"line": 1, "department_id": "5"},
                    {"line": 2, "department_id": "999"},
                ],
            }, "tester")

        assert not isinstance(result, str)
        by_line = {i["line"]: i for i in result["items"]}
        assert by_line[1]["department"] == "Truck Parts"
        assert by_line[2]["department_id"] is None
        history_actions = " | ".join(h["action"] for h in result["history"])
        assert "Skipped line 2" in history_actions

    # -- Add items ------------------------------------------------------

    def test_add_items_stores_valid_department(self, db_session):
        from includes.tools.rfq_crud import _add_items_sync
        rfq = self._make_rfq(db_session, with_items=0)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = _add_items_sync(rfq.rfq_number, {
                "items": [
                    {"input_description": "Brake pads", "department_id": "5"},
                    {"input_description": "No department"},
                ],
            }, "tester")

        assert not isinstance(result, str)
        by_line = {i["line"]: i for i in result["items"]}
        assert by_line[1]["department"] == "Truck Parts"
        assert by_line[2]["department_id"] is None

    def test_add_items_stores_department_by_label(self, db_session):
        from includes.tools.rfq_crud import _add_items_sync
        rfq = self._make_rfq(db_session, with_items=0)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = _add_items_sync(rfq.rfq_number, {
                "items": [{"input_description": "Radial tyres", "department": "tyres"}],
            }, "tester")

        assert not isinstance(result, str)
        item = result["items"][0]
        assert item["department_id"] == "7"
        assert item["department"] == "Tyres"

    def test_add_items_rejects_invalid_department(self, db_session):
        from includes.tools.rfq_crud import _add_items_sync
        rfq = self._make_rfq(db_session, with_items=0)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            # Commit one valid item first; the invalid call rolls back, so we
            # prove nothing extra was added by comparing against it.
            _add_items_sync(rfq.rfq_number, {"items": [{"input_description": "Kept"}]}, "tester")
            result = _add_items_sync(rfq.rfq_number, {
                "items": [{"input_description": "Mystery part", "department_id": "999"}],
            }, "tester")

        assert isinstance(result, str)
        assert "invalid department" in result.lower()
        assert "999" in result
        db_session.expire_all()
        stored = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).all()
        assert len(stored) == 1  # nothing extra added


# ---------------------------------------------------------------------------
# Item department auto-set (Phase 5) — product match → batched LLM fallback
# ---------------------------------------------------------------------------

class TestItemDepartmentAutoSet:
    """`_set_item_departments_sync`: copy from products, then one LLM call."""

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

    def _make_product(self, session, department_id=None) -> Product:
        from includes.dashboard.models import Product
        product = Product(
            netsuite_id=f"NS-{uuid.uuid4().hex[:8]}",
            part_number=f"PN-{uuid.uuid4().hex[:6]}",
            description="Test product",
            department_id=department_id,
        )
        session.add(product)
        session.flush()
        return product

    def _make_rfq_with_products(self, session, specs) -> RFQ:
        """specs: list of {product_id, department_id} per line."""
        rfq = RFQ(
            rfq_number=f"RFQ-2026-{uuid.uuid4().hex[:4].upper()}",
            customer="Test Customer",
            created_by="tester",
            created_date=datetime.now(timezone.utc),
        )
        session.add(rfq)
        session.flush()
        for line, spec in enumerate(specs, start=1):
            session.add(RFQItem(
                rfq_id=rfq.id, line=line,
                input_description=f"desc {line}",
                product_id=spec.get("product_id"),
                department_id=spec.get("department_id"),
            ))
        session.flush()
        return rfq

    # -- fakes for the genai client --------------------------------------

    class _FakeResponse:
        def __init__(self, text):
            self.text = text

    class _FakeModels:
        def __init__(self, text):
            self._text = text

        def generate_content(self, **kwargs):
            return TestItemDepartmentAutoSet._FakeResponse(self._text)

    class _FakeClient:
        def __init__(self, text, **kwargs):
            self.models = TestItemDepartmentAutoSet._FakeModels(text)

    class _BoomClient:
        def __init__(self, **kwargs):
            raise RuntimeError("LLM should not have been called")

    # -- product match ---------------------------------------------------

    def test_product_match_copies_department(self, db_session, monkeypatch):
        from includes.tools.rfq_crud import _set_item_departments_sync
        product = self._make_product(db_session, department_id="5")
        rfq = self._make_rfq_with_products(db_session, [{"product_id": product.id}])

        monkeypatch.setattr("google.genai.Client", self._BoomClient)  # no LLM needed

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = _set_item_departments_sync(rfq.rfq_number, "tester")

        assert result is not None and "product match" in result
        db_session.expire_all()
        item = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id, RFQItem.line == 1).first()
        assert item.department_id == "5"

    def test_existing_department_never_overwritten(self, db_session, monkeypatch):
        from includes.tools.rfq_crud import _set_item_departments_sync
        product = self._make_product(db_session, department_id="5")
        rfq = self._make_rfq_with_products(
            db_session, [{"product_id": product.id, "department_id": "7"}],
        )

        monkeypatch.setattr("google.genai.Client", self._BoomClient)  # no LLM needed

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = _set_item_departments_sync(rfq.rfq_number, "tester")

        assert result is None  # nothing changed
        db_session.expire_all()
        item = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id, RFQItem.line == 1).first()
        assert item.department_id == "7"  # untouched

    def test_product_without_department_falls_through_to_llm(self, db_session, monkeypatch):
        from includes.tools.rfq_crud import _set_item_departments_sync
        product = self._make_product(db_session, department_id=None)
        rfq = self._make_rfq_with_products(db_session, [{"product_id": product.id}])

        monkeypatch.setattr(
            "google.genai.Client",
            lambda **kw: TestItemDepartmentAutoSet._FakeClient(
                '{"departments": {"1": "5"}}', **kw,
            ),
        )

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = _set_item_departments_sync(rfq.rfq_number, "tester")

        assert "1 by LLM" in result
        db_session.expire_all()
        item = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id, RFQItem.line == 1).first()
        assert item.department_id == "5"

    # -- LLM fallback ----------------------------------------------------

    def test_llm_assignments_strictly_validated(self, db_session, monkeypatch):
        from includes.tools.rfq_crud import _set_item_departments_sync
        rfq = self._make_rfq_with_products(db_session, [{}, {}, {}])  # 3 items, no products

        monkeypatch.setattr(
            "google.genai.Client",
            lambda **kw: TestItemDepartmentAutoSet._FakeClient(
                # line 2 unknown ID, line 4 not in input — both must be skipped
                '{"departments": {"1": "5", "2": "999", "4": "7"}}', **kw,
            ),
        )

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = _set_item_departments_sync(rfq.rfq_number, "tester")

        assert "1 by LLM" in result
        assert "2 skipped" in result
        db_session.expire_all()
        items = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).order_by(RFQItem.line).all()
        assert items[0].department_id == "5"
        assert items[1].department_id is None
        assert items[2].department_id is None

    def test_llm_omitted_lines_left_empty(self, db_session, monkeypatch):
        from includes.tools.rfq_crud import _set_item_departments_sync
        rfq = self._make_rfq_with_products(db_session, [{}, {}, {}])

        monkeypatch.setattr(
            "google.genai.Client",
            lambda **kw: TestItemDepartmentAutoSet._FakeClient(
                '{"departments": {"1": "7"}}', **kw,  # only line 1 answered
            ),
        )

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = _set_item_departments_sync(rfq.rfq_number, "tester")

        assert "1 by LLM" in result
        db_session.expire_all()
        items = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).order_by(RFQItem.line).all()
        assert items[0].department_id == "7"
        assert items[1].department_id is None
        assert items[2].department_id is None

    def test_llm_failure_reported(self, db_session, monkeypatch):
        from includes.tools.rfq_crud import _set_item_departments_sync
        rfq = self._make_rfq_with_products(db_session, [{}])

        def _boom(**kwargs):
            raise RuntimeError("vertex down")

        monkeypatch.setattr("google.genai.Client", _boom)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = _set_item_departments_sync(rfq.rfq_number, "tester")

        assert isinstance(result, str)
        assert "Department LLM call failed" in result

    def test_llm_bad_json_reported(self, db_session, monkeypatch):
        from includes.tools.rfq_crud import _set_item_departments_sync
        rfq = self._make_rfq_with_products(db_session, [{}])

        monkeypatch.setattr(
            "google.genai.Client",
            lambda **kw: TestItemDepartmentAutoSet._FakeClient("not json at all", **kw),
        )

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = _set_item_departments_sync(rfq.rfq_number, "tester")

        assert isinstance(result, str)
        assert "Failed to parse department LLM response" in result

    def test_all_departments_set_skips_llm(self, db_session, monkeypatch):
        from includes.tools.rfq_crud import _set_item_departments_sync
        rfq = self._make_rfq_with_products(db_session, [{"department_id": "5"}])

        monkeypatch.setattr("google.genai.Client", self._BoomClient)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = _set_item_departments_sync(rfq.rfq_number, "tester")

        assert result is None


class TestSwapSupplier:
    """'Use instead' — replace a near-miss supplier on an RFQ line."""

    @pytest.fixture
    def db_session(self):
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

    def _setup(self, db_session):
        from includes.dashboard.models import RFQ, RFQItem, Supplier, Contact
        rfq = RFQ(
            rfq_number=f"RFQ-2026-{uuid.uuid4().hex[:4].upper()}",
            customer="Test Customer", created_by="tester",
            created_date=datetime.now(timezone.utc),
        )
        db_session.add(rfq)
        db_session.flush()

        primary = Supplier(name="TNT Express", netsuite_id="NS-6819", source="netsuite")
        web = Supplier(name="TNT Express (ZZ Test)", netsuite_id=None, source="web")
        db_session.add_all([primary, web])
        db_session.flush()
        db_session.add(Contact(
            supplier_id=primary.id, label="Main", fullname="Jane",
            email="jane@tnt.com", isinactive=False,
        ))
        db_session.flush()

        item = RFQItem(
            rfq_id=rfq.id, line=1, input_description="Cutterhead",
            suppliers=[{
                "supplier_id": str(web.id),
                "name": web.name,
                "db_match": "near_miss",
                "near_miss_names": ["TNT Express"],
                "status": "shortlisted",
                "quote_cost": 123.45,
                "quote_status": "quoted",
                "notes": "keep me",
            }],
        )
        db_session.add(item)
        db_session.flush()
        return rfq, item, primary, web

    def test_swap_replaces_entry_and_preserves_quote(self, db_session):
        from includes.tools.rfq_crud import _swap_supplier_sync
        rfq, item, primary, web = self._setup(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = _swap_supplier_sync(rfq.rfq_number, {
                "line": 1, "from_id": str(web.id), "to_id": str(primary.id),
            }, "tester")

        assert isinstance(result, dict)
        suppliers = result["items"][0]["suppliers"]
        assert len(suppliers) == 1
        s = suppliers[0]
        assert s["name"] == "TNT Express"
        assert s["supplier_id"] == str(primary.id)
        assert s["db_match"] == "exact"
        assert s["quote_cost"] == 123.45
        assert s["quote_status"] == "quoted"
        assert s["notes"] == "keep me"
        assert s["status"] == "shortlisted"
        assert "jane@tnt.com" in str(s.get("contacts"))
        # the flagged web record itself is untouched
        assert web.id is not None

    def test_swap_missing_target_errors(self, db_session):
        from includes.tools.rfq_crud import _swap_supplier_sync
        rfq, item, primary, web = self._setup(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = _swap_supplier_sync(rfq.rfq_number, {
                "line": 1, "from_id": str(web.id), "to_id": str(uuid.uuid4()),
            }, "tester")

        assert isinstance(result, str)
        assert "replacement supplier not found" in result

    def test_swap_missing_from_id_errors(self, db_session):
        from includes.tools.rfq_crud import _swap_supplier_sync
        rfq, item, primary, web = self._setup(db_session)

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = _swap_supplier_sync(rfq.rfq_number, {
                "line": 1, "from_id": str(uuid.uuid4()), "to_id": str(primary.id),
            }, "tester")

        assert isinstance(result, str)
        assert "supplier not found on this line" in result

    def test_db_linked_flagged_supplier_gets_near_miss(self, db_session):
        from includes.tools.rfq_crud import _add_supplier_sync
        from includes.dashboard.models import SupplierDuplicateCandidate
        rfq, item, primary, web = self._setup(db_session)
        db_session.add(SupplierDuplicateCandidate(
            primary_id=primary.id, duplicate_id=web.id,
            source="auto", status="proposed", confidence=0.7,
            reasons=["domain_mismatch"],
        ))
        db_session.flush()

        with patch("includes.tools.rfq_crud._get_session", return_value=db_session):
            result = _add_supplier_sync(rfq.rfq_number, {"line": 1, "suppliers": [
                {"name": web.name, "supplier_id": str(web.id)},
            ]}, "tester")

        assert isinstance(result, dict)
        entry = [s for s in result["items"][0]["suppliers"] if s["name"] == web.name][0]
        assert entry["db_match"] == "near_miss"
        assert entry["near_miss_names"] == ["TNT Express"]


