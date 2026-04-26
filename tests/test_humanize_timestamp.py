"""Tests for _humanize_timestamp in dashboard_routes."""

import pytest
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch

from includes.dashboard.routes import _humanize_timestamp


# Helper: build an ISO UTC timestamp for "now minus delta" in the configured tz
def _iso(days_ago: int = 0, hours_ago: int = 0, minutes_ago: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago, minutes=minutes_ago)
    return dt.isoformat()


class TestHumanizeTimestamp:
    def test_none_returns_dash(self):
        label, exact = _humanize_timestamp(None)
        assert label == "—"
        assert exact == ""

    def test_empty_string_returns_dash(self):
        label, exact = _humanize_timestamp("")
        assert label == "—"
        assert exact == ""

    def test_today(self):
        label, exact = _humanize_timestamp(_iso(days_ago=0, minutes_ago=5))
        assert label.startswith("Today")
        assert "AM" in label or "PM" in label
        assert exact  # non-empty exact time

    def test_yesterday(self):
        label, exact = _humanize_timestamp(_iso(days_ago=1))
        assert label.startswith("Yesterday")
        assert exact

    def test_few_days_ago(self):
        label, _ = _humanize_timestamp(_iso(days_ago=3))
        assert "3 days ago" in label

    def test_last_week(self):
        label, _ = _humanize_timestamp(_iso(days_ago=8))
        assert label == "Last week"

    def test_weeks_ago(self):
        label, _ = _humanize_timestamp(_iso(days_ago=20))
        # 20 // 7 = 2
        assert "2 weeks ago" in label

    def test_one_month_ago(self):
        label, _ = _humanize_timestamp(_iso(days_ago=35))
        assert "month" in label
        assert "months" not in label  # singular

    def test_months_ago(self):
        label, _ = _humanize_timestamp(_iso(days_ago=90))
        assert "months ago" in label

    def test_over_a_year_ago(self):
        label, _ = _humanize_timestamp(_iso(days_ago=400))
        # Should be like "Dec 2024"
        assert len(label) >= 4  # e.g. "Dec 2024"

    def test_z_suffix_parsed(self):
        """Timestamps ending in Z (common UTC format) should parse fine."""
        ts = "2026-01-15T10:30:00Z"
        label, exact = _humanize_timestamp(ts)
        assert exact  # parsed successfully

    def test_naive_timestamp(self):
        """Naive ISO strings (no tz) should be treated as UTC."""
        ts = "2026-04-26T05:00:00"
        label, exact = _humanize_timestamp(ts)
        assert exact  # parsed

    def test_malformed_returns_truncated(self):
        """Unparseable strings should return the first 16 chars."""
        label, exact = _humanize_timestamp("not-a-valid-timestamp-at-all")
        assert label == "not-a-valid-time"
        assert exact == "not-a-valid-timestamp-at-all"

    def test_exact_format(self):
        """Exact string should be YYYY-MM-DD HH:MM:SS."""
        _, exact = _humanize_timestamp(_iso(days_ago=2))
        # e.g. "2026-04-24 14:03:22"
        parts = exact.split(" ")
        assert len(parts) == 2
        assert len(parts[0].split("-")) == 3
        assert len(parts[1].split(":")) == 3
