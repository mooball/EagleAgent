"""Tests for includes/tools/comms_summary.py — AI communications summary."""

import re
import types
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from includes.tools.comms_summary import compute_cache_key, _build_bundle, decorate_dates


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestComputeCacheKey:
    def test_deterministic(self):
        fp = {"count": 3, "max_id": 7, "max_ts": "2026-08-14T01:00:00+00:00"}
        assert compute_cache_key(fp, "abc") == compute_cache_key(dict(fp), "abc")

    def test_changes_with_email_fingerprint(self):
        fp = {"count": 3, "max_id": 7}
        assert compute_cache_key(fp, "abc") != compute_cache_key({**fp, "max_id": 8}, "abc")

    def test_changes_with_quotes_fingerprint(self):
        fp = {"count": 3, "max_id": 7}
        assert compute_cache_key(fp, "abc") != compute_cache_key(fp, "abd")


class TestBuildBundle:
    def _rfq_dict(self):
        return {
            "rfq_number": "RFQ-2026-0042",
            "customer": "Acme Mining",
            "status": "in_progress",
            "created_date": "2026-08-01",
            "notes": "Urgent delivery",
            "items": [
                {
                    "line": 1,
                    "input_description": "Hydraulic hose",
                    "input_code": "HH-100",
                    "quantity": 5,
                    "uom": "ea",
                    "suppliers": [
                        {"name": "HoseCo", "price": 12.5, "price_type": "unit", "lead_time": "2 weeks"},
                    ],
                }
            ],
        }

    def _rows(self):
        return [
            {
                "direction": "sent",
                "subject": "Quotation Request",
                "sender_email": "staff@eagle.com",
                "recipient_email": "sales@hoseco.com",
                "sent_at": datetime(2026, 8, 2, 3, 0, tzinfo=timezone.utc),
                "created_at": None,
                "supplier_pipeline_result": None,
                "supplier_name": "HoseCo",
                "customer_name": None,
            },
            {
                "direction": "received",
                "subject": "Re: Quotation Request",
                "sender_email": "sales@hoseco.com",
                "recipient_email": "staff@eagle.com",
                "sent_at": datetime(2026, 8, 3, 5, 30, tzinfo=timezone.utc),
                "created_at": None,
                "supplier_pipeline_result": {"classification": "clarification_required", "reason": "Which hose length?"},
                "supplier_name": "HoseCo",
                "customer_name": None,
            },
        ]

    def test_bundle_contains_sections_and_events(self):
        bundle = _build_bundle(
            self._rfq_dict(),
            {"email_status": "awaiting_reply", "last_email_sent_at": None, "supplier_emails": [{"name": "HoseCo", "email": "sales@hoseco.com"}]},
            self._rows(),
            timezone.utc,
        )
        assert "RFQ-2026-0042" in bundle
        assert "Acme Mining" in bundle
        assert "## Supplier contacts" in bundle
        assert "## Line items" in bundle
        assert "Hydraulic hose" in bundle
        assert "## Email timeline" in bundle
        assert "clarification_required: Which hose length?" in bundle
        assert "## Quoted suppliers on items" in bundle
        assert "HoseCo" in bundle and "12.5" in bundle

    def test_bundle_handles_empty_data(self):
        bundle = _build_bundle(
            {
                "rfq_number": "RFQ-2026-0042", "customer": "X", "status": "draft",
                "created_date": "", "notes": "", "items": [],
            },
            {"email_status": None, "last_email_sent_at": None, "supplier_emails": None},
            [],
            timezone.utc,
        )
        assert "## Email timeline" in bundle
        assert bundle.strip().endswith("## Quoted suppliers on items")


class TestDecorateDates:
    def test_datetime_wrapped_with_brisbane_epoch(self):
        from zoneinfo import ZoneInfo

        out = decorate_dates("Latest response: 2026-08-01 14:30")
        m = re.search(r'data-ts="(\d+)"', out)
        assert m
        expected = int(datetime(2026, 8, 1, 14, 30, tzinfo=ZoneInfo("Australia/Brisbane")).timestamp())
        assert int(m.group(1)) == expected
        assert "2026-08-01 14:30" in out
        assert 'class="ts"' in out

    def test_date_only_wrapped(self):
        out = decorate_dates("RFQ created: 2026-08-01")
        assert 'data-ts=' in out
        assert "2026-08-01" in out

    def test_non_dates_untouched(self):
        s = "RFQ-2026-0042 has 3 lines, no dates here"
        assert decorate_dates(s) == s

    def test_malformed_date_untouched(self):
        s = "Bad date 2026-13-99"
        assert decorate_dates(s) == s

    def test_empty_string(self):
        assert decorate_dates("") == ""


class TestBuildDatesSection:
    def _row(self, direction, ts, supplier=False, customer=False):
        return {
            "direction": direction,
            "sent_at": ts,
            "created_at": None,
            "supplier_id": "s1" if supplier else None,
            "supplier_name": "HoseCo" if supplier else None,
            "customer_id": "c1" if customer else None,
            "customer_name": "Acme" if customer else None,
        }

    def test_computes_all_four_dates(self):
        from zoneinfo import ZoneInfo

        from includes.tools.comms_summary import _build_dates_section

        brisbane = ZoneInfo("Australia/Brisbane")
        rows = [
            self._row("received", datetime(2026, 8, 12, 1, 45, tzinfo=timezone.utc), customer=True),
            self._row("sent", datetime(2026, 8, 12, 2, 39, tzinfo=timezone.utc), supplier=True),
            self._row("received", datetime(2026, 8, 12, 23, 5, tzinfo=timezone.utc), supplier=True),
        ]
        rfq = types.SimpleNamespace(created_date=datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc))

        section = _build_dates_section(rfq, rows, brisbane)

        assert section.startswith("## Dates")
        # 01:45 UTC = 11:45 Brisbane
        assert "Customer's initial request was received: 2026-08-12 11:45" in section
        # midnight UTC = 10:00 Brisbane (not midnight) → time shown
        assert "RFQ was created: 2026-08-12 10:00" in section
        # 02:39 UTC = 12:39 Brisbane
        assert "Suppliers were first contacted: 2026-08-12 12:39" in section
        # 23:05 UTC = 09:05 Brisbane next day
        assert "Latest supplier response: 2026-08-13 09:05" in section

    def test_created_at_local_midnight_is_date_only(self):
        from zoneinfo import ZoneInfo

        from includes.tools.comms_summary import _build_dates_section

        brisbane = ZoneInfo("Australia/Brisbane")
        rfq = types.SimpleNamespace(created_date=datetime(2026, 8, 12, 0, 0, tzinfo=brisbane))
        section = _build_dates_section(rfq, [], brisbane)
        assert "RFQ was created: 2026-08-12" in section
        assert "RFQ was created: 2026-08-12 00:00" not in section

    def test_empty_rows_show_none_yet(self):
        from zoneinfo import ZoneInfo

        from includes.tools.comms_summary import _build_dates_section

        section = _build_dates_section(types.SimpleNamespace(created_date=None), [], ZoneInfo("Australia/Brisbane"))
        assert "Customer's initial request was received: (none yet)" in section
        assert "RFQ was created: (unknown)" in section
        assert "Suppliers were first contacted: (none yet)" in section
        assert "Latest supplier response: (none yet)" in section


class TestStripDatesSection:
    def test_strips_dates_section(self):
        from includes.tools.comms_summary import _strip_dates_section

        md = "## Dates\n- x\n\n## Quotes received\n1. **A**"
        assert _strip_dates_section(md) == "## Quotes received\n1. **A**"

    def test_no_dates_section_untouched(self):
        from includes.tools.comms_summary import _strip_dates_section

        md = "## Quotes received\n1. **A**"
        assert _strip_dates_section(md) == md


# ---------------------------------------------------------------------------
# get_or_generate_summary (mocked session)
# ---------------------------------------------------------------------------

def _fake_rfq(comms_summary=None, **kw):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        rfq_number="RFQ-2026-0042",
        email_status=None,
        last_email_sent_at=None,
        supplier_emails=[],
        items=[],
        comms_summary=comms_summary,
        **kw,
    )


def _patch_summary_env(rfq):
    """Patch _get_session and fingerprint helpers around get_or_generate_summary."""
    mocks = {}
    mocks["session"] = MagicMock()
    mocks["session"].query.return_value.filter.return_value.first.return_value = rfq
    return mocks


class TestGetOrGenerateSummary:
    def test_cache_hit_returns_cached_markdown(self):
        from includes.tools.comms_summary import get_or_generate_summary

        cache_key = compute_cache_key({"count": 1, "max_id": 1}, "qfp")
        rfq = _fake_rfq(
            comms_summary={"cache_key": cache_key, "generated_at": "2026-08-14T00:00:00+00:00", "markdown": "## Dates\n- today"},
        )
        m = _patch_summary_env(rfq)

        with patch("includes.tools.comms_summary._get_session", return_value=m["session"]), \
             patch("includes.tools.comms_summary._email_fingerprint", return_value={"count": 1, "max_id": 1}), \
             patch("includes.tools.comms_summary._quotes_fingerprint", return_value="qfp"), \
             patch("includes.tools.comms_summary.llm_call_with_retry") as mock_llm:
            result = get_or_generate_summary("RFQ-2026-0042")

        assert result["status"] == "ok"
        assert result["from_cache"] is True
        assert result["markdown"] == "## Dates\n- today"
        mock_llm.assert_not_called()

    def test_cache_miss_generates_and_persists(self):
        from includes.tools.comms_summary import get_or_generate_summary

        rfq = _fake_rfq()
        m = _patch_summary_env(rfq)
        fake_response = MagicMock()
        fake_response.text = "```md\n## Dates\n- generated\n\n## Quotes received\n1. **HoseCo** quoted 2026-08-12 10:00\n```"

        with patch("includes.tools.comms_summary._get_session", return_value=m["session"]), \
             patch("includes.tools.comms_summary._email_fingerprint", return_value={"count": 2, "max_id": 9}), \
             patch("includes.tools.comms_summary._quotes_fingerprint", return_value="qfp2"), \
             patch("includes.tools.rfq_crud._get_rfq_dict_sync", return_value={
                 "rfq_number": "RFQ-2026-0042", "customer": "X", "status": "draft",
                 "created_date": "", "notes": "", "items": [],
             }), \
             patch("includes.tools.comms_summary._email_rows", return_value=[]), \
             patch("includes.tools.comms_summary.llm_call_with_retry", return_value=fake_response) as mock_llm:
            result = get_or_generate_summary("RFQ-2026-0042")

        assert result["status"] == "ok"
        assert result["from_cache"] is False
        assert result["markdown"].startswith("## Dates\n")
        # LLM's own Dates section is stripped; deterministic section is prepended
        assert "- generated" not in result["markdown"]
        assert "## Quotes received\n1. **HoseCo** quoted 2026-08-12 10:00" in result["markdown"]
        mock_llm.assert_called_once()
        m["session"].commit.assert_called_once()
        assert rfq.comms_summary["markdown"] == result["markdown"]

    def test_force_bypasses_cache(self):
        from includes.tools.comms_summary import get_or_generate_summary

        cache_key = compute_cache_key({"count": 1, "max_id": 1}, "qfp")
        rfq = _fake_rfq(comms_summary={"cache_key": cache_key, "generated_at": "x", "markdown": "# old"})
        m = _patch_summary_env(rfq)
        fake_response = MagicMock()
        fake_response.text = "## Quotes received\n1. **HoseCo** quoted"

        with patch("includes.tools.comms_summary._get_session", return_value=m["session"]), \
             patch("includes.tools.comms_summary._email_fingerprint", return_value={"count": 1, "max_id": 1}), \
             patch("includes.tools.comms_summary._quotes_fingerprint", return_value="qfp"), \
             patch("includes.tools.rfq_crud._get_rfq_dict_sync", return_value={
                 "rfq_number": "RFQ-2026-0042", "customer": "X", "status": "draft",
                 "created_date": "", "notes": "", "items": [],
             }), \
             patch("includes.tools.comms_summary._email_rows", return_value=[]), \
             patch("includes.tools.comms_summary.llm_call_with_retry", return_value=fake_response):
            result = get_or_generate_summary("RFQ-2026-0042", force=True)

        assert result["from_cache"] is False
        assert result["markdown"].startswith("## Dates\n")
        assert "1. **HoseCo** quoted" in result["markdown"]

    def test_rfq_not_found(self):
        from includes.tools.comms_summary import get_or_generate_summary

        m = _patch_summary_env(None)
        with patch("includes.tools.comms_summary._get_session", return_value=m["session"]):
            result = get_or_generate_summary("RFQ-NOPE")
        assert result["status"] == "error"
        assert result["http_status"] == 404

    def test_llm_failure_returns_error(self):
        from includes.tools.comms_summary import get_or_generate_summary

        rfq = _fake_rfq()
        m = _patch_summary_env(rfq)
        with patch("includes.tools.comms_summary._get_session", return_value=m["session"]), \
             patch("includes.tools.comms_summary._email_fingerprint", return_value={"count": 0, "max_id": 0}), \
             patch("includes.tools.comms_summary._quotes_fingerprint", return_value="qfp"), \
             patch("includes.tools.rfq_crud._get_rfq_dict_sync", return_value={
                 "rfq_number": "RFQ-2026-0042", "customer": "X", "status": "draft",
                 "created_date": "", "notes": "", "items": [],
             }), \
             patch("includes.tools.comms_summary._email_rows", return_value=[]), \
             patch("includes.tools.comms_summary.llm_call_with_retry", side_effect=RuntimeError("boom")):
            result = get_or_generate_summary("RFQ-2026-0042")

        assert result["status"] == "error"
        assert result["http_status"] == 500


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

def _make_test_app():
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


class TestCommsSummaryEndpoint:
    @pytest.fixture
    def client(self):
        return TestClient(_make_test_app())

    def test_requires_auth(self, client):
        resp = client.get("/api/rfqs/RFQ-2026-0042/comms-summary", follow_redirects=False)
        assert resp.status_code == 303

    def test_returns_summary(self, client):
        client.get("/_test/login")
        with patch(
            "includes.tools.comms_summary.get_or_generate_summary",
            return_value={
                "status": "ok",
                "markdown": "## Dates\n- today",
                "generated_at": "2026-08-14T00:00:00+00:00",
                "from_cache": True,
            },
        ) as mock_gen:
            resp = client.get("/api/rfqs/RFQ-2026-0042/comms-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["from_cache"] is True
        mock_gen.assert_called_once_with("RFQ-2026-0042", False)

    def test_refresh_flag_forces_regeneration(self, client):
        client.get("/_test/login")
        with patch(
            "includes.tools.comms_summary.get_or_generate_summary",
            return_value={"status": "ok", "markdown": "# x", "generated_at": "x", "from_cache": False},
        ) as mock_gen:
            client.get("/api/rfqs/RFQ-2026-0042/comms-summary?refresh=1")
        mock_gen.assert_called_once_with("RFQ-2026-0042", True)

    def test_error_status_propagated(self, client):
        client.get("/_test/login")
        with patch(
            "includes.tools.comms_summary.get_or_generate_summary",
            return_value={"status": "error", "message": "RFQ not found", "http_status": 404},
        ):
            resp = client.get("/api/rfqs/RFQ-NOPE/comms-summary")
        assert resp.status_code == 404
        assert resp.json()["message"] == "RFQ not found"


class TestRfqSummaryEndpoint:
    @pytest.fixture
    def client(self):
        return TestClient(_make_test_app())

    def _rfq_dict(self):
        return {
            "id": "RFQ-2026-0042",
            "rfq_number": "RFQ-2026-0042",
            "customer": "Acme Mining",
            "status": "in_progress",
            "created_date": "2026-08-12",
            "title": "",
            "notes": "",
            "reference": "",
            "netsuite_opportunity": "",
            "hubspot_deal": "",
            "pipeline_stage": "complete",
            "assigned_to": "staff@eagle.com",
            "customer_contact": {"name": "Jane", "email": "jane@acme.com"},
            "items": [
                {
                    "line": 1,
                    "input_description": "Hydraulic hose",
                    "input_code": "HH-100",
                    "quantity": 5,
                    "uom": "ea",
                    "match": "specific",
                    "part_number": "HH-100",
                    "suppliers": [{"name": "HoseCo", "price": 12.5}],
                }
            ],
        }

    def test_requires_auth(self, client):
        resp = client.get("/api/rfqs/RFQ-2026-0042/summary-md", follow_redirects=False)
        assert resp.status_code == 303

    def test_returns_markdown_summary(self, client):
        client.get("/_test/login")
        with patch("includes.tools.rfq_crud._get_rfq_dict_sync", return_value=self._rfq_dict()):
            resp = client.get("/api/rfqs/RFQ-2026-0042/summary-md")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "RFQ-2026-0042" in data["markdown"]
        assert "Acme Mining" in data["markdown"]
        # includes the quotation snapshot (view_rfq_quotation view)
        assert "Quotation Status" in data["markdown"]
        assert "Price Matrix" in data["markdown"]

    def test_not_found(self, client):
        client.get("/_test/login")
        with patch("includes.tools.rfq_crud._get_rfq_dict_sync", return_value=None):
            resp = client.get("/api/rfqs/RFQ-NOPE/summary-md")
        assert resp.status_code == 404
        assert resp.json()["message"] == "RFQ not found"
