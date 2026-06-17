"""Fixed SQLAlchemy data layer for Chainlit.

Fixes upstream Chainlit bugs (still present in 2.11.1):
- get_current_timestamp() uses datetime.now() (local time) with "Z" suffix,
  producing timestamps that claim to be UTC but are actually local time.
- update_thread() can be called with user_id=None when the WebSocket session
  has a plain User instead of a PersistedUser (race condition in auth flow).
  Our override ensures userId is never overwritten with NULL on an existing
  thread, and serializes tags correctly for the VARCHAR column.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from chainlit.data.sql_alchemy import SQLAlchemyDataLayer

logger = logging.getLogger(__name__)


class FixedSQLAlchemyDataLayer(SQLAlchemyDataLayer):
    """Patches for upstream Chainlit SQLAlchemyDataLayer bugs.

    1. get_current_timestamp() — use proper UTC instead of local time.
    2. update_thread() — prevent userId/userIdentifier from being lost when
       the emitter calls update_thread with user_id=None (which happens when
       the WebSocket auth fails to return a PersistedUser).
    """

    async def get_current_timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    async def update_thread(
        self,
        thread_id: str,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        tags: Optional[List[str]] = None,
    ) -> None:
        if self.show_logger:
            logger.info(f"SQLAlchemy: update_thread, thread_id={thread_id}")

        user_identifier = None
        if user_id:
            user_identifier = await self._get_user_identifer_by_id(user_id)

        # Single query to check existence, get existing metadata and userId
        existing_thread = await self.execute_sql(
            query='SELECT "id", "userId", "metadata" FROM threads WHERE "id" = :id',
            parameters={"id": thread_id},
        )
        is_new_thread = not (isinstance(existing_thread, list) and existing_thread)

        # Early return: existing thread with no updates (same as upstream)
        has_updates = (
            metadata is not None
            or name is not None
            or user_id is not None
            or tags is not None
        )
        if not is_new_thread and not has_updates:
            return

        # Merge metadata with existing
        if metadata is not None:
            base = {}
            if not is_new_thread:
                raw = existing_thread[0].get("metadata") or {}
                if isinstance(raw, str):
                    try:
                        base = json.loads(raw)
                    except json.JSONDecodeError:
                        base = {}
                elif isinstance(raw, dict):
                    base = raw
            to_delete = {k for k, v in metadata.items() if v is None}
            incoming = {k: v for k, v in metadata.items() if v is not None}
            base = {k: v for k, v in base.items() if k not in to_delete}
            metadata = {**base, **incoming}

        name_value = name
        if name_value is None and metadata:
            name_value = metadata.get("name")

        # Only set createdAt on new threads (preserves original timestamp)
        created_at_value = await self.get_current_timestamp() if is_new_thread else None

        # FIX: If user_id is None but thread already has a userId, preserve it.
        # This prevents the emitter's flush_thread_queues from wiping userId
        # when session.user is a plain User instead of PersistedUser.
        if not user_id and not is_new_thread:
            existing_user_id = existing_thread[0].get("userId") if existing_thread else None
            if existing_user_id:
                user_id = existing_user_id
                user_identifier = await self._get_user_identifer_by_id(user_id)

        data = {
            "id": thread_id,
            "createdAt": created_at_value,
            "name": name_value,
            "userId": user_id,
            "userIdentifier": user_identifier,
            "tags": ",".join(tags) if isinstance(tags, list) else tags,
            "metadata": json.dumps(metadata) if metadata else None,
        }
        parameters = {
            key: value for key, value in data.items() if value is not None
        }
        columns = ", ".join(f'"{key}"' for key in parameters.keys())
        values = ", ".join(f":{key}" for key in parameters.keys())
        # Exclude createdAt from UPDATE to preserve original creation time
        updates = ", ".join(
            f'"{key}" = EXCLUDED."{key}"'
            for key in parameters.keys()
            if key not in ("id", "createdAt")
        )

        if updates:
            query = f"""
                INSERT INTO threads ({columns})
                VALUES ({values})
                ON CONFLICT ("id") DO UPDATE
                SET {updates};
            """
        else:
            query = f"""
                INSERT INTO threads ({columns})
                VALUES ({values})
                ON CONFLICT ("id") DO NOTHING;
            """
        await self.execute_sql(query=query, parameters=parameters)
