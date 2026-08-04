"""Simple key-value access for the system_settings table.

Usage:
    from includes.system_settings import get_setting, set_setting

    depts = get_setting("departments", default=[])
    set_setting("departments", [{"netsuite_id": "1", "name": "Machine Parts"}],
                description="NetSuite department list", updated_by="netsuite_sync")
"""

import logging
from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.orm import Session

from includes.dashboard.models import SystemSetting

logger = logging.getLogger(__name__)


def get_setting(session: Session, key: str, default=None):
    """Get a setting value, returning default if not found."""
    row = session.query(SystemSetting).filter(SystemSetting.key == key).first()
    if row is None:
        return default
    return row.value


def set_setting(
    session: Session,
    key: str,
    value,
    *,
    description: str | None = None,
    updated_by: str | None = None,
    upsert: bool = True,
):
    """Set a setting value, creating or updating as needed."""
    row = session.query(SystemSetting).filter(SystemSetting.key == key).first()
    if row is None:
        if not upsert:
            raise KeyError(f"Setting '{key}' not found")
        row = SystemSetting(key=key, value=value)
        session.add(row)
    else:
        row.value = value

    if description is not None:
        row.description = description
    if updated_by is not None:
        row.updated_by = updated_by
    row.updated_at = datetime.now(timezone.utc)

    return row


def list_settings(session: Session) -> list[dict]:
    """Return all settings as a list of dicts (key, description, updated_at)."""
    rows = session.execute(
        text("""
            SELECT key, description, updated_at, updated_by
            FROM system_settings
            ORDER BY key
        """)
    ).mappings().all()
    return [dict(r) for r in rows]
