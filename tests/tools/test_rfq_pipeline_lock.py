"""Tests for the RFQ agent-working lock (``rfqs.pipeline_activity``).

The create-RFQ pipeline creates the RFQ early (stage 2) and then spends
30s-2min adding items (stage 3). Nothing told the dashboard that, so staff
could edit a half-populated RFQ and race the pipeline's own writes.

The fix has two halves, both covered here:

  * the pipeline stamps / heartbeats / clears ``rfqs.pipeline_activity``
  * the dashboard rejects mutations with 409 while the flag is live, and
    renders the RFQ read-only with a self-polling banner

See ``.github/prompts/plan-rfqAgentWorkingLock.prompt.md``.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from includes.dashboard.models import Customer, EmailTracking, RFQ

RFQ_NUMBER = "RFQ-2026-7001"


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


def _activity(step: str = "adding_items", age_seconds: int = 0) -> dict:
    """Build a pipeline_activity marker, optionally aged for staleness tests."""
    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    return {
        "kind": "rfq_creation",
        "step": step,
        "started_at": ts,
        "heartbeat_at": ts,
    }


def _create_test_customer(session) -> Customer:
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
    defaults = {
        "gmail_thread_id": f"thread-{uuid.uuid4().hex[:8]}",
        "gmail_message_id": f"msg-{uuid.uuid4().hex[:8]}",
        "user_email": "test@eagle-exports.com",
        "direction": "received",
        "subject": "Test RFQ request",
        "sender_email": "customer@test.com",
    }
    defaults.update(overrides)
    tracking = EmailTracking(customer_id=customer_id, **defaults)
    session.add(tracking)
    session.flush()
    return tracking


def _create_test_rfq(session, rfq_number: str = RFQ_NUMBER) -> RFQ:
    rfq = RFQ(
        rfq_number=rfq_number,
        customer="Test Customer Corp",
        created_by="test@eagle-exports.com",
        created_date=datetime.now(timezone.utc),
        status="in_progress",
    )
    session.add(rfq)
    session.flush()
    return rfq


def _reload(session, rfq_number: str = RFQ_NUMBER) -> RFQ:
    """Re-read the RFQ from the DB, bypassing the identity map cache."""
    session.expire_all()
    return session.query(RFQ).filter(RFQ.rfq_number == rfq_number).first()


# ---------------------------------------------------------------------------
# _rfq_pipeline_active — read-side staleness / malformed handling
# ---------------------------------------------------------------------------

class TestPipelineActiveHelper:
    def test_none_when_no_activity(self):
        from includes.dashboard.routes.rfqs import _rfq_pipeline_active

        assert _rfq_pipeline_active(None) is None
        assert _rfq_pipeline_active({}) is None
        assert _rfq_pipeline_active({"pipeline_activity": None}) is None

    def test_active_when_fresh(self):
        from includes.dashboard.routes.rfqs import _rfq_pipeline_active

        act = _activity()
        assert _rfq_pipeline_active({"pipeline_activity": act}) == act

    def test_stale_heartbeat_treated_as_inactive(self):
        """A crashed daemon thread must not lock the RFQ forever."""
        from includes.dashboard.routes.rfqs import (
            _PIPELINE_STALE_SECONDS,
            _rfq_pipeline_active,
        )

        act = _activity(age_seconds=_PIPELINE_STALE_SECONDS + 60)
        assert _rfq_pipeline_active({"pipeline_activity": act}) is None

    def test_just_inside_threshold_is_active(self):
        from includes.dashboard.routes.rfqs import (
            _PIPELINE_STALE_SECONDS,
            _rfq_pipeline_active,
        )

        act = _activity(age_seconds=_PIPELINE_STALE_SECONDS - 60)
        assert _rfq_pipeline_active({"pipeline_activity": act}) is not None

    @pytest.mark.parametrize("bad", [
        "nope",
        42,
        [],
        {"step": "adding_items"},          # no heartbeat/started_at
        {"heartbeat_at": "not-a-date"},    # unparseable
        {"heartbeat_at": None},
    ])
    def test_malformed_is_inactive(self, bad):
        """Malformed → unlock. A stuck lock is worse than a missed one."""
        from includes.dashboard.routes.rfqs import _rfq_pipeline_active

        assert _rfq_pipeline_active({"pipeline_activity": bad}) is None

    def test_falls_back_to_started_at(self):
        """Earlier writers only set started_at — keep those working."""
        from includes.dashboard.routes.rfqs import _rfq_pipeline_active

        act = {
            "kind": "rfq_creation",
            "step": "adding_items",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        assert _rfq_pipeline_active({"pipeline_activity": act}) is not None

    def test_z_suffix_timestamp_accepted(self):
        from includes.dashboard.routes.rfqs import _rfq_pipeline_active

        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        act = {"step": "adding_items", "heartbeat_at": ts}
        assert _rfq_pipeline_active({"pipeline_activity": act}) is not None

    def test_orm_object_supported(self):
        from includes.dashboard.routes.rfqs import _rfq_pipeline_active

        rfq = MagicMock()
        rfq.pipeline_activity = _activity()
        assert _rfq_pipeline_active(rfq) is not None

    def test_step_label_known_and_unknown(self):
        from includes.dashboard.routes.rfqs import _pipeline_step_label

        assert _pipeline_step_label({"step": "adding_items"}) == "adding items"
        assert _pipeline_step_label({"step": "extracting_items"}) == "reading the email"
        assert _pipeline_step_label({"step": "who_knows"}) == "working"
        assert _pipeline_step_label(None) == "working"

    def test_lock_message_includes_step(self):
        from includes.dashboard.routes.rfqs import _pipeline_lock_message

        msg = _pipeline_lock_message({"step": "adding_items"})
        assert "adding items" in msg
        assert "read-only" in msg or "disabled" in msg

    def test_activity_lookup_by_number_returns_none_for_missing_rfq(self, db_session):
        """An unknown RFQ number means no lock — and must never raise."""
        from includes.dashboard.routes.rfqs import _rfq_pipeline_activity_for

        with patch("includes.dashboard.routes._helpers.get_session",
                   return_value=db_session):
            assert _rfq_pipeline_activity_for(f"RFQ-NOPE-{uuid.uuid4().hex[:8]}") is None


# ---------------------------------------------------------------------------
# Pipeline lifecycle — flag set, heartbeat, cleared
# ---------------------------------------------------------------------------

class TestPipelineLockLifecycle:
    def test_precreated_path_locks_then_unlocks(self, db_session):
        """The Gmail add-on pre-creates the RFQ; the pipeline must lock it.

        Also asserts the agent's own `_add_items_sync` write is NOT blocked —
        the lock is enforced at the HTTP layer, not in the DB helpers.
        """
        cust = _create_test_customer(db_session)
        _create_test_rfq(db_session)
        tracking = _create_test_email_tracking(db_session, customer_id=cust.id)
        db_session.flush()

        seen = {}

        def fake_add_items(rfq_number, data, user_id):
            row = _reload(db_session)
            seen["lock_during_add"] = row.pipeline_activity
            return {"rfq_number": rfq_number}

        with patch("includes.tools.rfq_creation_pipeline._get_session",
                   return_value=db_session), \
             patch("includes.tools.rfq_crud._add_items_sync", side_effect=fake_add_items), \
             patch("includes.tools.rfq_crud._update_rfq_sync") as mock_update, \
             patch("includes.tools.rfq_creation_pipeline._extract_rfq_items_sync") as mock_extract:

            mock_extract.return_value = (
                [{"input_description": "M16 bolt", "quantity": 4}],
                {"title": "Bolt order"},
            )
            mock_update.return_value = {"rfq_number": RFQ_NUMBER}

            from includes.tools.rfq_creation_pipeline import _run_rfq_creation_pipeline
            _run_rfq_creation_pipeline(tracking.id, "test-user", rfq_number=RFQ_NUMBER)

        # Lock was live while the agent was adding items...
        assert seen["lock_during_add"] is not None
        assert seen["lock_during_add"]["kind"] == "rfq_creation"
        assert seen["lock_during_add"]["step"] == "adding_items"
        assert seen["lock_during_add"]["started_at"]  # preserved from the first stamp

        # ...and released once the pipeline finished.
        row = _reload(db_session)
        assert row.pipeline_activity is None

        result = db_session.query(EmailTracking).filter(EmailTracking.id == tracking.id).first()
        assert result.rfq_creation_result["status"] == "complete"

    def test_started_at_preserved_across_heartbeats(self, db_session):
        """Heartbeats must not reset started_at — it is the run's start time."""
        from includes.tools.rfq_creation_pipeline import _set_rfq_pipeline_activity

        _create_test_rfq(db_session)

        with patch("includes.tools.rfq_creation_pipeline._get_session",
                   return_value=db_session):
            _set_rfq_pipeline_activity(RFQ_NUMBER, "extracting_items")
            first = _reload(db_session).pipeline_activity
            _set_rfq_pipeline_activity(RFQ_NUMBER, "adding_items")
            second = _reload(db_session).pipeline_activity

        assert second["step"] == "adding_items"
        assert second["started_at"] == first["started_at"]

    def test_lock_cleared_when_extraction_fails(self, db_session):
        """Extraction failure → partial result → RFQ unlocked for manual entry."""
        cust = _create_test_customer(db_session)
        _create_test_rfq(db_session)
        tracking = _create_test_email_tracking(db_session, customer_id=cust.id)
        db_session.flush()

        with patch("includes.tools.rfq_creation_pipeline._get_session",
                   return_value=db_session), \
             patch("includes.tools.rfq_creation_pipeline._extract_rfq_items_sync",
                   side_effect=RuntimeError("LLM exploded")):

            from includes.tools.rfq_creation_pipeline import _run_rfq_creation_pipeline
            _run_rfq_creation_pipeline(tracking.id, "test-user", rfq_number=RFQ_NUMBER)

        assert _reload(db_session).pipeline_activity is None
        result = db_session.query(EmailTracking).filter(EmailTracking.id == tracking.id).first()
        assert result.rfq_creation_result["status"] == "partial"

    def test_lock_cleared_by_save_error(self, db_session):
        """Early failures funnel through _save_error → lock released."""
        _create_test_rfq(db_session)
        tracking = _create_test_email_tracking(db_session, customer_id=None)
        db_session.flush()

        from includes.tools.rfq_creation_pipeline import (
            _set_rfq_pipeline_activity,
            _save_error,
        )

        with patch("includes.tools.rfq_creation_pipeline._get_session",
                   return_value=db_session):
            _set_rfq_pipeline_activity(RFQ_NUMBER, "adding_items")
            assert _reload(db_session).pipeline_activity is not None

            _save_error(tracking.id, "No customer linked", rfq_number=RFQ_NUMBER)

        assert _reload(db_session).pipeline_activity is None

    def test_clear_is_idempotent_when_no_lock(self, db_session):
        from includes.tools.rfq_creation_pipeline import _clear_rfq_pipeline_activity

        _create_test_rfq(db_session)
        with patch("includes.tools.rfq_creation_pipeline._get_session",
                   return_value=db_session):
            _clear_rfq_pipeline_activity(RFQ_NUMBER)  # must not raise

        assert _reload(db_session).pipeline_activity is None

    def test_set_ignores_missing_rfq(self, db_session):
        """A lock for an unknown RFQ is a no-op, never an exception."""
        from includes.tools.rfq_creation_pipeline import _set_rfq_pipeline_activity

        with patch("includes.tools.rfq_creation_pipeline._get_session",
                   return_value=db_session):
            _set_rfq_pipeline_activity("RFQ-DOES-NOT-EXIST-9999", "adding_items")
            _set_rfq_pipeline_activity(None, "adding_items")  # no rfq yet

    def test_stale_lock_reported_inactive_via_db_lookup(self, db_session):
        from includes.dashboard.routes.rfqs import (
            _PIPELINE_STALE_SECONDS,
            _rfq_pipeline_activity_for,
        )

        rfq = _create_test_rfq(db_session)
        rfq.pipeline_activity = _activity(age_seconds=_PIPELINE_STALE_SECONDS + 120)
        db_session.flush()

        with patch("includes.dashboard.routes._helpers.get_session",
                   return_value=db_session):
            assert _rfq_pipeline_activity_for(RFQ_NUMBER) is None

    def test_fresh_lock_reported_active_via_db_lookup(self, db_session):
        from includes.dashboard.routes.rfqs import _rfq_pipeline_activity_for

        rfq = _create_test_rfq(db_session)
        rfq.pipeline_activity = _activity()
        db_session.flush()

        with patch("includes.dashboard.routes._helpers.get_session",
                   return_value=db_session):
            act = _rfq_pipeline_activity_for(RFQ_NUMBER)
        assert act is not None and act["step"] == "adding_items"


# ---------------------------------------------------------------------------
# HTTP guards — 409 while locked, untouched behaviour when not
# ---------------------------------------------------------------------------

def _make_test_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        SessionMiddleware,
        secret_key="test-secret",
        session_cookie="eagleagent_session",
    )
    from includes.dashboard.routes import router
    app.include_router(router)

    @app.get("/_test/login")
    async def _login(request: Request, email: str = "admin@eagle.com"):
        request.session["user"] = {"email": email, "name": "Test Admin"}
        return Response(status_code=200)

    return app


LOCK_PATH = "includes.dashboard.routes.rfqs._rfq_pipeline_activity_for"


class TestMutationGuards:
    @pytest.fixture
    def client(self):
        client = TestClient(_make_test_app())
        client.get("/_test/login")
        return client

    def _locked(self):
        return patch(LOCK_PATH, return_value=_activity(step="adding_items"))

    def test_update_item_blocked(self, client):
        with self._locked(), \
             patch("includes.tools.quote_tools._update_item_sync") as m:
            resp = client.post(f"/partial/rfqs/{RFQ_NUMBER}/update-item",
                               data={"line": "1", "quantity": "9"})
        assert resp.status_code == 409
        assert "adding items" in resp.text
        m.assert_not_called()

    def test_delete_item_blocked(self, client):
        with self._locked(), \
             patch("includes.tools.rfq_crud._delete_item_sync") as m:
            resp = client.delete(f"/partial/rfqs/{RFQ_NUMBER}/delete-item/1")
        assert resp.status_code == 409
        m.assert_not_called()

    def test_add_item_blocked(self, client):
        with self._locked():
            resp = client.post(f"/partial/rfqs/{RFQ_NUMBER}/add-item",
                               data={"input_description": "Sneaky new row"})
        assert resp.status_code == 409

    def test_header_update_blocked(self, client):
        """Stage 3 writes title/notes — the header must be locked too."""
        with self._locked(), \
             patch("includes.tools.quote_tools._update_rfq_sync") as m:
            resp = client.post(f"/partial/rfqs/{RFQ_NUMBER}/update",
                               data={"title": "Renamed mid-run"})
        assert resp.status_code == 409
        m.assert_not_called()

    def test_bulk_update_blocked_returns_json(self, client):
        with self._locked(), \
             patch("includes.tools.rfq_crud._update_items_bulk_sync") as m:
            resp = client.post(f"/partial/rfqs/{RFQ_NUMBER}/bulk-update-items",
                               json={"items": [{"line": 1, "quantity": 3}]})
        assert resp.status_code == 409
        assert resp.json()["status"] == "error"
        m.assert_not_called()

    # -- unlocked control cases: the guard must not change normal behaviour --

    def test_update_item_proceeds_when_unlocked(self, client):
        with patch(LOCK_PATH, return_value=None), \
             patch("includes.tools.quote_tools._update_item_sync",
                   return_value="Item not found.") as m:
            resp = client.post(f"/partial/rfqs/{RFQ_NUMBER}/update-item",
                               data={"line": "1"})
        assert resp.status_code == 404
        m.assert_called_once()

    def test_update_item_proceeds_when_lock_is_stale(self, client):
        """An aged heartbeat must let writes through, not 409 forever."""
        from includes.dashboard.routes.rfqs import (
            _PIPELINE_STALE_SECONDS,
            _rfq_pipeline_activity_for,
        )

        stale = _activity(age_seconds=_PIPELINE_STALE_SECONDS + 60)
        with patch(LOCK_PATH, side_effect=lambda n: _rfq_pipeline_activity_for(
                {"pipeline_activity": stale})), \
             patch("includes.tools.quote_tools._update_item_sync",
                   return_value="Item not found.") as m:
            resp = client.post(f"/partial/rfqs/{RFQ_NUMBER}/update-item",
                               data={"line": "1"})
        assert resp.status_code == 404
        m.assert_called_once()

    def test_supplier_ops_not_blocked(self, client):
        """Supplier ops are deliberately out of scope (decision 3).

        clear-suppliers mutates a line's supplier list, not its item fields, so
        it cannot race the pipeline's item/header writes.
        """
        with self._locked(), \
             patch("includes.tools.quote_tools._clear_suppliers_sync",
                   return_value="Line not found.") as m, \
             patch("includes.tools.quote_tools._get_rfq_dict_sync",
                   return_value=None):
            resp = client.post(f"/partial/rfqs/{RFQ_NUMBER}/clear-suppliers",
                               data={"line": "1"})
        assert resp.status_code != 409
        m.assert_called_once()


class TestPipelineStatusEndpoint:
    @pytest.fixture
    def client(self):
        client = TestClient(_make_test_app())
        client.get("/_test/login")
        return client

    def test_requires_auth(self):
        client = TestClient(_make_test_app())
        resp = client.get(f"/partial/rfqs/{RFQ_NUMBER}/pipeline-status",
                          follow_redirects=False)
        assert resp.status_code == 303

    def test_returns_banner_while_active(self, client):
        with patch(LOCK_PATH, return_value=_activity(step="adding_items")):
            resp = client.get(f"/partial/rfqs/{RFQ_NUMBER}/pipeline-status")
        assert resp.status_code == 200
        assert "rfq-pipeline-banner" in resp.text
        assert "adding items" in resp.text
        # Self-polling: carries the URL the shared poller reads.
        assert f"/partial/rfqs/{RFQ_NUMBER}/pipeline-status" in resp.text
        assert 'data-processing="true"' in resp.text

    def test_step_label_updates_between_polls(self, client):
        with patch(LOCK_PATH, return_value=_activity(step="updating_details")):
            resp = client.get(f"/partial/rfqs/{RFQ_NUMBER}/pipeline-status")
        assert "updating details" in resp.text

    def test_empty_body_and_trigger_when_done(self, client):
        with patch(LOCK_PATH, return_value=None):
            resp = client.get(f"/partial/rfqs/{RFQ_NUMBER}/pipeline-status")
        assert resp.status_code == 200
        assert resp.text.strip() == ""
        trigger = resp.headers.get("HX-Trigger", "")
        assert "pipelineDone" in trigger
        assert f"/partial/rfqs/{RFQ_NUMBER}/items" in trigger

    def test_route_not_shadowed_by_tab_catchall(self, client):
        """`/pipeline-status` must not be parsed as a tab name."""
        with patch(LOCK_PATH, return_value=None):
            resp = client.get(f"/partial/rfqs/{RFQ_NUMBER}/pipeline-status")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Read-only rendering
# ---------------------------------------------------------------------------

@pytest.fixture
def jinja_env():
    from jinja2 import Environment, FileSystemLoader
    return Environment(loader=FileSystemLoader("templates"))


def _item(**overrides) -> dict:
    base = {
        "line": 1,
        "input_description": "M16 bolt",
        "part_number": "M16-100",
        "brand": "Acme",
        "quantity": 4,
        "uom": "ea",
        "match": "unmatched",
        "suppliers": [],
    }
    base.update(overrides)
    return base


class TestReadOnlyRendering:
    def test_banner_renders_step_and_poll_attrs(self, jinja_env):
        html = jinja_env.get_template("partials/_rfq_pipeline_banner.html").render(
            rfq={"id": RFQ_NUMBER},
            pipeline_activity=_activity(step="adding_items"),
            pipeline_step_label="adding items",
        )
        assert "rfq-pipeline-banner" in html
        assert f"/partial/rfqs/{RFQ_NUMBER}/pipeline-status" in html
        assert 'data-poll-url=' in html
        assert 'data-processing="true"' in html
        assert "adding items" in html

    def test_locked_table_hides_all_edit_controls(self, jinja_env):
        html = jinja_env.get_template("partials/_rfq_items_table.html").render(
            table_items=[_item()], rfq={"id": RFQ_NUMBER},
            show_add_row=True, pipeline_active=True, departments=[],
        )
        assert "add-item-form" not in html, "add row must be hidden"
        assert "editingItem = 1" not in html, "pencil edit must be hidden"
        assert "identifyItem(1)" not in html, "per-item agent run must be hidden"
        assert "M16 bolt" in html, "item data must still render read-only"

    def test_unlocked_table_keeps_edit_controls(self, jinja_env):
        html = jinja_env.get_template("partials/_rfq_items_table.html").render(
            table_items=[_item()], rfq={"id": RFQ_NUMBER},
            show_add_row=True, pipeline_active=False, departments=[],
        )
        assert "add-item-form" in html
        assert "editingItem = 1" in html
        assert "identifyItem(1)" in html

    def test_use_as_is_hidden_for_discrepancy_when_locked(self, jinja_env):
        tpl = jinja_env.get_template("partials/_rfq_items_table.html")
        item = _item(match="discrepancy")
        locked = tpl.render(table_items=[item], rfq={"id": RFQ_NUMBER},
                            show_add_row=False, pipeline_active=True, departments=[])
        unlocked = tpl.render(table_items=[item], rfq={"id": RFQ_NUMBER},
                              show_add_row=False, pipeline_active=False, departments=[])
        assert "Use as-is" not in locked
        assert "Use as-is" in unlocked

    def test_detail_context_exposes_flags(self, db_session):
        """_rfq_detail_context must pass the flag the templates read."""
        from includes.dashboard.routes.rfqs import _add_pipeline_flags

        ctx = _add_pipeline_flags({}, {"pipeline_activity": _activity()})
        assert ctx["pipeline_active"] is True
        assert ctx["pipeline_activity"]["step"] == "adding_items"
        assert ctx["pipeline_step_label"] == "adding items"

        ctx2 = _add_pipeline_flags({}, {"pipeline_activity": None})
        assert ctx2["pipeline_active"] is False
        assert ctx2["pipeline_activity"] == {}


# ---------------------------------------------------------------------------
# Discovery — the items tab must find a lock that appears AFTER it rendered
#
# The banner only polls once it is already on screen, so a tab opened while
# unlocked used to stay editable until a manual reload. The items tab now
# always carries a state watcher while the RFQ is fresh or already locked.
# ---------------------------------------------------------------------------

class TestFreshnessFlag:
    """Controls whether the items tab renders a state watcher at all."""

    def test_young_rfq_is_watched(self):
        from includes.dashboard.routes.rfqs import _add_pipeline_flags

        assert _add_pipeline_flags({}, {"age_hours": 0})["rfq_is_fresh"] is True

    def test_old_unlocked_rfq_is_not_watched(self):
        """No polling forever on idle RFQs."""
        from includes.dashboard.routes.rfqs import _add_pipeline_flags

        assert _add_pipeline_flags({}, {"age_hours": 5})["rfq_is_fresh"] is False

    def test_active_lock_forces_watch_even_when_old(self):
        from includes.dashboard.routes.rfqs import _add_pipeline_flags

        ctx = _add_pipeline_flags({}, {"pipeline_activity": _activity(), "age_hours": 99})
        assert ctx["rfq_is_fresh"] is True

    def test_unknown_age_errors_towards_watching(self):
        from includes.dashboard.routes.rfqs import _add_pipeline_flags

        assert _add_pipeline_flags({}, {})["rfq_is_fresh"] is True


class TestPipelineActiveProbe:
    """GET /partial/rfqs/{id}/pipeline-active — the watcher's JSON probe."""

    @pytest.fixture
    def client(self):
        client = TestClient(_make_test_app())
        client.get("/_test/login")
        return client

    def test_reports_active_with_step(self, client):
        with patch(LOCK_PATH, return_value=_activity(step="adding_items")):
            resp = client.get(f"/partial/rfqs/{RFQ_NUMBER}/pipeline-active")
        assert resp.status_code == 200
        assert resp.json() == {
            "active": True,
            "step": "adding_items",
            "label": "adding items",
        }

    def test_reports_inactive(self, client):
        with patch(LOCK_PATH, return_value=None):
            resp = client.get(f"/partial/rfqs/{RFQ_NUMBER}/pipeline-active")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is False
        assert data["step"] is None

    def test_stale_lock_reads_as_inactive(self, client):
        """A crashed run must not keep the tab refreshing forever."""
        from includes.dashboard.routes.rfqs import (
            _PIPELINE_STALE_SECONDS,
            _rfq_pipeline_active,
        )

        stale = _activity(age_seconds=_PIPELINE_STALE_SECONDS + 60)
        with patch(LOCK_PATH, side_effect=lambda n: _rfq_pipeline_active(
                {"pipeline_activity": stale})):
            resp = client.get(f"/partial/rfqs/{RFQ_NUMBER}/pipeline-active")
        assert resp.json()["active"] is False

    def test_not_shadowed_by_tab_catchall(self, client):
        """Must return JSON, not an HTML tab partial."""
        with patch(LOCK_PATH, return_value=None):
            resp = client.get(f"/partial/rfqs/{RFQ_NUMBER}/pipeline-active")
        assert resp.headers["content-type"].startswith("application/json")


# ---------------------------------------------------------------------------
# The add-on route must lock the RFQ BEFORE it hands off to the pipeline
# ---------------------------------------------------------------------------

class TestAddonRouteLocksBeforeHandoff:
    def test_lock_is_set_before_the_pipeline_is_triggered(self, db_session):
        """Reported bug: the first view of a just-created RFQ was editable.

        The route created the RFQ and returned the number, while the lock only
        landed ~100ms later in the pipeline thread. Now the route stamps it
        first, so the RFQ is already read-only when the caller learns its number.
        """
        cust = _create_test_customer(db_session)
        _create_test_rfq(db_session)  # RFQ_NUMBER, so the lock has a row to land on
        tracking = _create_test_email_tracking(db_session, customer_id=cust.id)
        db_session.flush()

        seen = {}

        def fake_trigger(email_tracking_id, user_id="system", rfq_number=None):
            seen["lock_at_handoff"] = _reload(db_session).pipeline_activity
            seen["rfq_number"] = rfq_number

        with patch("includes.dashboard.database.get_session",
                   return_value=db_session), \
             patch("includes.tools.rfq_creation_pipeline._get_session",
                   return_value=db_session), \
             patch("includes.tools.rfq_crud._create_rfq_sync",
                   return_value={"rfq_number": RFQ_NUMBER, "id": "test-id"}), \
             patch("includes.netsuite.records.opportunity.create_and_link_opportunity"), \
             patch("includes.tools.rfq_creation_pipeline.trigger_rfq_creation_pipeline",
                   side_effect=fake_trigger):

            from includes.dashboard.routes.addon import CreateRfqRequest, create_rfq
            resp = create_rfq(
                CreateRfqRequest(gmail_message_id=tracking.gmail_message_id,
                                 gmail_thread_id=tracking.gmail_thread_id),
                {"email": "test@eagle-exports.com"},
            )

        assert resp.status_code == 200, resp.body
        assert json.loads(resp.body)["rfq_number"] == RFQ_NUMBER

        # The decisive assertion: the lock already existed at handoff time.
        assert seen["rfq_number"] == RFQ_NUMBER
        assert seen["lock_at_handoff"] is not None, "RFQ was unlocked when the pipeline was handed off"
        assert seen["lock_at_handoff"]["step"] == "extracting_items"

    def test_lock_cleared_if_the_run_fails_to_start(self, db_session):
        """Don't strand a locked RFQ if the thread never launches."""
        cust = _create_test_customer(db_session)
        _create_test_rfq(db_session)
        tracking = _create_test_email_tracking(db_session, customer_id=cust.id)
        db_session.flush()

        with patch("includes.dashboard.database.get_session",
                   return_value=db_session), \
             patch("includes.tools.rfq_creation_pipeline._get_session",
                   return_value=db_session), \
             patch("includes.tools.rfq_crud._create_rfq_sync",
                   return_value={"rfq_number": RFQ_NUMBER, "id": "test-id"}), \
             patch("includes.netsuite.records.opportunity.create_and_link_opportunity"), \
             patch("includes.tools.rfq_creation_pipeline.trigger_rfq_creation_pipeline",
                   side_effect=RuntimeError("thread would not start")):

            from includes.dashboard.routes.addon import CreateRfqRequest, create_rfq
            resp = create_rfq(
                CreateRfqRequest(gmail_message_id=tracking.gmail_message_id,
                                 gmail_thread_id=tracking.gmail_thread_id),
                {"email": "test@eagle-exports.com"},
            )

        assert resp.status_code == 500
        assert _reload(db_session).pipeline_activity is None
