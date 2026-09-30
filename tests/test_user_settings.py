"""Tests for per-user dashboard settings (``user_settings`` table) and the
RFQ list filter resolution built on top of it."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from includes.dashboard import user_settings
from includes.dashboard.models import UserSetting
from includes.dashboard.user_settings import (
    DEFAULT_SETTINGS,
    _deep_merge,
    get_settings,
    save_settings,
)


# ============================================================================
# Pure helpers
# ============================================================================

class TestDeepMerge:
    def test_nested_merge_keeps_siblings(self):
        base = {"a": {"x": 1, "y": 2}, "b": 3}
        merged = _deep_merge(base, {"a": {"y": 99, "z": 4}})
        assert merged == {"a": {"x": 1, "y": 99, "z": 4}, "b": 3}

    def test_scalar_replaces_dict(self):
        assert _deep_merge({"a": {"x": 1}}, {"a": None}) == {"a": None}

    def test_does_not_mutate_base(self):
        base = {"a": {"x": 1}}
        _deep_merge(base, {"a": {"x": 2}})
        assert base == {"a": {"x": 1}}


# ============================================================================
# Storage round-trip (real local Postgres, like the other dashboard tests)
# ============================================================================

class TestStorage:
    def test_defaults_when_no_row(self):
        email = f"pytest-us-{uuid.uuid4()}@example.com"
        session = user_settings.get_session()
        try:
            settings = get_settings(session, email)
            assert settings == DEFAULT_SETTINGS
        finally:
            session.close()

    def test_save_merges_and_preserves_other_keys(self):
        from includes.dashboard.database import get_session

        email = f"pytest-us-{uuid.uuid4()}@example.com"
        session = get_session()
        try:
            save_settings(session, email, {"rfq_filters": {"mine": "all"}})
            # Partial override wins; the rest of the defaults survive.
            after_filter = get_settings(session, email)
            assert after_filter["rfq_filters"]["mine"] == "all"
            assert after_filter["rfq_filters"]["status"] == "open"
            assert after_filter["rfq_filters"]["sort"] == "rfq_number"

            # A later patch to a different group must not clobber the first.
            save_settings(session, email, {"ui": {"chat_open": False}})
            after_ui = get_settings(session, email)
            assert after_ui["rfq_filters"]["mine"] == "all"
            assert after_ui["ui"]["chat_open"] is False
        finally:
            session.query(UserSetting).filter(UserSetting.user_email == email).delete()
            session.commit()
            session.close()


# ============================================================================
# Async wrappers degrade gracefully
# ============================================================================

async def test_load_falls_back_to_defaults_on_error(monkeypatch):
    monkeypatch.setattr(user_settings, "load_sync", lambda email: (_ for _ in ()).throw(RuntimeError("db down")))
    assert await user_settings.load_user_settings("x@example.com") == DEFAULT_SETTINGS


# ============================================================================
# RFQ filter resolution
# ============================================================================

SAVED = {
    "rfq_filters": {
        "mine": "all",
        "status": "quoted",
        "sort": "customer",
        "order": "asc",
        "q": "widget",
    }
}


class TestResolveRfqFilters:
    async def test_absent_params_fall_back_to_saved(self):
        from includes.dashboard.routes import rfqs

        with patch.object(rfqs, "load_user_settings", AsyncMock(return_value=SAVED)), \
                patch.object(rfqs, "persist_user_settings", AsyncMock()) as save:
            resolved = await rfqs._resolve_rfq_filters(
                "u@example.com", q=None, mine=None, status=None, sort=None, order=None)

        assert resolved == SAVED["rfq_filters"]
        save.assert_not_awaited()

    async def test_explicit_param_wins_and_is_persisted(self):
        from includes.dashboard.routes import rfqs

        with patch.object(rfqs, "load_user_settings", AsyncMock(return_value=SAVED)), \
                patch.object(rfqs, "persist_user_settings", AsyncMock()) as save:
            resolved = await rfqs._resolve_rfq_filters(
                "u@example.com", q=None, mine="1", status=None, sort=None, order=None)

        # The explicit owner wins; everything else comes from what was saved.
        assert resolved["mine"] == "1"
        assert resolved["status"] == "quoted"
        assert resolved["q"] == "widget"
        save.assert_awaited_once()
        assert save.await_args.args[1]["rfq_filters"]["mine"] == "1"

    async def test_no_write_when_nothing_changed(self):
        from includes.dashboard.routes import rfqs

        with patch.object(rfqs, "load_user_settings", AsyncMock(return_value=SAVED)), \
                patch.object(rfqs, "persist_user_settings", AsyncMock()) as save:
            # The infinite-scroll request re-sends the current filters verbatim.
            await rfqs._resolve_rfq_filters(
                "u@example.com", q="widget", mine="all", status="quoted",
                sort="customer", order="asc")

        save.assert_not_awaited()
