"""Transcript storage for the chat UI — reads/writes the ``threads`` / ``steps``
/ ``elements`` / ``users`` tables directly.

These tables were created by Chainlit and are still named after it, but they are
now plain application tables: Chainlit has been removed, so the schema is ours
and ``alembic/env.py`` keeps excluding them from autogenerate. A later migration
can rename them for clarity; the columns and semantics would not change.

Writes go through :func:`execute_sql`, a thin wrapper over an app-owned async
engine — there is no Chainlit data layer any more.
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

_async_engine = None


def _get_async_engine():
    """The app's async engine, created once."""
    global _async_engine
    if _async_engine is None:
        from sqlalchemy.ext.asyncio import create_async_engine

        from config.settings import Config

        url = Config.DATABASE_URL
        if url.startswith("postgresql+psycopg://"):
            url = url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        _async_engine = create_async_engine(
            url, pool_pre_ping=True, pool_size=10, max_overflow=20,
        )
    return _async_engine


async def execute_sql(query: str, parameters: dict | None = None) -> list[dict]:
    """Run a statement and return the rows as dicts.

    ``parameters`` uses ``:name`` bind syntax. Callers pass JSON columns as
    ``json.dumps(...)`` text, which asyncpg sends verbatim for json/jsonb.
    """
    from sqlalchemy import text

    engine = _get_async_engine()
    async with engine.connect() as conn:
        result = await conn.execute(text(query), parameters or {})
        rows = [dict(row) for row in result.mappings().all()] if result.returns_rows else []
        await conn.commit()
    return rows


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _SqlExecutor:
    """The thread/step writes that used to go through Chainlit's data layer.

    Ported from ``FixedSQLAlchemyDataLayer`` so stored rows keep the exact
    shape the existing data has (timestamp format, metadata merge, userId
    preservation) — only the Chainlit dependency is gone.
    """

    async def execute_sql(self, query: str, parameters: dict | None = None) -> list[dict]:
        return await execute_sql(query, parameters)

    async def _user_identifier(self, user_id: str) -> Optional[str]:
        rows = await execute_sql(
            'SELECT "identifier" FROM users WHERE "id" = :id', {"id": user_id},
        )
        return rows[0]["identifier"] if rows else None

    async def update_thread(
        self,
        thread_id: str,
        *,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        tags: Optional[list] = None,
    ) -> None:
        """Upsert a thread row.

        ``metadata`` is merged shallowly onto the stored value (a ``None``
        value deletes that key), so callers can set one key — ``scratch``,
        ``agent`` — without clobbering the others. A missing ``user_id`` never
        clears an existing ``userId``, and ``createdAt`` is written only on
        insert so creation time survives later updates.
        """
        existing = await execute_sql(
            'SELECT "id","userId","metadata" FROM threads WHERE "id" = :id',
            {"id": thread_id},
        )
        is_new_thread = not existing

        has_updates = (
            metadata is not None or name is not None or user_id is not None or tags is not None
        )
        if not is_new_thread and not has_updates:
            return

        if metadata is not None:
            base: dict = {}
            if not is_new_thread:
                raw = existing[0].get("metadata") or {}
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

        if not user_id and not is_new_thread:
            user_id = existing[0].get("userId") or None

        user_identifier = await self._user_identifier(user_id) if user_id else None

        data = {
            "id": thread_id,
            "createdAt": _now() if is_new_thread else None,
            "name": name_value,
            "userId": user_id,
            "userIdentifier": user_identifier,
            "tags": ",".join(tags) if isinstance(tags, list) else tags,
            "metadata": json.dumps(metadata) if metadata else None,
        }
        parameters = {k: v for k, v in data.items() if v is not None}
        columns = ", ".join(f'"{k}"' for k in parameters)
        values = ", ".join(f":{k}" for k in parameters)
        updates = ", ".join(
            f'"{k}" = EXCLUDED."{k}"' for k in parameters if k not in ("id", "createdAt")
        )
        conflict = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
        await execute_sql(
            f'INSERT INTO threads ({columns}) VALUES ({values}) '
            f'ON CONFLICT ("id") {conflict};',
            parameters,
        )


async def _data_layer() -> Any:
    """The transcript SQL executor (historical name — no Chainlit involved)."""
    return _SqlExecutor()


async def ensure_user(user_email: str, user_name: str | None = None) -> str:
    """Return the user row id for ``user_email``, creating it if needed.

    ``threads.userId``/``userIdentifier`` resolve through this, so the id must
    stay stable per email.
    """
    rows = await execute_sql(
        'SELECT "id" FROM users WHERE "identifier" = :email',
        {"email": user_email},
    )
    if rows:
        return str(rows[0]["id"])

    user_id = str(uuid.uuid4())
    await execute_sql(
        'INSERT INTO users ("id","identifier","metadata","createdAt") '
        'VALUES (:id, :email, CAST(:metadata AS jsonb), :created_at) '
        'ON CONFLICT ("identifier") DO NOTHING',
        {
            "id": user_id,
            "email": user_email,
            "metadata": json.dumps({"name": user_name or user_email}),
            "created_at": _now(),
        },
    )
    # A concurrent insert may have won; re-read to return the stored id.
    rows = await execute_sql(
        'SELECT "id" FROM users WHERE "identifier" = :email',
        {"email": user_email},
    )
    return str(rows[0]["id"]) if rows else user_id


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
    """Threads owned by the user, most recently active first.

    Activity = the latest step's createdAt, falling back to the thread's
    createdAt (empty threads sort by when they were created).
    """
    dl = await _data_layer()
    rows = await dl.execute_sql(
        'SELECT t."id", t."name", t."createdAt", t."metadata", '
        'COALESCE((SELECT MAX(s."createdAt") FROM steps s '
        'WHERE s."threadId" = t."id"), t."createdAt") AS "activity" '
        'FROM threads t '
        'WHERE t."userIdentifier" = :email '
        'ORDER BY "activity" DESC LIMIT :limit',
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
                "last_activity": row.get("activity"),
                "agent": (metadata or {}).get("agent", "eagle"),
            }
        )
    return threads


async def get_thread_scratch(thread_id: str) -> dict:
    """The thread's persisted scratch dict (P3 — replaces Chainlit's
    user_session survival across runs, scoped per thread)."""
    dl = await _data_layer()
    rows = await dl.execute_sql(
        'SELECT "metadata" FROM threads WHERE "id" = :tid',
        {"tid": thread_id},
    )
    raw = None
    if rows and rows[0]:
        raw = rows[0].get("metadata")
    metadata: dict = {}
    if isinstance(raw, str):
        try:
            metadata = json.loads(raw)
        except json.JSONDecodeError:
            metadata = {}
    elif isinstance(raw, dict):
        metadata = raw
    scratch = metadata.get("scratch") or {}
    return scratch if isinstance(scratch, dict) else {}


async def save_thread_scratch(thread_id: str, scratch: dict) -> None:
    """Persist the scratch dict onto the thread's metadata.

    The data layer merges metadata shallowly, so the ``scratch`` key is
    replaced wholesale and other keys (e.g. ``agent``) are preserved.
    """
    dl = await _data_layer()
    await dl.update_thread(thread_id=thread_id, metadata={"scratch": scratch})


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


async def filter_owned_threads(thread_ids: list[str], user_email: str) -> list[str]:
    """The subset of ``thread_ids`` owned by the user, in one query."""
    if not thread_ids:
        return []
    dl = await _data_layer()
    placeholders = ", ".join(f":id{i}" for i in range(len(thread_ids)))
    params: dict[str, Any] = {f"id{i}": tid for i, tid in enumerate(thread_ids)}
    params["email"] = user_email
    rows = await dl.execute_sql(
        f'SELECT "id" FROM threads WHERE "id" IN ({placeholders}) '
        'AND "userIdentifier" = :email',
        params,
    )
    owned = {row.get("id") for row in rows or []}
    return [tid for tid in thread_ids if tid in owned]


async def rename_thread(thread_id: str, name: str) -> None:
    dl = await _data_layer()
    await dl.update_thread(thread_id=thread_id, name=name)


async def update_thread_agent(thread_id: str, agent_key: str) -> None:
    """Record the agent that handled the latest turn (list shows it)."""
    dl = await _data_layer()
    await dl.update_thread(thread_id=thread_id, metadata={"agent": agent_key})


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
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        steps.append(
            {
                "id": row.get("id"),
                "type": row.get("type"),
                "name": row.get("name"),
                "output": row.get("output") or "",
                "input": row.get("input") or "",
                "created_at": row.get("createdAt"),
                "metadata": metadata,
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
    step_id: str | None = None,
) -> str:
    """Persist a step; returns the new step id.

    Mirrors Chainlit's own ``create_step`` upsert via ``execute_sql`` — the
    public ``dl.create_step`` is wrapped in ``queue_until_user_message`` and
    requires a live Chainlit websocket session, which the beta UI doesn't have.

    ``step_id`` lets a caller that must know the id in advance supply it (widgets
    do: the card's own submit URL is built from the id before the row exists).
    """
    step_id = step_id or str(uuid.uuid4())
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


async def get_step(step_id: str) -> dict | None:
    """One step with parsed metadata, or ``None`` when it does not exist.

    Returns the owning ``thread_id`` too (``STEP_COLUMNS`` omits it): the widget
    layer stores its state in step metadata, so it must be able to check that a
    step id handed back by the client belongs to a thread the caller owns.
    """
    dl = await _data_layer()
    rows = await dl.execute_sql(
        'SELECT "id","threadId","type","name","output","metadata" FROM steps '
        'WHERE "id" = :id',
        {"id": step_id},
    )
    if not rows:
        return None
    row = rows[0]
    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    return {
        "id": row.get("id"),
        "thread_id": row.get("threadId"),
        "type": row.get("type"),
        "name": row.get("name"),
        "output": row.get("output") or "",
        "metadata": metadata,
    }


async def update_step_metadata(step_id: str, metadata: dict) -> None:
    """Replace a step's metadata wholesale.

    Widget state lives in step metadata rather than a table of its own: the step
    is the thing that renders, so state and markup cannot drift apart, and a
    reload re-renders the same widget in the same position for free.
    """
    dl = await _data_layer()
    await dl.execute_sql(
        'UPDATE steps SET "metadata" = :metadata WHERE "id" = :id',
        {"id": step_id, "metadata": json.dumps(metadata)},
    )


async def delete_step(step_id: str) -> None:
    dl = await _data_layer()
    await dl.execute_sql('DELETE FROM steps WHERE "id" = :id', {"id": step_id})


# ── Elements (file attachments) ────────────────────────────────────────────
# Mirrors Chainlit's own persistence: an upload creates an elements row with
# forId NULL; sending attaches the row to the persisted user step. Both UIs
# read the same rows, so legacy attachments render identically.

ELEMENT_COLUMNS = (
    '"id","type","name","url","display","objectKey","mime","size","forId"'
)


def _element_dict(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "type": row.get("type"),
        "name": row.get("name"),
        "url": row.get("url"),
        "display": row.get("display"),
        "object_key": row.get("objectKey"),
        "mime": row.get("mime"),
        "size": row.get("size"),
        "for_id": row.get("forId"),
    }


async def create_element(
    thread_id: str,
    *,
    element_id: str,
    name: str,
    type_: str,
    mime: str,
    url: str,
    object_key: str,
    size: str = "medium",
) -> None:
    """Persist an uploaded file's element row (forId NULL = pending attach)."""
    dl = await _data_layer()
    await dl.execute_sql(
        'INSERT INTO elements '
        '("id","threadId","type","name","url","display","objectKey",'
        '"chainlitKey","mime","size","forId") '
        "VALUES (:id, :tid, :type, :name, :url, 'inline', :object_key, "
        ":id, :mime, :size, NULL)",
        {
            "id": element_id,
            "tid": thread_id,
            "type": type_,
            "name": name,
            "url": url,
            "object_key": object_key,
            "mime": mime,
            "size": size,
        },
    )


async def attach_element(element_id: str, step_id: str, thread_id: str) -> bool:
    """Link a pending element to a persisted step. Returns success."""
    dl = await _data_layer()
    await dl.execute_sql(
        'UPDATE elements SET "forId" = :step '
        'WHERE "id" = :eid AND "threadId" = :tid AND "forId" IS NULL',
        {"eid": element_id, "step": step_id, "tid": thread_id},
    )
    rows = await dl.execute_sql(
        'SELECT "id" FROM elements WHERE "id" = :eid AND "forId" = :step',
        {"eid": element_id, "step": step_id},
    )
    return bool(rows)


async def list_elements(thread_id: str) -> list[dict]:
    """All elements in a thread, attached or pending."""
    dl = await _data_layer()
    rows = await dl.execute_sql(
        f'SELECT {ELEMENT_COLUMNS} FROM elements WHERE "threadId" = :tid',
        {"tid": thread_id},
    )
    return [_element_dict(row) for row in rows or []]


async def get_element(element_id: str, thread_id: str) -> Optional[dict]:
    dl = await _data_layer()
    rows = await dl.execute_sql(
        f'SELECT {ELEMENT_COLUMNS} FROM elements '
        'WHERE "id" = :eid AND "threadId" = :tid',
        {"eid": element_id, "tid": thread_id},
    )
    return _element_dict(rows[0]) if rows else None


async def delete_element(element_id: str, thread_id: str) -> bool:
    """Delete a PENDING element only (never one attached to a step)."""
    dl = await _data_layer()
    rows = await dl.execute_sql(
        'DELETE FROM elements WHERE "id" = :eid AND "threadId" = :tid '
        'AND "forId" IS NULL RETURNING "id"',
        {"eid": element_id, "tid": thread_id},
    )
    return bool(rows)
