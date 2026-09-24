"""Tests for includes.timeutil and the RFQ created-date rendering.

Regression cover for the bug where /rfqs and the RFQ detail page displayed the
creation time in UTC (10 hours behind AEST, and on the wrong calendar day for
anything created before 10am AEST).
"""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from includes import timeutil


@pytest.fixture
def brisbane(monkeypatch):
    """Pin the display timezone to Brisbane (UTC+10, no DST)."""
    monkeypatch.setattr(timeutil.config, "TIMEZONE", "Australia/Brisbane")
    return ZoneInfo("Australia/Brisbane")


def _stub_rfq(**overrides):
    """Minimal RFQ stand-in for _rfq_to_dict (empty item list)."""
    base = dict(
        rfq_number="RFQ-2026-1236",
        customer="Acme",
        customer_id=None,
        customer_contact=None,
        reference=None,
        netsuite_opportunity=None,
        opportunity_id=None,
        hubspot_deal=None,
        quote_brand_id=None,
        quote_brand=None,
        created_by=None,
        created_date=datetime(2026, 9, 23, 9, 37, 40, tzinfo=timezone.utc),
        assigned_to=None,
        thread_id=None,
        status="draft",
        title=None,
        notes=None,
        history=None,
        item_groups=None,
        opportunity_sync_state=None,
        pipeline_stage="unprocessed",
        pipeline_activity=None,
        supplier_meta=None,
        items=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestDisplayTz:
    def test_matches_config(self, monkeypatch):
        monkeypatch.setattr(timeutil.config, "TIMEZONE", "Australia/Brisbane")
        assert str(timeutil.display_tz()) == "Australia/Brisbane"

    def test_honours_config_change(self, monkeypatch):
        monkeypatch.setattr(timeutil.config, "TIMEZONE", "Australia/Perth")
        assert str(timeutil.display_tz()) == "Australia/Perth"


class TestToLocal:
    def test_utc_shifts_to_display_tz(self, brisbane):
        # 09:37 UTC == 19:37 AEST
        got = timeutil.to_local(datetime(2026, 9, 23, 9, 37, 40, tzinfo=timezone.utc))
        assert (got.hour, got.minute) == (19, 37)
        assert got.utcoffset() == timedelta(hours=10)

    def test_naive_is_assumed_utc(self, brisbane):
        got = timeutil.to_local(datetime(2026, 9, 23, 9, 37))
        assert (got.hour, got.minute) == (19, 37)
        assert got.tzinfo is not None

    def test_rolls_over_to_next_local_day(self, brisbane):
        # 22:30 UTC on the 23rd == 08:30 AEST on the 24th
        got = timeutil.to_local(datetime(2026, 9, 23, 22, 30, tzinfo=timezone.utc))
        assert got.strftime("%Y-%m-%d %H:%M") == "2026-09-24 08:30"

    def test_aware_input_preserves_the_instant(self, brisbane):
        already_local = datetime(2026, 9, 23, 19, 37, tzinfo=brisbane)
        got = timeutil.to_local(already_local)
        assert (got.hour, got.minute) == (19, 37)

    def test_respects_configured_timezone(self, monkeypatch):
        monkeypatch.setattr(timeutil.config, "TIMEZONE", "Australia/Perth")
        got = timeutil.to_local(datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc))
        assert got.strftime("%H:%M") == "17:00"

    def test_plain_date_raises(self, brisbane):
        with pytest.raises(TypeError, match="expects a datetime"):
            timeutil.to_local(date(2026, 9, 23))


class TestRfqToDictCreatedDate:
    """_rfq_to_dict must render created_date/created_display in config.TIMEZONE."""

    def test_created_display_uses_display_timezone(self, brisbane):
        from includes.tools.rfq_crud import _rfq_to_dict

        d = _rfq_to_dict(_stub_rfq())

        assert d["created_date"] == "2026-09-23"
        # 09:37 UTC == 19:37 AEST — previously rendered "9:37am".
        assert d["created_display"].startswith("2026-09-23 7:37pm (")
        assert "9:37am" not in d["created_display"]

    def test_date_rolls_to_next_local_day(self, brisbane):
        from includes.tools.rfq_crud import _rfq_to_dict

        d = _rfq_to_dict(
            _stub_rfq(created_date=datetime(2026, 9, 23, 22, 30, tzinfo=timezone.utc))
        )

        # Previously showed the 23rd (UTC date).
        assert d["created_date"] == "2026-09-24"
        assert d["created_display"].startswith("2026-09-24 8:30am (")

    def test_legacy_date_row_keeps_date_and_omits_time(self, brisbane):
        from includes.tools.rfq_crud import _rfq_to_dict

        d = _rfq_to_dict(_stub_rfq(created_date=date(2026, 9, 23)))

        assert d["created_date"] == "2026-09-23"
        assert d["created_display"].startswith("2026-09-23 (")
        assert ":" not in d["created_display"].split("(")[0]

    def test_missing_date_is_blank(self, brisbane):
        from includes.tools.rfq_crud import _rfq_to_dict

        d = _rfq_to_dict(_stub_rfq(created_date=None))

        assert d["created_date"] == ""
        assert d["created_display"] == ""
        assert d["age_hours"] == 0

    def test_age_hours_is_timezone_independent(self, monkeypatch):
        from includes.tools.rfq_crud import _rfq_to_dict

        created = datetime.now(timezone.utc) - timedelta(hours=10)

        monkeypatch.setattr(timeutil.config, "TIMEZONE", "Australia/Brisbane")
        brisbane_age = _rfq_to_dict(_stub_rfq(created_date=created))["age_hours"]

        monkeypatch.setattr(timeutil.config, "TIMEZONE", "Australia/Perth")
        perth_age = _rfq_to_dict(_stub_rfq(created_date=created))["age_hours"]

        assert brisbane_age == perth_age == 10
