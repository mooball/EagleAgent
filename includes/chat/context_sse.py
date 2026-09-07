"""SSE implementation of :class:`~includes.chat.context.ChatContext`.

Mirrors ``context_chainlit.py`` behaviour over a per-thread ``asyncio.Queue``
drained by the ``/chat-ui`` SSE endpoint. Every message is persisted into the
same Chainlit `steps` table via :mod:`includes.chat.transcript` — the Track A
"swap the writer" adapter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from includes.chat import transcript
from includes.chat.context import ActionSpec

logger = logging.getLogger(__name__)


def _serialize_actions(actions: list[ActionSpec] | None) -> list[dict]:
    """ActionSpec → JSON-safe dicts for the SSE payload and step metadata."""
    if not actions:
        return []
    return [
        {
            "name": a.name,
            "label": a.label,
            "payload": a.payload or {},
            "tooltip": a.tooltip,
        }
        for a in actions
    ]


def _json_safe(value: Any) -> bool:
    """True if value survives json.dumps (thread metadata is JSON)."""
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


class SseMessageHandle:
    """A message streamed to the browser and persisted to `steps`.

    ``id`` is the persisted step id, so update/remove address the same row.
    Transient progress lines are not persisted and are client-only.
    """

    def __init__(
        self,
        message_id: str,
        *,
        content: str,
        author: str | None,
        queue: asyncio.Queue,
        persisted: bool = True,
    ) -> None:
        self.id = message_id
        self.content = content
        self.author = author
        self._queue = queue
        self._persisted = persisted

    async def _emit(self, event: str, data: dict) -> None:
        await self._queue.put({"event": event, "data": data})

    async def stream(self, token: str) -> None:
        self.content += token
        await self._emit("token", {"id": self.id, "text": token})

    async def update(self) -> None:
        # Progress-line updates ("⏳ Using X (xN)…") — full replace + persist.
        if self._persisted:
            try:
                await transcript.update_step(self.id, self.content)
            except Exception as exc:
                logger.warning("[chat-ui] step update failed: %s", exc)
        await self._emit("message_update", {"id": self.id, "content": self.content})

    async def remove(self) -> None:
        # Mirrors cl.Message.remove(): gone from UI and from history.
        if self._persisted:
            try:
                await transcript.delete_step(self.id)
            except Exception as exc:
                logger.warning("[chat-ui] step delete failed: %s", exc)
        await self._emit("message_remove", {"id": self.id})

    async def save(self) -> None:
        # Persisted at say() time (mirrors cl.Message.send()); the tail of the
        # stream lands in steps via update_step on the final update.
        if self._persisted:
            try:
                await transcript.update_step(self.id, self.content)
            except Exception as exc:
                logger.warning("[chat-ui] step save failed: %s", exc)
        await self._emit("message_end", {"id": self.id, "content": self.content})


class SseChatContext:
    """A ``ChatContext`` streaming to one browser connection over SSE."""

    def __init__(
        self,
        *,
        thread_id: str,
        user_email: str,
        agent: str,
        queue: asyncio.Queue,
        cancel_key: str,
    ) -> None:
        self.thread_id = thread_id
        self.user_email = user_email
        self.agent = agent
        self.active_message: SseMessageHandle | None = None
        self._queue = queue
        self._cancel_key = cancel_key
        self._scratch: dict[str, Any] = {}
        self._scratch_loaded = False
        self._scratch_dirty = False

    async def load_scratch(self) -> None:
        """Hydrate the scratch dict from the thread's metadata (P3).

        Called at the start of every run, so counters like
        ``pipeline_fixes_{rfq_id}`` survive across separate button clicks.
        Failures never break the run — the scratch just starts empty.
        """
        if self._scratch_loaded:
            return
        try:
            persisted = await transcript.get_thread_scratch(self.thread_id)
            # Merge persisted values UNDER the in-memory dict: keys seeded
            # before load (e.g. active_graph) must survive, and per-run state
            # wins over stale persisted copies.
            self._scratch = {**persisted, **self._scratch}
        except Exception as exc:
            logger.warning("[chat-ui] scratch load failed: %s", exc)
        self._scratch_loaded = True

    async def flush_scratch(self) -> None:
        """Persist the scratch dict at the end of the run (P3).

        Only JSON-safe values are written — in-memory objects like
        ``active_graph`` never reach thread metadata.
        """
        if not self._scratch_dirty:
            return
        try:
            safe = {k: v for k, v in self._scratch.items() if _json_safe(v)}
            await transcript.save_thread_scratch(self.thread_id, safe)
            self._scratch_dirty = False
        except Exception as exc:
            logger.warning("[chat-ui] scratch flush failed: %s", exc)

    async def say(
        self,
        text: str,
        *,
        actions: list[ActionSpec] | None = None,
        author: str | None = None,
        transient: bool = False,
    ) -> SseMessageHandle:
        """Send a message. Persisted immediately, like Chainlit's send().

        ``actions`` are emitted in the message_start event and persisted in the
        step metadata, so chat-emitted buttons survive a reload.
        ``transient`` messages (tool-progress lines) are never persisted:
        the runner removes them, and they are not part of the transcript.
        """
        step_name = author or "EagleAgent"
        action_data = _serialize_actions(actions)
        step_id: str | None = None
        if not transient:
            try:
                step_id = await transcript.create_step(
                    self.thread_id,
                    type_="assistant_message",
                    name=step_name,
                    output=text,
                    metadata={"actions": action_data} if action_data else None,
                )
            except Exception as exc:
                # Never break the run over persistence — the client still sees it.
                logger.warning("[chat-ui] step persist failed: %s", exc)
        # The step id doubles as the message id so update/remove hit that row.
        message_id = step_id or str(uuid.uuid4())
        await self._queue.put(
            {
                "event": "message_start",
                "data": {
                    "id": message_id,
                    "author": step_name,
                    "content": text,
                    "transient": transient,
                    "actions": action_data,
                },
            }
        )
        return SseMessageHandle(
            message_id,
            content=text,
            author=step_name,
            queue=self._queue,
            persisted=step_id is not None,
        )

    async def image(self, path: str, *, name: str) -> None:
        # POC: persist the 📸 marker like Chainlit; inline image rendering of
        # the file itself is a later checklist item (B10).
        await self.say(f"📸 {name}")

    async def notify_dashboard(self, command: str, payload: dict | None = None) -> None:
        # Same-document beta UI: the embed forwards this to the dashboard shell
        # via a DOM CustomEvent. (The Chainlit path uses cl.send_window_message.)
        await self._queue.put(
            {
                "event": "dashboard",
                "data": {"command": command, "payload": payload or {}},
            }
        )

    async def rename_thread(self, name: str) -> None:
        try:
            await transcript.rename_thread(self.thread_id, name)
        except Exception as exc:
            logger.warning("[chat-ui] thread rename failed: %s", exc)

    def get(self, key: str, default: Any = None) -> Any:
        return self._scratch.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._scratch[key] = value
        self._scratch_dirty = True

    @property
    def cancelled(self) -> bool:
        from includes.agent_bridge import is_stop_requested

        try:
            return is_stop_requested(self._cancel_key)
        except Exception:
            return False

    def reset_cancel(self) -> None:
        from includes.agent_bridge import clear_stop

        try:
            clear_stop(self._cancel_key)
        except Exception:
            pass
