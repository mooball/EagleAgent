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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from includes.chat import transcript
from includes.chat.context import chat_context
from includes.chat.context_sse import SseChatContext
from includes.chat.runner import RunInProgress, run_turn
from includes.agents.registry import AGENTS, resolve

from ._helpers import require_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat-ui", tags=["chat-ui"])

#: Active runs: thread_id -> {"queue": Queue, "task": Task}
_active_runs: dict[str, dict[str, Any]] = {}


def _cancel_key(thread_id: str) -> str:
    return f"chat-ui:{thread_id}"


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


# ── Run orchestration ──────────────────────────────────────────────────────


async def _run_task(
    thread_id: str,
    text: str,
    user_email: str,
    agent_key: str,
    queue: asyncio.Queue,
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

        # Eagle Agent defaults to supplier lookup, matching app.py.
        intent_context = ""
        if agent_key == "eagle":
            from includes.prompts import get_intent_context

            intent_context = get_intent_context("find_supplier") or ""

        dashboard_context = format_context_for_prompt(user_email)

        with chat_context(ctx):
            await run_turn(
                text,
                ctx,
                graph=graph,
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
    return templates.TemplateResponse(
        request,
        "chat_ui/thread.html",
        {
            "user": user,
            "thread": thread,
            "steps": steps,
            "agents": AGENTS,
        },
    )


# ── Thread CRUD ────────────────────────────────────────────────────────────


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
    steps = await transcript.get_steps(thread_id)
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
        }
    )


# ── Embedded panel (same-document, shell-less) ────────────────────────────


@router.get("/embed-threads")
async def embed_threads(user: dict = Depends(require_user)):
    """Thread list for the embedded panel (JSON)."""
    await _guard(user)
    threads = await transcript.list_threads(user["email"])
    return JSONResponse(
        {
            "threads": [
                {"id": t["id"], "name": t["name"], "agent": t["agent"]}
                for t in threads
            ]
        }
    )


def _embed_data(user: dict, threads: list[dict], thread: dict | None, steps: list[dict]) -> dict:
    """JSON payload embedded into the panel template (Jinja can't build it)."""
    return {
        "user_email": user["email"],
        "agents": [{"key": k, "label": s.label} for k, s in AGENTS.items()],
        "threads": [{"id": t["id"], "name": t["name"], "agent": t["agent"]} for t in threads],
        "thread": (
            {
                "id": thread["id"],
                "name": thread["name"],
                "agent": (thread.get("metadata") or {}).get("agent", "eagle"),
            }
            if thread
            else None
        ),
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
    steps = await transcript.get_steps(thread_id)
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


# ── The run ────────────────────────────────────────────────────────────────


@router.post("/threads/{thread_id}/messages")
async def post_message(
    request: Request, thread_id: str, user: dict = Depends(require_user)
):
    await _guard(user)
    await _owned_thread(thread_id, user)

    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "Message text is required"}, status_code=400)
    agent_key = resolve(str(body.get("agent") or "")).key

    run = _active_runs.get(thread_id)
    if run and not run["task"].done():
        return JSONResponse(
            {"error": "Still working on the previous message — one moment."},
            status_code=409,
        )

    # Persist the user turn into the shared steps table (Chainlit parity).
    await transcript.create_step(
        thread_id, type_="user_message", name=user["email"], output=text
    )

    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        _run_task(thread_id, text, user["email"], agent_key, queue)
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
