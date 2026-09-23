"""Regression tests: ``rfq_items.suppliers`` entries whose ``supplier_id``
does not resolve.

Context — the 2026-09-23 production incident:

``rfq_items.suppliers`` is JSONB with no foreign key, so an element can carry a
``supplier_id`` that never existed. The ProcurementAgent can invent UUID-shaped
ids — they pass ``_is_valid_uuid_id`` (a *shape* check) and were then persisted
as if they were real DB links. Four RFQs ended up holding such ids; when one was
marked ``quote_status='selected'``, ``_rfq_sync_readiness`` dereferenced the
(found to be ``None``) supplier row and the whole RFQ detail page 500'd for
every user, on every tab.

Three layers are covered here:
  A. ``_rfq_sync_readiness`` tolerates a dangling id and flags it.
  B. ``_add_suppliers_to_line_core`` drops a non-existent id and matches by name.
  C. ``_select_quote_core`` refuses to select a supplier with a broken link.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from includes.dashboard.models import RFQ, RFQItem, Supplier

# A syntactically valid UUID that exists nowhere in the database.
BOGUS_ID = "5f448455-89f8-4e33-ae3f-36fa4429e64e"


@pytest.fixture
def db_session():
    """DB session with SAVEPOINT so commits inside helpers don't end the outer
    transaction — everything rolls back at the end."""
    from includes.dashboard.database import _sync_url

    engine = create_engine(_sync_url(), pool_pre_ping=True)
    connection = engine.connect()
    connection.begin()
    session = sessionmaker(bind=connection)(bind=connection)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess, trans):
        if trans.nested and not trans._parent.nested:
            sess.begin_nested()

    session.close = lambda: None  # type: ignore[method-assign]
    yield session
    session.rollback()
    connection.close()
    engine.dispose()


def _unique_rfq_number() -> str:
    """Non-hex suffix — hex suffixes collide with real RFQ numbers.

    ``RFQ-2026-{uuid4().hex[:4]}`` only has 65536 values and numeric suffixes
    are valid hex, so it clashes with live rows (see the flaky-test notes).
    """
    return f"RFQ-2026-T{uuid.uuid4().hex[:6].upper()}"


def _make_supplier(session, name: str) -> Supplier:
    sup = Supplier(name=name, source="test")
    session.add(sup)
    session.flush()
    return sup


def _make_rfq(session, suppliers_by_line: dict[int, list[dict]]) -> RFQ:
    rfq = RFQ(
        rfq_number=_unique_rfq_number(),
        customer="Test Customer",
        created_by="tester",
        created_date=datetime.now(timezone.utc),
    )
    session.add(rfq)
    session.flush()
    for line, sups in suppliers_by_line.items():
        session.add(RFQItem(
            rfq_id=rfq.id,
            line=line,
            input_description=f"desc {line}",
            part_number=f"PN-{line}",
            suppliers=[dict(s) for s in sups],
        ))
    session.flush()
    return rfq


def _line_item(session, rfq, line: int) -> RFQItem:
    return session.query(RFQItem).filter(
        RFQItem.rfq_id == rfq.id, RFQItem.line == line
    ).first()


# ---------------------------------------------------------------------------
# The shared helper
# ---------------------------------------------------------------------------

class TestExistingSupplierIds:
    def test_returns_only_ids_that_resolve(self, db_session):
        from includes.dashboard.supplier_dedup import existing_supplier_ids

        real = _make_supplier(db_session, "Integrity Test Supplier A")
        found = existing_supplier_ids(db_session, [str(real.id), BOGUS_ID])
        assert found == {str(real.id)}

    def test_tolerates_malformed_and_empty_values(self, db_session):
        from includes.dashboard.supplier_dedup import existing_supplier_ids

        assert existing_supplier_ids(db_session, []) == set()
        assert existing_supplier_ids(db_session, [None, ""]) == set()
        # "sup_1597" is not a UUID — must be dropped, not raise a cast error.
        assert existing_supplier_ids(db_session, ["sup_1597", "926"]) == set()

    def test_normalises_uuid_formatting(self, db_session):
        from includes.dashboard.supplier_dedup import existing_supplier_ids

        real = _make_supplier(db_session, "Integrity Test Supplier B")
        assert existing_supplier_ids(db_session, [str(real.id).upper()]) == {str(real.id)}


# ---------------------------------------------------------------------------
# Layer B — never persist an unverified id
# ---------------------------------------------------------------------------

class TestAddSuppliersDropsUnverifiedIds:
    def _add(self, db_session, rfq, line, data, resolved_name_id):
        """Run _add_suppliers_to_line_core with name-matching stubbed out.

        _match_suppliers_to_db opens its own (non-savepoint) session and will
        CREATE a supplier for an unknown name, so it must never be exercised
        for real here.
        """

        def fake_match(suppliers, product_hint=""):
            for sup in suppliers:
                sup["supplier_id"] = str(resolved_name_id)
                sup["db_match"] = "exact"

        with patch("includes.tools.quote_tools._match_suppliers_to_db", fake_match):
            from includes.tools.rfq_crud import _add_suppliers_to_line_core

            item = _line_item(db_session, rfq, line)
            return _add_suppliers_to_line_core(db_session, rfq, item, data)

    def test_nonexistent_uuid_is_dropped_and_matched_by_name(self, db_session):
        real = _make_supplier(db_session, "Name Matched Supplier")
        rfq = _make_rfq(db_session, {1: []})

        added, updated, skipped, error = self._add(
            db_session, rfq, 1,
            {"line": 1, "suppliers": [
                {"name": "Name Matched Supplier", "supplier_id": BOGUS_ID},
            ]},
            real.id,
        )

        assert error is None
        assert added == ["Name Matched Supplier"]
        # No expire_all(): the helper deliberately does not commit.
        stored = _line_item(db_session, rfq, 1).suppliers
        assert stored[0]["supplier_id"] == str(real.id)
        assert stored[0]["supplier_id"] != BOGUS_ID

    def test_real_uuid_is_preserved(self, db_session):
        real = _make_supplier(db_session, "Genuinely Linked Supplier")
        rfq = _make_rfq(db_session, {1: []})

        def fail_match(suppliers, product_hint=""):
            raise AssertionError("name matching should not run for a valid id")

        with patch("includes.tools.quote_tools._match_suppliers_to_db", fail_match):
            from includes.tools.rfq_crud import _add_suppliers_to_line_core

            item = _line_item(db_session, rfq, 1)
            added, _, _, error = _add_suppliers_to_line_core(
                db_session, rfq, item,
                {"line": 1, "suppliers": [
                    {"name": "Genuinely Linked Supplier", "supplier_id": str(real.id)},
                ]},
            )

        assert error is None
        assert added == ["Genuinely Linked Supplier"]
        stored = _line_item(db_session, rfq, 1).suppliers
        assert stored[0]["supplier_id"] == str(real.id)

    def test_malformed_id_still_dropped(self, db_session):
        """The original 2026-09-07 guard must keep working."""
        real = _make_supplier(db_session, "Malformed Id Supplier")
        rfq = _make_rfq(db_session, {1: []})

        added, _, _, error = self._add(
            db_session, rfq, 1,
            {"line": 1, "suppliers": [
                {"name": "Malformed Id Supplier", "supplier_id": "sup_1597"},
            ]},
            real.id,
        )

        assert error is None
        assert added == ["Malformed Id Supplier"]
        stored = _line_item(db_session, rfq, 1).suppliers
        assert stored[0]["supplier_id"] == str(real.id)


# ---------------------------------------------------------------------------
# Layer C — refuse to select a supplier with a broken link
# ---------------------------------------------------------------------------

class TestSelectQuoteRefusesBrokenLink:
    @staticmethod
    def _select(db_session, rfq, line, name):
        from includes.tools.rfq_crud import _select_quote_core

        return _select_quote_core(
            db_session, rfq, _line_item(db_session, rfq, line), {"name": name}
        )

    def test_selecting_dangling_supplier_errors_and_changes_nothing(self, db_session):
        rfq = _make_rfq(db_session, {1: [
            {"name": "Broken Link Co", "status": "shortlisted",
             "supplier_id": BOGUS_ID, "quote_status": "quoted", "quote_cost": 10},
        ]})

        action, selected = self._select(db_session, rfq, 1, "Broken Link Co")

        assert action.startswith("Error:"), action
        assert "broken" in action
        assert selected is None
        assert _line_item(db_session, rfq, 1).suppliers[0]["quote_status"] == "quoted"

    def test_deselecting_broken_supplier_is_still_allowed(self, db_session):
        """A dangling id that is already selected must be undoable."""
        rfq = _make_rfq(db_session, {1: [
            {"name": "Stuck Co", "status": "shortlisted",
             "supplier_id": BOGUS_ID, "quote_status": "selected"},
        ]})

        action, _ = self._select(db_session, rfq, 1, "Stuck Co")

        assert action.startswith("Deselected"), action
        assert _line_item(db_session, rfq, 1).suppliers[0]["quote_status"] == "quoted"

    def test_selecting_supplier_without_any_id_is_allowed(self, db_session):
        rfq = _make_rfq(db_session, {1: [
            {"name": "No Id Co", "status": "shortlisted",
             "quote_status": "quoted", "quote_cost": 12},
        ]})

        action, _ = self._select(db_session, rfq, 1, "No Id Co")

        assert action.startswith("Selected"), action
        assert _line_item(db_session, rfq, 1).suppliers[0]["quote_status"] == "selected"


# ---------------------------------------------------------------------------
# Layer A — the 500 regression itself
# ---------------------------------------------------------------------------

class TestSyncReadinessToleratesDanglingId:
    @staticmethod
    def _readiness(rfq_dict, session):
        from includes.dashboard.routes import _helpers
        from includes.dashboard.routes.rfqs import _rfq_sync_readiness

        with patch.object(_helpers, "get_session", return_value=session):
            return _rfq_sync_readiness(rfq_dict)

    def test_selected_dangling_supplier_does_not_raise(self, db_session):
        """Reproduces the RFQ-2026-2111 crash: this used to raise
        AttributeError: 'NoneType' object has no attribute 'name'."""
        rfq = _make_rfq(db_session, {1: [
            {"name": "Total Tools Coopers Plains", "status": "shortlisted",
             "supplier_id": BOGUS_ID, "quote_status": "selected"},
        ]})
        rfq_dict = {
            "id": rfq.rfq_number,
            "items": [{
                "line": 1,
                "brand": "",
                "part_number": "PN-1",
                "product_id": None,
                "suppliers": [{
                    "name": "Total Tools Coopers Plains",
                    "status": "shortlisted",
                    "supplier_id": BOGUS_ID,
                    "quote_status": "selected",
                }],
            }],
        }

        ctx = self._readiness(rfq_dict, db_session)  # must not raise

        selected = rfq_dict["items"][0]["selected_supplier"]
        assert selected["supplier_missing"] is True
        assert rfq_dict["items"][0]["suppliers"][0]["link_broken"] is True
        assert selected["ns_linked"] is False
        assert selected["near_matches"] == []
        # A broken link must not count as "ready to sync".
        assert ctx["sync_ready"] == 0
        assert ctx["sync_can_sync"] is False
        assert "supplier" in {i["key"] for i in rfq_dict["items"][0]["sync_issues"]}

    def test_healthy_selected_supplier_is_prefilled(self, db_session):
        real = _make_supplier(db_session, "Healthy Co")
        real.url = "https://healthy.example"
        real.country = "Australia"
        real.contacts = [{"email": "a@healthy.example", "phone": "123"}]
        db_session.flush()

        rfq = _make_rfq(db_session, {1: []})
        rfq_dict = {
            "id": rfq.rfq_number,
            "items": [{
                "line": 1,
                "brand": "",
                "part_number": "PN-1",
                "product_id": None,
                "suppliers": [{
                    "name": "Healthy Co",
                    "status": "shortlisted",
                    "supplier_id": str(real.id),
                    "quote_status": "selected",
                }],
            }],
        }

        self._readiness(rfq_dict, db_session)

        selected = rfq_dict["items"][0]["selected_supplier"]
        assert selected["supplier_missing"] is False
        assert rfq_dict["items"][0]["suppliers"][0]["link_broken"] is False
        assert selected["supplier_name"] == "Healthy Co"
        assert selected["supplier_url"] == "https://healthy.example"
        assert selected["supplier_email"] == "a@healthy.example"
