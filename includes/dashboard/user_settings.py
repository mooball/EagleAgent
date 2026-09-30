"""Per-user dashboard settings — one JSON blob per user, keyed by email.

The blob is grouped by feature so new preferences can be added without a
migration. Anything stored here is a *user-visible server-side* dashboard
preference, not agent memory (that lives in the LangGraph store):

    {
      "rfq_filters": {"mine": "1", "status": "open",
                       "sort": "rfq_number", "order": "desc", "q": ""}
    }

Pure client layout state (chat panel width/open-close) deliberately stays in
``localStorage`` — it needs no round trip and is per-browser. Only overrides
are persisted; :func:`get_settings` merges them on top of
:data:`DEFAULT_SETTINGS`, so a missing row or key always yields a usable dict.

Reads are best-effort: a failure (missing table before migration, DB blip)
logs a warning and falls back to the defaults, so dashboard rendering never
depends on this table being present.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from includes.dashboard.database import get_session
from includes.dashboard.models import UserSetting

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS: dict[str, Any] = {
    "rfq_filters": {
        "mine": "1",
        "status": "open",
        "sort": "rfq_number",
        "order": "desc",
        "q": "",
    },
}


def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge ``patch`` into a copy of ``base`` (patch wins)."""
    merged = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def get_settings(session: Session, email: str) -> dict:
    """Return the user's settings merged over the defaults."""
    row = session.query(UserSetting).filter(UserSetting.user_email == email).first()
    stored = row.settings if row is not None and isinstance(row.settings, dict) else {}
    return _deep_merge(DEFAULT_SETTINGS, stored)


def save_settings(session: Session, email: str, patch: dict) -> dict:
    """Deep-merge ``patch`` into what is stored, upsert, and return the result.

    Merges against the *raw stored* blob (not the defaults), so the row holds
    only overrides.
    """
    row = session.query(UserSetting).filter(UserSetting.user_email == email).first()
    stored = row.settings if row is not None and isinstance(row.settings, dict) else {}
    merged = _deep_merge(stored, patch)
    if row is None:
        session.add(UserSetting(user_email=email, settings=merged))
    else:
        row.settings = merged
        row.updated_at = datetime.now(timezone.utc)
    session.commit()
    return _deep_merge(DEFAULT_SETTINGS, merged)


def load_sync(email: str) -> dict:
    """Blocking read used via :func:`asyncio.to_thread`."""
    session = get_session()
    try:
        return get_settings(session, email)
    finally:
        session.close()


def persist_sync(email: str, patch: dict) -> dict:
    """Blocking write used via :func:`asyncio.to_thread`."""
    session = get_session()
    try:
        return save_settings(session, email, patch)
    finally:
        session.close()


async def load_user_settings(email: str) -> dict:
    """Async read; falls back to defaults on any storage error."""
    if not email:
        return copy.deepcopy(DEFAULT_SETTINGS)
    try:
        return await asyncio.to_thread(load_sync, email)
    except Exception:
        logger.warning("user_settings: load failed for %s", email, exc_info=True)
        return copy.deepcopy(DEFAULT_SETTINGS)


async def persist_user_settings(email: str, patch: dict) -> dict:
    """Async deep-merge write; returns the effective settings."""
    if not email:
        return copy.deepcopy(DEFAULT_SETTINGS)
    try:
        return await asyncio.to_thread(persist_sync, email, patch)
    except Exception:
        logger.warning("user_settings: save failed for %s", email, exc_info=True)
        return copy.deepcopy(DEFAULT_SETTINGS)
