"""Beta /chat-ui routes: allowlist gate, CRUD, run start, SSE."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware


def _make_test_app():
    app = FastAPI()
    app.add_middleware(
        SessionMiddleware,
        secret_key="test-secret",
        session_cookie="eagleagent_session",
    )
    from includes.dashboard.routes.chat_ui import router

    app.include_router(router)

    @app.get("/_test/login")
    async def _login(request: Request, email: str = "tom@eagle-exports.com",
                     name: str = "Tom"):
        request.session["user"] = {
            "email": email,
            "name": name,
            "given_name": name.split()[0],
            "family_name": name.split()[-1],
            "picture": "",
            "hd": email.split("@")[-1],
        }
        return Response(status_code=200)

    return app


@pytest.fixture
def app():
    return _make_test_app()


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture(autouse=True)
def allowlist(monkeypatch):
    monkeypatch.setattr(
        "config.settings.Config.CHAT_UI_BETA_USERS",
        "tom@eagle-exports.com",
    )


def _login(client, email="tom@eagle-exports.com"):
    client.get(f"/_test/login?email={email}")


def _patch_transcript(monkeypatch, **overrides):
    defaults = {
        "list_threads": AsyncMock(return_value=[]),
        "get_thread": AsyncMock(return_value={
            "id": "t1", "name": "New chat", "metadata": {"agent": "eagle"},
        }),
        "get_steps": AsyncMock(return_value=[]),
        "list_elements": AsyncMock(return_value=[]),
        "create_thread": AsyncMock(return_value="t1"),
        "rename_thread": AsyncMock(return_value=None),
        "delete_thread": AsyncMock(return_value=None),
        "create_step": AsyncMock(return_value="s1"),
    }
    defaults.update(overrides)
    for name, mock in defaults.items():
        monkeypatch.setattr(f"includes.chat.transcript.{name}", mock)
    return defaults


class TestAllowlist:
    def test_unauthenticated_redirects_to_login(self, client):
        resp = client.get("/chat-ui", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_non_beta_user_gets_404(self, client):
        _login(client, email="outsider@eagle-exports.com")
        with patch("includes.chat.transcript.list_threads", new=AsyncMock()):
            resp = client.get("/chat-ui")
        assert resp.status_code == 404

    def test_beta_user_sees_index(self, client, monkeypatch):
        mocks = _patch_transcript(monkeypatch)
        _login(client)
        resp = client.get("/chat-ui")
        assert resp.status_code == 200
        mocks["list_threads"].assert_awaited_once()

    def test_beta_user_stream_gated_too(self, client, monkeypatch):
        _patch_transcript(monkeypatch)
        _login(client, email="outsider@eagle-exports.com")
        resp = client.get("/chat-ui/threads/t1/stream")
        assert resp.status_code == 404


class TestThreadCRUD:
    def test_create_thread_redirects(self, client, monkeypatch):
        mocks = _patch_transcript(monkeypatch)
        _login(client)
        resp = client.post(
            "/chat-ui/threads", data={"agent": "research"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].endswith("/chat-ui/threads/t1")
        assert mocks["create_thread"].call_args.kwargs["agent_key"] == "research"

    def test_thread_page_renders_history(self, client, monkeypatch):
        mocks = _patch_transcript(
            monkeypatch,
            get_steps=AsyncMock(return_value=[
                {"id": "s1", "type": "user_message", "name": "tom@eagle-exports.com",
                 "output": "hello", "metadata": {}},
            ]),
        )
        _login(client)
        resp = client.get("/chat-ui/threads/t1")
        assert resp.status_code == 200
        assert "hello" in resp.text

    def test_thread_not_owned_is_404(self, client, monkeypatch):
        _patch_transcript(monkeypatch, get_thread=AsyncMock(return_value=None))
        _login(client)
        resp = client.get("/chat-ui/threads/t1")
        assert resp.status_code == 404

    def test_rename_and_delete(self, client, monkeypatch):
        mocks = _patch_transcript(monkeypatch)
        _login(client)
        assert client.patch("/chat-ui/threads/t1", json={"name": "Renamed"}).status_code == 200
        mocks["rename_thread"].assert_awaited_once_with("t1", "Renamed")
        assert client.delete("/chat-ui/threads/t1").status_code == 200
        mocks["delete_thread"].assert_awaited_once_with("t1")


class TestEmbed:
    def test_embed_requires_beta(self, client, monkeypatch):
        _patch_transcript(monkeypatch)
        _login(client, email="outsider@eagle-exports.com")
        assert client.get("/chat-ui/embed").status_code == 404

    def test_embed_renders(self, client, monkeypatch):
        _patch_transcript(monkeypatch)
        _login(client)
        resp = client.get("/chat-ui/embed")
        assert resp.status_code == 200
        assert "chat-ui-embed-root" in resp.text

    def test_embed_thread_renders_history(self, client, monkeypatch):
        _patch_transcript(
            monkeypatch,
            get_steps=AsyncMock(return_value=[
                {"id": "s1", "type": "user_message", "name": "tom@eagle-exports.com",
                 "output": "hi", "metadata": {}},
            ]),
        )
        _login(client)
        resp = client.get("/chat-ui/embed/threads/t1")
        assert resp.status_code == 200
        assert "chat-ui-embed-data" in resp.text

    def test_embed_threads_json(self, client, monkeypatch):
        _patch_transcript(
            monkeypatch,
            list_threads=AsyncMock(return_value=[{"id": "t1", "name": "A", "agent": "eagle"}]),
        )
        _login(client)
        resp = client.get("/chat-ui/embed-threads")
        assert resp.status_code == 200
        assert resp.json()["threads"][0]["id"] == "t1"

    def test_thread_meta(self, client, monkeypatch):
        _patch_transcript(monkeypatch)
        _login(client)
        resp = client.get("/chat-ui/threads/t1/meta")
        assert resp.status_code == 200
        assert resp.json()["id"] == "t1"
        assert resp.json()["agent"] == "eagle"

    def test_create_thread_embed_returns_json(self, client, monkeypatch):
        _patch_transcript(monkeypatch)
        _login(client)
        resp = client.post("/chat-ui/threads", data={"agent": "research", "embed": "1"})
        assert resp.status_code == 200
        assert resp.json() == {"thread_id": "t1", "agent": "research"}


class TestRunFlow:
    def test_message_requires_text(self, client, monkeypatch):
        _patch_transcript(monkeypatch)
        _login(client)
        resp = client.post("/chat-ui/threads/t1/messages", json={"text": "  "})
        assert resp.status_code == 400

    def test_message_rejected_when_run_active(self, client, monkeypatch):
        _patch_transcript(monkeypatch)
        _login(client)
        fake_task = MagicMock()
        fake_task.done.return_value = False
        import includes.dashboard.routes.chat_ui as chat_ui

        monkeypatch.setattr(
            chat_ui, "_active_runs", {"t1": {"queue": MagicMock(), "task": fake_task}}
        )
        resp = client.post("/chat-ui/threads/t1/messages", json={"text": "hi"})
        assert resp.status_code == 409
        assert "Still working" in resp.json()["error"]

    def test_message_starts_run_and_persists_user_step(self, client, monkeypatch):
        mocks = _patch_transcript(monkeypatch)
        _login(client)
        run_task = AsyncMock()
        import includes.dashboard.routes.chat_ui as chat_ui

        monkeypatch.setattr(chat_ui, "_active_runs", {})
        monkeypatch.setattr(chat_ui, "_run_task", run_task)
        resp = client.post(
            "/chat-ui/threads/t1/messages",
            json={"text": "find a widget", "agent": "internal"},
        )
        assert resp.status_code == 200
        assert mocks["create_step"].call_args.kwargs["type_"] == "user_message"
        assert mocks["create_step"].call_args.kwargs["output"] == "find a widget"

    def test_message_with_intent_routes_to_owning_agent(self, client, monkeypatch):
        _patch_transcript(monkeypatch)
        import includes.dashboard.routes.chat_ui as chat_ui

        monkeypatch.setattr(chat_ui, "_active_runs", {})
        monkeypatch.setattr(chat_ui, "_run_task", AsyncMock())
        _login(client)
        resp = client.post(
            "/chat-ui/threads/t1/messages",
            json={"text": "research this widget", "intent": "research_product_info"},
        )
        assert resp.status_code == 200
        args = chat_ui._run_task.call_args
        assert args.args[3] == "research"
        assert args.kwargs["intent_context"]

    def test_message_with_unknown_intent_falls_back_to_default(self, client, monkeypatch):
        _patch_transcript(monkeypatch)
        import includes.dashboard.routes.chat_ui as chat_ui

        monkeypatch.setattr(chat_ui, "_active_runs", {})
        monkeypatch.setattr(chat_ui, "_run_task", AsyncMock())
        _login(client)
        resp = client.post(
            "/chat-ui/threads/t1/messages",
            json={"text": "hello", "intent": "not_a_real_intent"},
        )
        assert resp.status_code == 200
        args = chat_ui._run_task.call_args
        assert args.args[3] == "eagle"
        assert args.kwargs["intent_context"] == ""

    def test_stream_with_no_active_run_yields_done(self, client, monkeypatch):
        _patch_transcript(monkeypatch)
        _login(client)
        import includes.dashboard.routes.chat_ui as chat_ui

        monkeypatch.setattr(chat_ui, "_active_runs", {})
        resp = client.get("/chat-ui/threads/t1/stream")
        assert resp.status_code == 200
        body = resp.text
        assert "event: done" in body

    def test_stop_requests_cancel(self, client, monkeypatch):
        _patch_transcript(monkeypatch)
        _login(client)
        req = AsyncMock()
        with patch("includes.agent_bridge.request_stop", new=req):
            resp = client.post("/chat-ui/threads/t1/stop")
        assert resp.status_code == 200
        req.assert_awaited_once_with("chat-ui:t1")


class TestUploads:
    def test_upload_requires_thread(self, client, monkeypatch):
        _patch_transcript(monkeypatch)
        _login(client)
        resp = client.post(
            "/chat-ui/upload", files={"files": ("a.txt", b"hello", "text/plain")}
        )
        assert resp.status_code == 400

    def test_upload_persists_element_and_file(self, client, monkeypatch, tmp_path):
        _patch_transcript(monkeypatch)
        monkeypatch.setattr(
            "includes.chat.transcript.ensure_user", AsyncMock(return_value="uid1")
        )
        monkeypatch.setattr(
            "includes.chat.transcript.create_element", AsyncMock(return_value=None)
        )
        monkeypatch.setattr("config.settings.Config.DATA_DIR", str(tmp_path))
        _login(client)
        resp = client.post(
            "/chat-ui/upload",
            data={"thread_id": "t1"},
            files=[("files", ("a.txt", b"hello world", "text/plain"))],
        )
        assert resp.status_code == 200
        f = resp.json()["files"][0]
        assert f["name"] == "a.txt"
        assert f["type"] == "file"
        assert f["url"].startswith("/files/uid1/")
        object_key = f["url"].removeprefix("/files/")
        assert (tmp_path / "attachments" / object_key).read_bytes() == b"hello world"

    def test_upload_image_gets_image_type(self, client, monkeypatch, tmp_path):
        _patch_transcript(monkeypatch)
        monkeypatch.setattr(
            "includes.chat.transcript.ensure_user", AsyncMock(return_value="uid1")
        )
        monkeypatch.setattr(
            "includes.chat.transcript.create_element", AsyncMock(return_value=None)
        )
        monkeypatch.setattr("config.settings.Config.DATA_DIR", str(tmp_path))
        _login(client)
        resp = client.post(
            "/chat-ui/upload",
            data={"thread_id": "t1"},
            files=[("files", ("pic.png", b"\x89PNG", "image/png"))],
        )
        assert resp.status_code == 200
        assert resp.json()["files"][0]["type"] == "image"

    def test_delete_pending_file(self, client, monkeypatch):
        _patch_transcript(monkeypatch)
        monkeypatch.setattr(
            "includes.chat.transcript.get_element",
            AsyncMock(
                return_value={
                    "id": "e1", "type": "file", "name": "a.txt", "url": "/files/x",
                    "display": "inline", "mime": "text/plain", "size": "small",
                    "for_id": None, "object_key": "uid1/e1/a.txt",
                }
            ),
        )
        monkeypatch.setattr(
            "includes.chat.transcript.delete_element", AsyncMock(return_value=True)
        )
        _login(client)
        resp = client.delete("/chat-ui/files/e1?thread_id=t1")
        assert resp.status_code == 200

    def test_delete_attached_file_rejected(self, client, monkeypatch):
        _patch_transcript(monkeypatch)
        monkeypatch.setattr(
            "includes.chat.transcript.get_element",
            AsyncMock(
                return_value={
                    "id": "e1", "type": "file", "name": "a.txt", "url": "/files/x",
                    "display": "inline", "mime": "text/plain", "size": "small",
                    "for_id": "s1", "object_key": "uid1/e1/a.txt",
                }
            ),
        )
        _login(client)
        resp = client.delete("/chat-ui/files/e1?thread_id=t1")
        assert resp.status_code == 400

    def test_message_with_file_ids_attaches_and_processes(self, client, monkeypatch):
        _patch_transcript(monkeypatch)
        import includes.dashboard.routes.chat_ui as chat_ui
        from includes.chat import transcript as tmod

        monkeypatch.setattr(chat_ui, "_active_runs", {})
        monkeypatch.setattr(chat_ui, "_run_task", AsyncMock())
        monkeypatch.setattr(
            "includes.chat.transcript.list_elements",
            AsyncMock(
                return_value=[
                    {
                        "id": "e1", "type": "file", "name": "a.txt", "url": "/files/x",
                        "display": "inline", "mime": "text/plain", "size": "small",
                        "for_id": None, "object_key": "uid1/e1/a.txt",
                    }
                ]
            ),
        )
        monkeypatch.setattr(chat_ui, "_read_file_bytes", lambda path: b"hello")
        monkeypatch.setattr(
            "includes.chat.document_processing.process_file",
            lambda data, mime, name: {
                "filename": name, "mime_type": mime,
                "processed_type": "text", "content": "hello",
            },
        )
        monkeypatch.setattr(
            "includes.chat.transcript.attach_element", AsyncMock(return_value=True)
        )
        _login(client)
        resp = client.post(
            "/chat-ui/threads/t1/messages",
            json={"text": "check this", "file_ids": ["e1"]},
        )
        assert resp.status_code == 200
        assert tmod.attach_element.call_args.args == ("e1", "s1", "t1")
        kwargs = chat_ui._run_task.call_args.kwargs
        assert kwargs["files"][0]["processed_type"] == "text"
        assert kwargs["file_metadata"][0]["name"] == "a.txt"
