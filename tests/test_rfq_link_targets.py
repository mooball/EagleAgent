"""Tests for the RFQ communications-tab "link email to a business" targets.

A thread lands under "Other / Unmatched" when both ``email_tracking.supplier_id``
and ``customer_id`` are NULL. The Link button on those threads only ever offers
businesses already on the RFQ, served by
``GET /partial/rfqs/{id}/link-targets`` and assembled by
``_build_rfq_link_targets``.
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from includes.dashboard.routes.rfqs import _build_rfq_link_targets

RFQ_NUMBER = "RFQ-2026-7002"
CUST_ID = "11111111-1111-1111-1111-111111111111"
SUP_ID = "22222222-2222-2222-2222-222222222222"
OTHER_SUP_ID = "33333333-3333-3333-3333-333333333333"

GET_RFQ_DICT = "includes.tools.quote_tools._get_rfq_dict_sync"


def _rfq(customer_id=CUST_ID, items=None) -> dict:
    return {
        "id": RFQ_NUMBER,
        "customer": "Test Customer Corp",
        "customer_id": customer_id,
        "items": items if items is not None else [
            {"line": 1, "suppliers": [
                {"name": "Total Tools", "status": "shortlisted", "supplier_id": SUP_ID},
                {"name": "Dropped Co", "status": "dropped", "supplier_id": OTHER_SUP_ID},
            ]},
            {"line": 2, "suppliers": [
                {"name": "Total Tools", "status": "shortlisted", "supplier_id": SUP_ID},
                {"name": "Crusher Spares Ltd", "status": "shortlisted", "supplier_id": None},
            ]},
        ],
    }


class TestBuildRfqLinkTargets:
    def test_shortlisted_only_and_deduped_across_items(self):
        targets = _build_rfq_link_targets(_rfq())

        names = [s["name"] for s in targets["suppliers"]]
        assert names == ["Crusher Spares Ltd", "Total Tools"]  # sorted, deduped
        assert "Dropped Co" not in names

    def test_customer_returned_with_id_and_name(self):
        assert _build_rfq_link_targets(_rfq())["customer"] == {
            "id": CUST_ID, "name": "Test Customer Corp",
        }

    def test_no_customer_id_yields_no_customer(self):
        assert _build_rfq_link_targets(_rfq(customer_id=None))["customer"] is None

    def test_unreadable_supplier_id_kept_but_unlinkable(self):
        """A shortlisted supplier with a legacy id must still be listed —
        hiding it would look like the supplier is missing from the RFQ —
        but with no id, so the UI can grey the row out."""
        targets = _build_rfq_link_targets(_rfq(items=[
            {"line": 1, "suppliers": [
                {"name": "Legacy Co", "status": "shortlisted", "supplier_id": "sup_1597"},
            ]},
        ]))

        assert targets["suppliers"] == [{"id": None, "name": "Legacy Co"}]

    def test_real_id_wins_over_unreadable_one(self):
        """Same supplier on two items, one row with a junk id — the linkable
        occurrence must be the one that survives the dedup."""
        targets = _build_rfq_link_targets(_rfq(items=[
            {"line": 1, "suppliers": [
                {"name": "Total Tools", "status": "shortlisted", "supplier_id": "926"},
            ]},
            {"line": 2, "suppliers": [
                {"name": "total tools", "status": "shortlisted", "supplier_id": SUP_ID},
            ]},
        ]))

        assert targets["suppliers"] == [{"id": SUP_ID, "name": "Total Tools"}]

    def test_empty_rfq(self):
        targets = _build_rfq_link_targets({"id": RFQ_NUMBER, "items": []})
        assert targets["suppliers"] == []
        assert targets["customer"] is None


# ---------------------------------------------------------------------------
# HTTP — the endpoint must not be swallowed by the /{tab} catch-all
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


@pytest.fixture
def client():
    client = TestClient(_make_test_app())
    client.get("/_test/login")
    return client


class TestLinkTargetsEndpoint:
    def test_returns_json_targets(self, client):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(GET_RFQ_DICT, lambda rfq_id: _rfq())
            resp = client.get(f"/partial/rfqs/{RFQ_NUMBER}/link-targets")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["customer"]["id"] == CUST_ID
        assert [s["name"] for s in data["suppliers"]] == ["Crusher Spares Ltd", "Total Tools"]

    def test_missing_rfq_returns_404(self, client):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(GET_RFQ_DICT, lambda rfq_id: None)
            resp = client.get("/partial/rfqs/RFQ-DOES-NOT-EXIST-9999/link-targets")

        assert resp.status_code == 404
        assert resp.json()["status"] == "error"

    def test_requires_login(self):
        anon = TestClient(_make_test_app())
        resp = anon.get(f"/partial/rfqs/{RFQ_NUMBER}/link-targets", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"
