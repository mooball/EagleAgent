"""Direct endpoint tests for the Selection tab's select-supplier actions.

The route coroutines are called with a real (rolled-back) DB session so the
single-select and bulk-select mutation logic is exercised end to end, including
the JSON row/totals payloads the client swaps in.
"""

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from includes.dashboard.models import RFQ, RFQItem


@pytest.fixture
def db_session():
    """DB session with SAVEPOINT so commits inside the route don't end the
    outer transaction — everything rolls back at the end."""
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
    """Non-hex suffix — hex suffixes can collide with real RFQ numbers."""
    return f"RFQ-2026-T{uuid.uuid4().hex[:6].upper()}"


def _make_rfq(session, suppliers_by_line):
    rfq = RFQ(rfq_number=_unique_rfq_number(), customer="Test Customer",
              created_by="tester", created_date=datetime.now(timezone.utc))
    session.add(rfq)
    session.flush()
    for line, sups in suppliers_by_line.items():
        session.add(RFQItem(
            rfq_id=rfq.id, line=line,
            input_description=f"desc {line}",
            quantity=1, uom="ea",
            suppliers=[dict(s) for s in sups],
        ))
    session.flush()
    return rfq


def _stored_item(session, rfq, line):
    return session.query(RFQItem).filter(
        RFQItem.rfq_id == rfq.id, RFQItem.line == line
    ).first()


async def _json_request(payload: dict) -> Request:
    body = json.dumps(payload).encode()
    state = {"sent": False}

    async def receive():
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }
    return Request(scope, receive)


def _sup(name, quote_status="quoted", quote_cost=5.0, part_number=None):
    return {
        "name": name,
        "status": "shortlisted",
        "quote_status": quote_status,
        "quote_cost": quote_cost,
        "quote_part_number": part_number,
    }


SESSION_PATCH = "includes.dashboard.routes._helpers.get_session"


class TestSingleSelectEndpoint:
    async def test_copies_cost_to_item_and_returns_row(self, db_session):
        from includes.dashboard.routes.rfqs import quotation_select_supplier

        rfq = _make_rfq(db_session, {1: [_sup("Acme", quote_cost=12.5)]})
        with patch(SESSION_PATCH, return_value=db_session):
            resp = await quotation_select_supplier(
                await _json_request({"supplier_name": "Acme"}),
                rfq.rfq_number, 1, {"email": "t@test"},
            )

        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert 'data-line="1"' in data["row"]
        assert "quote-selected" in data["row"]
        assert "data-selection-totals" in data["totals"]

        stored = _stored_item(db_session, rfq, 1)
        assert stored.suppliers[0]["quote_status"] == "selected"
        assert float(stored.cost_price) == 12.5

    async def test_deselects_previous_supplier(self, db_session):
        from includes.dashboard.routes.rfqs import quotation_select_supplier

        rfq = _make_rfq(db_session, {1: [
            _sup("Other", "selected", 3.0),
            _sup("Acme", "quoted", 9.0),
        ]})
        with patch(SESSION_PATCH, return_value=db_session):
            resp = await quotation_select_supplier(
                await _json_request({"supplier_name": "Acme"}),
                rfq.rfq_number, 1, {"email": "t@test"},
            )

        assert resp.status_code == 200
        by_name = {s["name"]: s for s in _stored_item(db_session, rfq, 1).suppliers}
        assert by_name["Other"]["quote_status"] == "quoted"
        assert by_name["Acme"]["quote_status"] == "selected"
        assert float(_stored_item(db_session, rfq, 1).cost_price) == 9.0

    async def test_clicking_selected_supplier_toggles_off(self, db_session):
        from includes.dashboard.routes.rfqs import quotation_select_supplier

        rfq = _make_rfq(db_session, {1: [_sup("Acme", "selected", 5.0)]})
        with patch(SESSION_PATCH, return_value=db_session):
            resp = await quotation_select_supplier(
                await _json_request({"supplier_name": "Acme"}),
                rfq.rfq_number, 1, {"email": "t@test"},
            )

        assert resp.status_code == 200
        assert _stored_item(db_session, rfq, 1).suppliers[0]["quote_status"] == "quoted"

    async def test_missing_supplier_returns_404(self, db_session):
        from includes.dashboard.routes.rfqs import quotation_select_supplier

        rfq = _make_rfq(db_session, {1: [_sup("Acme")]})
        with patch(SESSION_PATCH, return_value=db_session):
            resp = await quotation_select_supplier(
                await _json_request({"supplier_name": "Nobody"}),
                rfq.rfq_number, 1, {"email": "t@test"},
            )

        assert resp.status_code == 404

    async def test_rejects_non_shortlisted_supplier(self, db_session):
        """A direct/stale request must not select a candidate the matrix never
        offers."""
        from includes.dashboard.routes.rfqs import quotation_select_supplier

        rfq = _make_rfq(db_session, {1: [
            {"name": "Acme", "status": "candidate",
             "quote_status": "quoted", "quote_cost": 5.0},
        ]})
        with patch(SESSION_PATCH, return_value=db_session):
            resp = await quotation_select_supplier(
                await _json_request({"supplier_name": "Acme"}),
                rfq.rfq_number, 1, {"email": "t@test"},
            )

        assert resp.status_code == 400
        assert _stored_item(db_session, rfq, 1).suppliers[0]["quote_status"] == "quoted"


class TestSelectAllEndpoint:
    async def test_selects_quoted_lines_and_reports_skipped(self, db_session):
        from includes.dashboard.routes.rfqs import quotation_select_supplier_all

        rfq = _make_rfq(db_session, {
            1: [_sup("Acme", "quoted", 5.0)],
            2: [_sup("Beta", "selected", 2.0), _sup("Acme", "quoted", 6.0)],
            3: [_sup("Acme", "declined", None)],
            4: [_sup("Other", "quoted", 1.0)],  # Acme absent here
        })
        with patch(SESSION_PATCH, return_value=db_session), \
             patch("includes.dashboard.routes.rfqs._rfq_pipeline_activity_for",
                   return_value=None):
            resp = await quotation_select_supplier_all(
                await _json_request({"supplier_name": "Acme"}),
                rfq.rfq_number, {"email": "t@test"},
            )

        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert data["status"] == "ok"
        assert data["changed"] == 2
        assert data["skipped"] == 1
        assert set(data["rows"]) == {"1", "2"}
        assert "data-selection-totals" in data["totals"]

        one = _stored_item(db_session, rfq, 1)
        assert one.suppliers[0]["quote_status"] == "selected"
        assert float(one.cost_price) == 5.0

        two = {s["name"]: s for s in _stored_item(db_session, rfq, 2).suppliers}
        assert two["Beta"]["quote_status"] == "quoted"
        assert two["Acme"]["quote_status"] == "selected"
        assert float(_stored_item(db_session, rfq, 2).cost_price) == 6.0

        # Declined line untouched; absent line untouched.
        assert _stored_item(db_session, rfq, 3).suppliers[0]["quote_status"] == "declined"
        assert _stored_item(db_session, rfq, 4).suppliers[0]["quote_status"] == "quoted"

    async def test_large_change_asks_for_full_refresh(self, db_session):
        from includes.dashboard.routes.rfqs import (
            _SELECT_ALL_INLINE_ROW_LIMIT,
            quotation_select_supplier_all,
        )

        lines = {
            line: [_sup("Acme", "quoted", float(line))]
            for line in range(1, _SELECT_ALL_INLINE_ROW_LIMIT + 2)
        }
        rfq = _make_rfq(db_session, lines)
        with patch(SESSION_PATCH, return_value=db_session), \
             patch("includes.dashboard.routes.rfqs._rfq_pipeline_activity_for",
                   return_value=None):
            resp = await quotation_select_supplier_all(
                await _json_request({"supplier_name": "Acme"}),
                rfq.rfq_number, {"email": "t@test"},
            )

        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert data["changed"] == _SELECT_ALL_INLINE_ROW_LIMIT + 1
        assert data["refresh"] == "full"
        assert "rows" not in data

    async def test_blocked_by_pipeline_lock(self, db_session):
        from includes.dashboard.routes.rfqs import quotation_select_supplier_all

        rfq = _make_rfq(db_session, {1: [_sup("Acme")]})
        with patch("includes.dashboard.routes.rfqs._rfq_pipeline_activity_for",
                   return_value={"step": "adding_items"}):
            resp = await quotation_select_supplier_all(
                await _json_request({"supplier_name": "Acme"}),
                rfq.rfq_number, {"email": "t@test"},
            )

        assert resp.status_code == 409
        assert json.loads(resp.body)["status"] == "error"

    async def test_retry_does_not_double_count(self, db_session):
        """_commit_bulk_with_retry re-runs _apply after a deadlock. The retry must
        reset the accumulators, or changed/skipped are counted twice."""
        from includes.dashboard.routes import rfqs as rfq_mod

        rfq = _make_rfq(db_session, {
            1: [_sup("Acme", "quoted", 5.0)],
            2: [_sup("Acme", "quoted", 6.0)],
            3: [_sup("Acme", "declined", None)],
        })

        async def retrying_commit(session, apply_fn, attempts=3):
            apply_fn()
            session.expire_all()  # retry re-reads state (as after a rollback)
            apply_fn()
            session.commit()

        with patch(SESSION_PATCH, return_value=db_session), \
             patch.object(rfq_mod, "_rfq_pipeline_activity_for", return_value=None), \
             patch.object(rfq_mod, "_commit_bulk_with_retry", new=retrying_commit):
            resp = await rfq_mod.quotation_select_supplier_all(
                await _json_request({"supplier_name": "Acme"}),
                rfq.rfq_number, {"email": "t@test"},
            )

        data = json.loads(resp.body)
        assert data["changed"] == 2
        assert data["skipped"] == 1
        assert set(data["rows"]) == {"1", "2"}

    async def test_skips_non_shortlisted_supplier(self, db_session):
        from includes.dashboard.routes.rfqs import quotation_select_supplier_all

        rfq = _make_rfq(db_session, {1: [
            {"name": "Acme", "status": "candidate",
             "quote_status": "quoted", "quote_cost": 5.0},
        ]})
        with patch(SESSION_PATCH, return_value=db_session), \
             patch("includes.dashboard.routes.rfqs._rfq_pipeline_activity_for",
                   return_value=None):
            resp = await quotation_select_supplier_all(
                await _json_request({"supplier_name": "Acme"}),
                rfq.rfq_number, {"email": "t@test"},
            )

        data = json.loads(resp.body)
        assert data["changed"] == 0
        assert data["skipped"] == 1
        assert _stored_item(db_session, rfq, 1).suppliers[0]["quote_status"] == "quoted"
