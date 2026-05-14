"""Fixed SQLAlchemy data layer for Chainlit.

Fixes upstream Chainlit bug (still present in 2.11.1):
- get_current_timestamp() uses datetime.now() (local time) with "Z" suffix,
  producing timestamps that claim to be UTC but are actually local time.

The update_thread() createdAt-overwrite bug was fixed upstream in 2.10.0.
"""

import logging
from datetime import datetime, timezone

from chainlit.data.sql_alchemy import SQLAlchemyDataLayer

logger = logging.getLogger(__name__)


class FixedSQLAlchemyDataLayer(SQLAlchemyDataLayer):
    """Fix upstream Chainlit bug: get_current_timestamp() uses local time.

    Chainlit's default implementation does ``datetime.now().isoformat() + "Z"``
    which produces a local-time timestamp with a misleading UTC suffix.
    We override it to use proper ``datetime.now(timezone.utc).isoformat()``.
    """

    async def get_current_timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat()
