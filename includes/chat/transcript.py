"""Track A transcript adapter — read/write Chainlit's `threads` / `steps`.

Parent plan §11: keep the Chainlit schema, swap the writer. The new UI and
Chainlit share these tables so threads appear in both UIs, with zero migration.
Only talks to the configured Chainlit data layer — never to Chainlit sessions.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Step columns the UI renders. Deliberately excludes Chainlit-only columns
#: (playerConfig, command, modes, ...) we never read.
STEP_COLUMNS = (
    '"id","type","name","output","input","createdAt","metadata",'
    '"parentId","isError","streaming"'
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _data_layer() -> Any:
    """The configured Chainlit data layer (same instance Chainlit uses)."""
    from chainlit.data import get_data_layer

    dl = get_data_layer()
    if dl is None:
        raise RuntimeError("Chainlit data layer not configured")
    return dl


async def ensure_user(user_email: str, user_name: str | None = None) -> str:
    """Return the Chainlit user id for ``user_email``, creating the user row
    if needed so ``threads.userId``/``userIdentifier`` resolve the same way
    Chainlit resolves them."""
    dl = await _data_layer()
    persisted = await dl.get_user(user_email)
    if persisted is not None:
        return str(getattr(persisted, "id", "") or "")
    from chainlit.user import User as CLUser

    created = await dl.create_user(
        CLUser(identifier=user_email, metadata={"name": user_name or user_email})
    )
    return str(getattr(created, "id", "") or "")


# ── Threads ────────────────────────────────────────────────────────────────


async def create_thread(
    user_email: str,
    *,
    user_name: str | None = None,
    name: str = "New chat",
    agent_key: str = "eagle",
    thread_id: str | None = None,
) -> str:
    """Create (upsert) a thread row and return its id.

    The id is the LangGraph ``thread_id`` — the load-bearing invariant from the
    parent plan (threads.id == thread_id == rfq_threads.thread_id).
    """
    tid = thread_id or str(uuid.uuid4())
    user_id = await ensure_user(user_email, user_name)
    dl = await _data_layer()
    await dl.update_thread(
        thread_id=tid,
        name=name,
        user_id=user_id,
        metadata={"agent": agent_key},
    )
    return tid


async def list_threads(user_email: str, limit: int = 100) -> list[dict]:
    """Threads owned by the user, newest first."""
    dl = await _data_layer()
    rows = await dl.execute_sql(
        'SELECT "id","name","createdAt","metadata" FROM threads '
        'WHERE "userIdentifier" = :email '
        'ORDER BY "createdAt" DESC LIMIT :limit',
        {"email": user_email, "limit": limit},
    )
    threads: list[dict] = []
    for row in rows or []:
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        threads.append(
            {
                "id": row.get("id"),
                "name": row.get("name") or "Untitled",
                "created_at": row.get("createdAt"),
                "agent": (metadata or {}).get("agent", "eagle"),
            }
        )
    return threads


async def get_thread(thread_id: str, user_email: str) -> Optional[dict]:
    """A thread row, but only if owned by ``user_email`` (ownership guard)."""
    dl = await _data_layer()
    rows = await dl.execute_sql(
        'SELECT "id","name","createdAt","metadata","userIdentifier" '
        'FROM threads WHERE "id" = :tid',
        {"tid": thread_id},
    )
    if not rows:
        return None
    row = rows[0]
    if (row.get("userIdentifier") or "") != user_email:
        return None
    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    return {
        "id": row.get("id"),
        "name": row.get("name") or "Untitled",
        "created_at": row.get("createdAt"),
        "metadata": metadata,
    }


async def rename_thread(thread_id: str, name: str) -> None:
    dl = await _data_layer()
    await dl.update_thread(thread_id=thread_id, name=name)


async def delete_thread(thread_id: str) -> None:
    """Delete the thread row and its steps (Chainlit's delete_thread only
    removes the thread row; clear steps explicitly so history is fully gone)."""
    dl = await _data_layer()
    await dl.execute_sql('DELETE FROM steps WHERE "threadId" = :tid', {"tid": thread_id})
    await dl.execute_sql('DELETE FROM threads WHERE "id" = :tid', {"tid": thread_id})


# ── Steps ──────────────────────────────────────────────────────────────────


async def get_steps(thread_id: str) -> list[dict]:
    """Rendered transcript of a thread, oldest first."""
    dl = await _data_layer()
    rows = await dl.execute_sql(
        f'SELECT {STEP_COLUMNS} FROM steps WHERE "threadId" = :tid '
        'ORDER BY "createdAt" ASC',
        {"tid": thread_id},
    )
    steps: list[dict] = []
    for row in rows or []:
        steps.append(
            {
                "id": row.get("id"),
                "type": row.get("type"),
                "name": row.get("name"),
                "output": row.get("output") or "",
                "input": row.get("input") or "",
                "created_at": row.get("createdAt"),
                "metadata": row.get("metadata") or {},
                "parent_id": row.get("parentId"),
                "is_error": bool(row.get("isError")),
                "streaming": bool(row.get("streaming")),
            }
        )
    return steps


def _step_dict(
    thread_id: str,
    *,
    step_id: str,
    type_: str,
    name: str,
    output: str,
    metadata: dict | None = None,
    parent_id: str | None = None,
) -> dict:
    """A Chainlit StepDict in the shape ``create_step`` expects."""
    now = _now()
    return {
        "id": step_id,
        "threadId": thread_id,
        "name": name,
        "type": type_,
        "output": output,
        "createdAt": now,
        "start": now,
        "end": now,
        "streaming": False,
        "metadata": metadata or {},
        "tags": None,
        "input": "",
        "isError": False,
        "parentId": parent_id,
        "language": None,
        "showInput": None,
        "generation": None,
        "defaultOpen": None,
        "autoCollapse": None,
    }


async def create_step(
    thread_id: str,
    *,
    type_: str = "assistant_message",
    name: str = "EagleAgent",
    output: str = "",
    metadata: dict | None = None,
    parent_id: str | None = None,
) -> str:
    """Persist a step; returns the new step id.

    Mirrors Chainlit's own ``create_step`` upsert via ``execute_sql`` — the
    public ``dl.create_step`` is wrapped in ``queue_until_user_message`` and
    requires a live Chainlit websocket session, which the beta UI doesn't have.
    """
    step_id = str(uuid.uuid4())
    step_dict = _step_dict(
        thread_id,
        step_id=step_id,
        type_=type_,
        name=name,
        output=output,
        metadata=metadata,
        parent_id=parent_id,
    )
    parameters = {
        key: value
        for key, value in step_dict.items()
        if value is not None and not (isinstance(value, dict) and not value)
    }
    parameters["metadata"] = json.dumps(step_dict.get("metadata", {}))
    parameters["generation"] = json.dumps(step_dict.get("generation", {}))
    columns = ", ".join(f'"{key}"' for key in parameters.keys())
    values = ", ".join(f":{key}" for key in parameters.keys())
    updates = ", ".join(
        f'"{key}" = :{key}' for key in parameters.keys() if key != "id"
    )
    query = f"""
        INSERT INTO steps ({columns})
        VALUES ({values})
        ON CONFLICT (id) DO UPDATE
        SET {updates};
    """
    dl = await _data_layer()
    await dl.execute_sql(query=query, parameters=parameters)
    return step_id


async def update_step(step_id: str, output: str) -> None:
    dl = await _data_layer()
    await dl.execute_sql(
        'UPDATE steps SET "output" = :output WHERE "id" = :id',
        {"id": step_id, "output": output},
    )


async def delete_step(step_id: str) -> None:
    dl = await _data_layer()
    await dl.execute_sql('DELETE FROM steps WHERE "id" = :id', {"id": step_id})
