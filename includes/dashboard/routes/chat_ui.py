"""Beta chat UI — /chat-ui (SSE transport over ``run_turn``).

Plan: [.github/prompts/plan-chatMigration-beta.prompt.md].

Additive to the existing app: Chainlit stays at /chat; this router serves a
minimal chat POC at /chat-ui for users on the ``CHAT_UI_BETA_USERS`` allowlist.
Its job is to prove the SSE framework through the Railway proxy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy import text

from includes.chat import transcript
from includes.chat.context import chat_context
from includes.chat.context_sse import SseChatContext
from includes.chat.runner import RunInProgress, run_turn
from includes.agents.registry import AGENTS, default_agent, resolve

from ._helpers import require_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat-ui", tags=["chat-ui"])

#: Active runs: thread_id -> {"queue": Queue, "task": Task}
_active_runs: dict[str, dict[str, Any]] = {}


def _cancel_key(thread_id: str) -> str:
    return f"chat-ui:{thread_id}"


# ── Current-thread anchor (one per user, non-RFQ home base) ────────────────


def _get_current_thread_id(user_email: str) -> str | None:
    from . import _helpers

    session = _helpers.get_session()
    try:
        return session.execute(
            text(
                "SELECT thread_id FROM chat_ui_current_threads "
                "WHERE user_email = :email"
            ),
            {"email": user_email},
        ).scalar()
    finally:
        session.close()


def _set_current_thread_id(user_email: str, thread_id: str) -> None:
    from . import _helpers

    session = _helpers.get_session()
    try:
        session.execute(
            text(
                "INSERT INTO chat_ui_current_threads "
                "(user_email, thread_id, updated_at) VALUES (:email, :tid, NOW()) "
                "ON CONFLICT (user_email) DO UPDATE "
                "SET thread_id = EXCLUDED.thread_id, updated_at = NOW()"
            ),
            {"email": user_email, "tid": thread_id},
        )
        session.commit()
    finally:
        session.close()


async def _current_thread_id_or_create(user: dict) -> str:
    """The user's current thread, created on first use (stale ids repaired)."""
    email = user["email"]
    thread_id = _get_current_thread_id(email)
    if thread_id:
        existing = await transcript.get_thread(thread_id, email)
        if existing is not None:
            return thread_id
    thread_id = await transcript.create_thread(
        email,
        user_name=user.get("name"),
        name="Current chat",
        agent_key="eagle",
    )
    _set_current_thread_id(email, thread_id)
    return thread_id


def _intent_route(intent_name: str) -> tuple[str, str]:
    """Map a composer command (intent name) onto (agent_key, intent_context).

    The agent registry is the single routing table: whichever agent declares
    the intent owns the turn. Unknown intents fall back to the default agent
    with no extra context.
    """
    for key, spec in AGENTS.items():
        intents = spec.command_intents()
        if intent_name in intents:
            return key, intents[intent_name]["context"]
    return default_agent().key, ""


async def _guard(user: dict) -> None:
    """404 for anyone not on the beta allowlist — the UI is invisible to them."""
    from config import config

    if user.get("email", "").lower() not in config.get_beta_chat_users():
        raise HTTPException(status_code=404)


async def _owned_thread(thread_id: str, user: dict) -> dict:
    thread = await transcript.get_thread(thread_id, user["email"])
    if thread is None:
        raise HTTPException(status_code=404)
    return thread


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ── File attachments ───────────────────────────────────────────────────────


def _size_label(num_bytes: int) -> str:
    """Chainlit's small/medium/large size labels."""
    if num_bytes < 1024 * 1024:
        return "small"
    if num_bytes < 10 * 1024 * 1024:
        return "medium"
    return "large"


def _element_type(mime: str) -> str:
    if mime.startswith("image/"):
        return "image"
    if mime == "application/pdf":
        return "pdf"
    return "file"


def _element_path(element: dict) -> str:
    """Absolute disk path for an element (same dir main.py serves /files from)."""
    from config import config

    return os.path.join(config.DATA_DIR, "attachments", element["object_key"])


def _read_file_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _file_payload(element: dict) -> dict:
    """Client-facing file fields (never leak object_key)."""
    return {
        "id": element["id"],
        "type": element["type"],
        "name": element["name"],
        "url": element["url"],
        "mime": element["mime"],
        "size": element["size"],
    }


async def _steps_with_files(thread_id: str) -> list[dict]:
    """Transcript with each step's attachments merged in (legacy + new)."""
    steps = await transcript.get_steps(thread_id)
    elements = await transcript.list_elements(thread_id)
    files_by_step: dict[str, list[dict]] = {}
    for el in elements:
        if el["for_id"]:
            files_by_step.setdefault(el["for_id"], []).append(_file_payload(el))
    for step in steps:
        step["files"] = files_by_step.get(step["id"], [])
    return steps


# ── Run orchestration ──────────────────────────────────────────────────────


async def _run_task(
    thread_id: str,
    text: str,
    user_email: str,
    agent_key: str,
    queue: asyncio.Queue,
    files: list[dict] | None = None,
    file_metadata: list[dict] | None = None,
    intent_context: str = "",
) -> None:
    """One agent turn, streaming events into ``queue``. Mirrors app.py's
    main() adapter minus the Chainlit session."""
    from includes.graph import setup_globals
    from includes.dashboard.context import format_context_for_prompt

    try:
        await setup_globals()
        graph = resolve(agent_key).graph()
        ctx = SseChatContext(
            thread_id=thread_id,
            user_email=user_email,
            agent=agent_key,
            queue=queue,
            cancel_key=_cancel_key(thread_id),
        )

        # Eagle Agent defaults to supplier lookup, matching app.py — unless a
        # command already supplied an intent context.
        if not intent_context and agent_key == "eagle":
            from includes.prompts import get_intent_context

            intent_context = get_intent_context("find_supplier") or ""

        dashboard_context = format_context_for_prompt(
            user_email, thread_id=thread_id
        )

        with chat_context(ctx):
            await run_turn(
                text,
                ctx,
                graph=graph,
                files=files,
                file_metadata=file_metadata,
                intent_context=intent_context,
                dashboard_context=dashboard_context,
                on_busy="reject",
            )
    except RunInProgress:
        await queue.put(
            {
                "event": "error",
                "data": {"message": "Still working on the previous message — one moment."},
            }
        )
    except Exception:
        logger.exception("[chat-ui] run failed for thread %s", thread_id[:8])
        await queue.put(
            {
                "event": "error",
                "data": {"message": "Sorry, an unexpected error occurred. Please try again."},
            }
        )
    finally:
        await queue.put({"event": "done", "data": {}})
        _active_runs.pop(thread_id, None)


# ── Pages ──────────────────────────────────────────────────────────────────


@router.get("")
async def index(request: Request, user: dict = Depends(require_user)):
    await _guard(user)
    from ._helpers import templates

    threads = await transcript.list_threads(user["email"])
    return templates.TemplateResponse(
        request, "chat_ui/index.html", {"user": user, "threads": threads, "agents": AGENTS}
    )


@router.get("/threads/{thread_id}")
async def thread_page(
    request: Request, thread_id: str, user: dict = Depends(require_user)
):
    await _guard(user)
    from ._helpers import templates

    thread = await _owned_thread(thread_id, user)
    steps = await transcript.get_steps(thread_id)
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = {}
    return templates.TemplateResponse(
        request,
        "chat_ui/thread.html",
        {
            "user": user,
            "thread": thread,
            "steps": steps,
            "agents": AGENTS,
            "agent_options": _agent_options(),
            "commands": _command_data(),
            "thread_agent": metadata.get("agent", "eagle"),
        },
    )


# ── Thread CRUD ────────────────────────────────────────────────────────────


@router.get("/current-thread")
async def current_thread(user: dict = Depends(require_user)):
    """The user's current (non-RFQ) thread — created on first use."""
    await _guard(user)
    thread_id = await _current_thread_id_or_create(user)
    return JSONResponse({"thread_id": thread_id})


@router.post("/current-thread")
async def set_current_thread(
    request: Request, user: dict = Depends(require_user)
):
    """Point the user's current thread at an existing (owned) thread."""
    await _guard(user)
    body = await request.json()
    thread_id = str(body.get("thread_id") or "")
    if not thread_id:
        return JSONResponse({"error": "thread_id required"}, status_code=400)
    existing = await transcript.get_thread(thread_id, user["email"])
    if existing is None:
        return JSONResponse({"error": "Thread not found"}, status_code=404)
    _set_current_thread_id(user["email"], thread_id)
    return JSONResponse({"ok": True, "thread_id": thread_id})


@router.post("/threads")
async def create_thread(
    request: Request, user: dict = Depends(require_user)
):
    await _guard(user)
    form = await request.form()
    agent_key = resolve(str(form.get("agent") or "")).key
    thread_id = await transcript.create_thread(
        user["email"],
        user_name=user.get("name"),
        name="New chat",
        agent_key=agent_key,
    )
    if str(form.get("embed") or "") == "1":
        # Embedded panel flow — the embed JS navigates itself.
        return JSONResponse({"thread_id": thread_id, "agent": agent_key})
    return RedirectResponse(f"/chat-ui/threads/{thread_id}", status_code=303)


@router.patch("/threads/{thread_id}")
async def rename_thread(
    request: Request, thread_id: str, user: dict = Depends(require_user)
):
    await _guard(user)
    await _owned_thread(thread_id, user)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "Name is required"}, status_code=400)
    await transcript.rename_thread(thread_id, name)
    return JSONResponse({"ok": True})


@router.delete("/threads/{thread_id}")
async def delete_thread(
    thread_id: str, user: dict = Depends(require_user)
):
    await _guard(user)
    await _owned_thread(thread_id, user)
    await transcript.delete_thread(thread_id)
    return JSONResponse({"ok": True})


@router.get("/threads/{thread_id}/messages")
async def thread_messages(
    thread_id: str, user: dict = Depends(require_user)
):
    await _guard(user)
    await _owned_thread(thread_id, user)
    steps = await _steps_with_files(thread_id)
    return JSONResponse({"steps": steps})


@router.get("/threads/{thread_id}/meta")
async def thread_meta(
    thread_id: str, user: dict = Depends(require_user)
):
    await _guard(user)
    thread = await _owned_thread(thread_id, user)
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = {}
    return JSONResponse(
        {
            "id": thread["id"],
            "name": thread["name"],
            "agent": metadata.get("agent", "eagle"),
            "rfq_id": _rfq_binding_map(user["email"]).get(thread_id),
        }
    )


# ── Embedded panel (same-document, shell-less) ────────────────────────────


@router.get("/embed-threads")
async def embed_threads(user: dict = Depends(require_user)):
    """Thread list for the embedded panel (JSON)."""
    await _guard(user)
    threads = await transcript.list_threads(user["email"])
    bindings = _rfq_binding_map(user["email"])
    rfq_meta = _rfq_meta_by_number(list(bindings.values()))
    return JSONResponse(
        {
            "threads": [_thread_payload(t, bindings, rfq_meta) for t in threads]
        }
    )


def _agent_options() -> list[dict]:
    """Agent list for client-side <select> building."""
    return [{"key": k, "label": s.label} for k, s in AGENTS.items()]


def _rfq_binding_map(user_email: str) -> dict[str, str]:
    """Map thread_id -> rfq_number for the user's RFQ bindings.

    Lets the embed mark bound threads and know when a click should navigate
    the whole dashboard to the owning RFQ instead of opening the thread.
    """
    from includes.dashboard.models import RFQThread
    from . import _helpers

    session = _helpers.get_session()
    try:
        rows = (
            session.query(RFQThread)
            .filter(RFQThread.user_email == user_email)
            .all()
        )
        return {row.thread_id: row.rfq_number for row in rows}
    finally:
        session.close()


def _rfq_meta_by_number(rfq_numbers: list[str]) -> dict[str, dict]:
    """rfq_number -> {title, op_number} for the bound-thread list pill."""
    if not rfq_numbers:
        return {}
    from includes.dashboard.models import RFQ, Opportunity
    from . import _helpers

    session = _helpers.get_session()
    try:
        rfqs = session.query(RFQ).filter(RFQ.rfq_number.in_(rfq_numbers)).all()
        opp_ids = [r.opportunity_id for r in rfqs if r.opportunity_id]
        opps: dict = {}
        if opp_ids:
            opps = {
                o.id: o
                for o in session.query(Opportunity).filter(Opportunity.id.in_(opp_ids)).all()
            }
        meta: dict[str, dict] = {}
        for r in rfqs:
            op_number = None
            if r.opportunity_id and r.opportunity_id in opps:
                op_number = opps[r.opportunity_id].opportunity_number
            meta[r.rfq_number] = {
                "title": r.title,
                "op_number": op_number,
                "customer": r.customer,
            }
        return meta
    finally:
        session.close()


def _thread_payload(t: dict, bindings: dict, rfq_meta: dict) -> dict:
    """A thread dict for the client, annotated with RFQ details when bound."""
    payload = {
        "id": t["id"],
        "name": t["name"],
        "agent": t["agent"],
        "rfq_id": bindings.get(t["id"]),
        "last_activity": t.get("last_activity"),
    }
    rfq_id = payload["rfq_id"]
    if rfq_id:
        meta = rfq_meta.get(rfq_id) or {}
        payload["rfq_title"] = meta.get("title")
        payload["op_number"] = meta.get("op_number")
        payload["rfq_customer"] = meta.get("customer")
    return payload


def _command_data() -> list[dict]:
    """Composer command menu: every agent's composer intents plus a prefill.

    Clicking a command in the Tools dropdown switches the agent and prefills
    the composer — the run itself still flows through normal text sending.
    """
    commands: list[dict] = []
    for key, spec in AGENTS.items():
        for name, intent in spec.command_intents().items():
            commands.append(
                {
                    "name": name,
                    "label": intent["label"],
                    "description": intent["description"],
                    "icon": intent.get("icon", "⚡"),
                    "agent": key,
                    "prefill": f"{intent['label']}: ",
                }
            )
    return commands


def _embed_data(user: dict, threads: list[dict], thread: dict | None, steps: list[dict]) -> dict:
    """JSON payload embedded into the panel template (Jinja can't build it)."""
    bindings = _rfq_binding_map(user["email"])
    rfq_meta = _rfq_meta_by_number(list(bindings.values()))
    thread_data = (
        {
            "id": thread["id"],
            "name": thread["name"],
            "agent": (thread.get("metadata") or {}).get("agent", "eagle"),
            "rfq_id": bindings.get(thread["id"]),
        }
        if thread
        else None
    )
    return {
        "user_email": user["email"],
        "agents": _agent_options(),
        "commands": _command_data(),
        "current_thread_id": _get_current_thread_id(user["email"]),
        "threads": [_thread_payload(t, bindings, rfq_meta) for t in threads],
        "thread": thread_data,
        "steps": steps,
    }


@router.get("/embed")
async def embed(
    request: Request, user: dict = Depends(require_user)
):
    """Shell-less chat panel for the dashboard right-hand side."""
    await _guard(user)
    from ._helpers import templates

    threads = await transcript.list_threads(user["email"])
    return templates.TemplateResponse(
        request,
        "chat_ui/embed.html",
        {
            "user": user,
            "threads": threads,
            "thread": None,
            "steps": [],
            "agents": AGENTS,
            "embed_data": _embed_data(user, threads, None, []),
        },
    )


@router.get("/embed/threads/{thread_id}")
async def embed_thread(
    request: Request, thread_id: str, user: dict = Depends(require_user)
):
    await _guard(user)
    from ._helpers import templates

    thread = await _owned_thread(thread_id, user)
    steps = await _steps_with_files(thread_id)
    threads = await transcript.list_threads(user["email"])
    return templates.TemplateResponse(
        request,
        "chat_ui/embed.html",
        {
            "user": user,
            "threads": threads,
            "thread": thread,
            "steps": steps,
            "agents": AGENTS,
            "embed_data": _embed_data(user, threads, thread, steps),
        },
    )


# ── File upload (same persistence Chainlit uses) ───────────────────────────


@router.post("/upload")
async def upload_files(
    request: Request, user: dict = Depends(require_user)
):
    """Receive file(s), store them like Chainlit, persist pending elements."""
    await _guard(user)
    form = await request.form()
    thread_id = str(form.get("thread_id") or "")
    if not thread_id:
        return JSONResponse({"error": "thread_id required"}, status_code=400)
    await _owned_thread(thread_id, user)

    uploads = form.getlist("files")
    if not uploads:
        return JSONResponse({"error": "No files provided"}, status_code=400)

    from config import config
    import aiofiles

    user_id = await transcript.ensure_user(user["email"], user.get("name"))
    out: list[dict] = []
    for upload in uploads:
        data = upload.file.read()
        element_id = str(uuid.uuid4())
        safe_name = os.path.basename(str(upload.filename or "file"))[:200]
        object_key = f"{user_id}/{element_id}/{safe_name}"

        dest = os.path.join(config.DATA_DIR, "attachments", object_key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        async with aiofiles.open(dest, "wb") as fh:
            await fh.write(data)

        mime = upload.content_type or "application/octet-stream"
        etype = _element_type(mime)
        url = f"/files/{object_key}"
        await transcript.create_element(
            thread_id,
            element_id=element_id,
            name=safe_name,
            type_=etype,
            mime=mime,
            url=url,
            object_key=object_key,
            size=_size_label(len(data)),
        )
        out.append(
            {
                "id": element_id,
                "name": safe_name,
                "type": etype,
                "mime": mime,
                "url": url,
                "size": len(data),
            }
        )
    return JSONResponse({"files": out})


@router.delete("/files/{element_id}")
async def remove_upload(
    element_id: str,
    thread_id: str,
    user: dict = Depends(require_user),
):
    """Remove a pending upload (attached elements are never deletable here)."""
    await _guard(user)
    if not thread_id:
        return JSONResponse({"error": "thread_id required"}, status_code=400)
    await _owned_thread(thread_id, user)

    element = await transcript.get_element(element_id, thread_id)
    if element is None:
        return JSONResponse({"ok": True})
    if element["for_id"]:
        return JSONResponse({"error": "Attached files cannot be removed"}, status_code=400)

    if await transcript.delete_element(element_id, thread_id):
        try:
            await asyncio.to_thread(os.remove, _element_path(element))
        except OSError:
            pass
    return JSONResponse({"ok": True})


# ── The run ────────────────────────────────────────────────────────────────


@router.post("/threads/{thread_id}/messages")
async def post_message(
    request: Request, thread_id: str, user: dict = Depends(require_user)
):
    await _guard(user)
    await _owned_thread(thread_id, user)

    body = await request.json()
    text = (body.get("text") or "").strip()
    agent_key = resolve(str(body.get("agent") or "")).key

    # Command routing: the Tools dropdown sends an intent name; the registry
    # decides the owning agent and its prompt context. Plain messages keep
    # the thread's default agent.
    intent_name = str(body.get("intent") or "")
    intent_context = ""
    if intent_name:
        agent_key, intent_context = _intent_route(intent_name)

    file_ids = [str(fid) for fid in (body.get("file_ids") or []) if str(fid)]

    # Process pending uploads before persisting the turn: the agent gets the
    # extracted content (multimodal), the step gets the elements attached.
    processed_files: list[dict] = []
    file_metadata: list[dict] = []
    attached_ids: list[str] = []
    if file_ids:
        from includes.chat.document_processing import process_file

        elements = await transcript.list_elements(thread_id)
        pending = {e["id"]: e for e in elements if not e["for_id"]}
        for fid in file_ids:
            element = pending.get(fid)
            if element is None:
                continue
            path = _element_path(element)
            try:
                data = await asyncio.to_thread(_read_file_bytes, path)
                processed = await asyncio.to_thread(
                    process_file,
                    data,
                    element["mime"] or "application/octet-stream",
                    element["name"],
                )
            except Exception:
                logger.exception("[chat-ui] could not process upload %s", fid[:8])
                continue
            processed_files.append(processed)
            file_metadata.append(
                {
                    "name": element["name"],
                    "mime_type": element["mime"],
                    "size": element["size"],
                    "processed_type": processed.get("processed_type"),
                }
            )
            attached_ids.append(fid)

    if not text and not processed_files:
        return JSONResponse({"error": "Message text is required"}, status_code=400)

    run = _active_runs.get(thread_id)
    if run and not run["task"].done():
        return JSONResponse(
            {"error": "Still working on the previous message — one moment."},
            status_code=409,
        )

    # Persist the user turn into the shared steps table (Chainlit parity).
    step_id = await transcript.create_step(
        thread_id, type_="user_message", name=user["email"], output=text
    )

    # Track the agent that will handle this turn — the thread list shows it.
    await transcript.update_thread_agent(thread_id, agent_key)

    # Attach pending uploads to the persisted step (matches Chainlit's forId).
    for fid in attached_ids:
        await transcript.attach_element(fid, step_id, thread_id)

    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        _run_task(
            thread_id,
            text,
            user["email"],
            agent_key,
            queue,
            files=processed_files or None,
            file_metadata=file_metadata or None,
            intent_context=intent_context,
        )
    )
    _active_runs[thread_id] = {"queue": queue, "task": task}
    return JSONResponse({"ok": True})


@router.get("/threads/{thread_id}/stream")
async def stream(thread_id: str, user: dict = Depends(require_user)):
    await _guard(user)
    await _owned_thread(thread_id, user)

    async def event_stream():
        # Keep-alive so proxies see an active stream immediately.
        yield ": connected\n\n"
        run = _active_runs.get(thread_id)
        if run is None:
            yield _sse("done", {})
            return
        queue: asyncio.Queue = run["queue"]
        while True:
            item = await queue.get()
            yield _sse(item["event"], item["data"])
            if item["event"] in ("done", "error"):
                return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Railway/nginx proxy buffers SSE without this.
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/threads/{thread_id}/stop")
async def stop(thread_id: str, user: dict = Depends(require_user)):
    await _guard(user)
    await _owned_thread(thread_id, user)
    from includes.agent_bridge import request_stop

    await request_stop(_cancel_key(thread_id))
    return JSONResponse({"ok": True})
