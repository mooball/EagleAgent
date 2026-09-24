"""Widget transport: the /chat-ui endpoints and the Tools-menu payload.

The handler itself is stubbed here on purpose — these tests are about the
transport contract (ownership, the pending/closed state machine, metadata
persistence, the transcript notice). Handler behaviour is covered by
``tests/test_supplier_create.py`` and ``tests/chat/test_widgets.py``.
"""

import re

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from includes.chat import transcript
from includes.chat.widgets import WIDGET_META_KEY, WidgetOutcome

OWNER = "tom@eagle-exports.com"


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
    async def _login(request: Request, email: str = OWNER, name: str = "Tom"):
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
def client():
    return TestClient(_make_test_app())


def _login(client, email=OWNER):
    client.get(f"/_test/login?email={email}")


def _widget_state(**overrides):
    state = {
        "id": "w1",
        "name": "add_supplier",
        "status": "pending",
        "data": {},
        "rfq_id": None,
        "error": "",
        "errors": {},
        "duplicates": [],
        "duplicate_checked": False,
        "result": None,
        "notice": "",
    }
    state.update(overrides)
    return state


@pytest.fixture
def widgets_transport(monkeypatch):
    """In-memory stand-in for the transcript + dispatched handler."""
    import includes.dashboard.supplier_widget as supplier_widget

    threads = {"thread-1": {"id": "thread-1", "owner": OWNER}}
    steps: dict[str, dict] = {}
    captured: dict = {"created_steps": [], "dispatched": []}

    async def _get_thread(thread_id, user_email):
        thread = threads.get(thread_id)
        if thread and thread["owner"] == user_email:
            return thread
        return None

    async def _get_step(step_id):
        return steps.get(step_id)

    async def _create_step(thread_id, *, type_="assistant_message",
                           name="EagleAgent", output="", metadata=None,
                           parent_id=None, step_id=None):
        step_id = step_id or f"step-{len(captured['created_steps']) + 1}"
        captured["created_steps"].append({
            "id": step_id, "thread_id": thread_id, "type_": type_,
            "output": output, "metadata": metadata,
        })
        # Every step is stored, widget or not: one test needs a step that exists
        # but carries no widget, to prove that is rejected rather than rendered.
        steps[step_id] = {
            "id": step_id, "thread_id": thread_id, "type": type_,
            "name": name, "output": output,
            "metadata": metadata or {},
        }
        return step_id

    async def _update_step_metadata(step_id, metadata):
        captured.setdefault("metadata_updates", []).append((step_id, metadata))
        if step_id in steps:
            steps[step_id]["metadata"] = metadata

    async def _delete_step(step_id):
        captured.setdefault("deleted", []).append(step_id)
        steps.pop(step_id, None)

    monkeypatch.setattr(transcript, "get_thread", _get_thread)
    monkeypatch.setattr(transcript, "get_step", _get_step)
    monkeypatch.setattr(transcript, "create_step", _create_step)
    monkeypatch.setattr(transcript, "update_step_metadata", _update_step_metadata)
    monkeypatch.setattr(transcript, "delete_step", _delete_step)

    # Keep the card render off the database: the RFQ line picker reads it.
    monkeypatch.setattr(supplier_widget, "rfq_lines", lambda rfq_id: [
        {"line": 1, "part_number": "ABC-1", "description": ""},
        {"line": 2, "part_number": "ABC-2", "description": ""},
    ])
    return steps, captured


def _dispatch_stub(monkeypatch, outcome, captured):
    import includes.chat.widgets as widgets

    def _fake(name, data, state, user_email):
        captured["dispatched"].append({
            "name": name, "data": data, "state": state, "user_email": user_email,
        })
        return outcome

    monkeypatch.setattr(widgets, "dispatch_widget", _fake)


class TestOpen:
    def test_creates_a_widget_step_and_returns_the_card(self, client, widgets_transport):
        _login(client)
        _steps, captured = widgets_transport

        resp = client.post("/chat-ui/threads/thread-1/widgets",
                           json={"name": "add_supplier"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["widget_id"] == captured["created_steps"][0]["id"]
        assert 'data-widget-form="1"' in body["html"]
        assert "Add new supplier" in body["html"]
        assert captured["created_steps"][0]["type_"] == "widget"
        assert captured["created_steps"][0]["metadata"][WIDGET_META_KEY]["status"] == "pending"

    def test_unknown_widget_is_404(self, client, widgets_transport):
        _login(client)
        resp = client.post("/chat-ui/threads/thread-1/widgets",
                           json={"name": "no_such_widget"})
        assert resp.status_code == 404

    def test_another_users_thread_is_404(self, client, widgets_transport):
        _login(client, "someone@else.com")
        resp = client.post("/chat-ui/threads/thread-1/widgets",
                           json={"name": "add_supplier"})
        assert resp.status_code == 404


class TestRender:
    def test_returns_the_card_for_the_owner(self, client, widgets_transport):
        _login(client)
        resp = client.post("/chat-ui/threads/thread-1/widgets",
                           json={"name": "add_supplier"})
        widget_id = resp.json()["widget_id"]

        rendered = client.get(f"/chat-ui/widgets/{widget_id}/render")

        assert rendered.status_code == 200
        assert rendered.json()["status"] == "pending"
        assert 'data-widget-form="1"' in rendered.json()["html"]

    def test_the_rfq_line_picker_only_appears_when_locked_to_an_rfq(
        self, client, widgets_transport
    ):
        _login(client)
        _steps, captured = widgets_transport
        resp = client.post("/chat-ui/threads/thread-1/widgets",
                           json={"name": "add_supplier"})
        widget_id = resp.json()["widget_id"]
        assert "Add to RFQ" not in resp.json()["html"]

        # A bound thread gets the picker, with the RFQ's real lines.
        captured["created_steps"][0]["metadata"][WIDGET_META_KEY]["rfq_id"] = "RFQ-2026-9"
        rendered = client.get(f"/chat-ui/widgets/{widget_id}/render")
        html = rendered.json()["html"]
        assert "RFQ-2026-9" in html and 'name="line_mode"' in html
        assert "Line 1" in html and "Line 2" in html

    @pytest.mark.asyncio
    async def test_a_step_that_is_not_a_widget_is_404(self, client, widgets_transport):
        _login(client)
        _steps, captured = widgets_transport
        # A plain assistant message: metadata holds actions, not a widget.
        await transcript.create_step(
            "thread-1", type_="assistant_message", output="hi",
            metadata={"actions": []},
        )
        resp = client.get("/chat-ui/widgets/step-1/render")
        assert resp.status_code == 404

    def test_an_unknown_step_is_404(self, client, widgets_transport):
        _login(client)
        assert client.get("/chat-ui/widgets/nope/render").status_code == 404


class TestSubmit:
    def _open(self, client):
        resp = client.post("/chat-ui/threads/thread-1/widgets",
                           json={"name": "add_supplier"})
        return resp.json()["widget_id"]

    def test_runs_the_handler_and_persists_the_new_state(
        self, client, widgets_transport, monkeypatch
    ):
        _login(client)
        _steps, captured = widgets_transport
        widget_id = self._open(client)
        _dispatch_stub(monkeypatch, WidgetOutcome(
            status="submitted",
            result={"supplier_id": "sup-1", "name": "Acme Pty Ltd",
                    "email": "a@acme.com", "lines": [1, 2], "line_error": ""},
            notice="✅ Created supplier **Acme Pty Ltd**.",
        ), captured)

        resp = client.post(
            f"/chat-ui/widgets/{widget_id}/submit",
            data={"name": "Acme Pty Ltd", "email": "a@acme.com",
                  "__widget_action": "submit"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "submitted"
        assert "/suppliers/sup-1" in body["html"]
        assert body["notice"]["content"].startswith("✅")
        sent = captured["dispatched"][0]
        assert sent["name"] == "add_supplier"
        assert sent["user_email"] == OWNER
        assert sent["data"]["name"] == "Acme Pty Ltd"
        # The pressed button must survive form parsing — that is what tells
        # Cancel from Add supplier.
        assert sent["data"]["__widget_action"] == "submit"

    def test_a_rejected_submission_re_renders_the_form_with_the_values(
        self, client, widgets_transport, monkeypatch
    ):
        _login(client)
        _steps, captured = widgets_transport
        widget_id = self._open(client)
        _dispatch_stub(monkeypatch, WidgetOutcome(
            status="pending",
            data={"name": "Acme Pty Ltd", "email": "not-an-email"},
            errors={"email": "That does not look like an email address."},
            error="Please fix the highlighted fields.",
        ), captured)

        resp = client.post(
            f"/chat-ui/widgets/{widget_id}/submit",
            data={"name": "Acme Pty Ltd", "email": "not-an-email"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "pending"
        assert "That does not look like an email address." in body["html"]
        # Retyping the whole form to fix one field is the thing this prevents.
        assert 'value="Acme Pty Ltd"' in body["html"]

    def test_the_notice_is_a_persisted_step(self, client, widgets_transport, monkeypatch):
        _login(client)
        _steps, captured = widgets_transport
        widget_id = self._open(client)
        _dispatch_stub(monkeypatch, WidgetOutcome(
            status="submitted", result={"supplier_id": "sup-9", "name": "Acme"},
            notice="✅ Created supplier **Acme**.",
        ), captured)

        client.post(f"/chat-ui/widgets/{widget_id}/submit", data={"name": "Acme"})

        notices = [s for s in captured["created_steps"]
                   if s["type_"] == "assistant_message"]
        assert len(notices) == 1
        assert notices[0]["output"].startswith("✅")

    def test_metadata_is_written_back_so_a_reload_shows_the_result(
        self, client, widgets_transport, monkeypatch
    ):
        _login(client)
        steps, captured = widgets_transport
        widget_id = self._open(client)
        _dispatch_stub(monkeypatch, WidgetOutcome(
            status="submitted", result={"supplier_id": "sup-2", "name": "Acme"},
        ), captured)

        client.post(f"/chat-ui/widgets/{widget_id}/submit", data={"name": "Acme"})

        stored = steps[widget_id]["metadata"][WIDGET_META_KEY]
        assert stored["status"] == "submitted"
        assert stored["result"]["supplier_id"] == "sup-2"

    def test_cancelling_removes_the_card_entirely(
        self, client, widgets_transport, monkeypatch
    ):
        """Cancelling leaves nothing behind — no "nothing was saved" note, and
        no step for a reload to render one from."""
        _login(client)
        steps, captured = widgets_transport
        widget_id = self._open(client)
        _dispatch_stub(monkeypatch, WidgetOutcome(status="cancelled"), captured)

        resp = client.post(f"/chat-ui/widgets/{widget_id}/submit",
                           data={"__widget_action": "cancel"})

        body = resp.json()
        assert body["status"] == "cancelled"
        assert body["removed"] is True
        assert "html" not in body, "a removed card has nothing to re-render"
        assert captured["dispatched"][0]["data"]["__widget_action"] == "cancel"
        assert captured["deleted"] == [widget_id]
        assert widget_id not in steps, "the step is what a reload renders"
        assert not [s for s in captured["created_steps"] if s["type_"] == "assistant_message"], (
            "a cancel must not write a transcript line"
        )

    def test_a_cancelled_widget_cannot_be_submitted_again(
        self, client, widgets_transport, monkeypatch
    ):
        _login(client)
        _steps, captured = widgets_transport
        widget_id = self._open(client)
        _dispatch_stub(monkeypatch, WidgetOutcome(status="cancelled"), captured)
        client.post(f"/chat-ui/widgets/{widget_id}/submit",
                    data={"__widget_action": "cancel"})

        again = client.post(f"/chat-ui/widgets/{widget_id}/submit", data={"name": "Acme"})

        assert again.status_code == 404, "the widget no longer exists"
        assert len(captured["dispatched"]) == 1

    def test_a_closed_widget_cannot_be_submitted_again(
        self, client, widgets_transport, monkeypatch
    ):
        """A double click, or a stale tab, must not create a second supplier."""
        _login(client)
        _steps, captured = widgets_transport
        widget_id = self._open(client)
        _dispatch_stub(monkeypatch, WidgetOutcome(
            status="submitted", result={"supplier_id": "sup-3", "name": "Acme"},
        ), captured)
        client.post(f"/chat-ui/widgets/{widget_id}/submit", data={"name": "Acme"})

        again = client.post(f"/chat-ui/widgets/{widget_id}/submit", data={"name": "Acme"})

        assert again.status_code == 409
        assert again.json()["html"]            # the current card, so the client can re-sync
        assert len(captured["dispatched"]) == 1

    def test_a_handler_failure_is_reported_without_closing_the_widget(
        self, client, widgets_transport, monkeypatch
    ):
        _login(client)
        _steps, captured = widgets_transport
        widget_id = self._open(client)
        import includes.chat.widgets as widgets

        def _boom(*_a, **_k):
            raise RuntimeError("database on fire")

        monkeypatch.setattr(widgets, "dispatch_widget", _boom)

        resp = client.post(f"/chat-ui/widgets/{widget_id}/submit", data={"name": "Acme"})

        assert resp.status_code == 500
        # Still pending: a transient failure must not burn the form.
        rendered = client.get(f"/chat-ui/widgets/{widget_id}/render")
        assert rendered.json()["status"] == "pending"

    def test_a_dashboard_command_from_the_handler_reaches_the_client(
        self, client, widgets_transport, monkeypatch
    ):
        """The route is the only thing that can carry a shell command out of a
        widget, since the submit is not an agent run with an SSE queue."""
        _login(client)
        _steps, captured = widgets_transport
        widget_id = self._open(client)
        _dispatch_stub(monkeypatch, WidgetOutcome(
            status="submitted",
            result={"supplier_id": "sup-4", "name": "Acme", "lines": [1]},
            dashboard={"command": "dashboard_refresh", "payload": {"rfq_id": "RFQ-2026-1"}},
        ), captured)

        body = client.post(f"/chat-ui/widgets/{widget_id}/submit",
                           data={"name": "Acme"}).json()

        assert body["dashboard"] == {
            "command": "dashboard_refresh", "payload": {"rfq_id": "RFQ-2026-1"},
        }

    def test_no_dashboard_key_when_the_handler_has_nothing_to_say(
        self, client, widgets_transport, monkeypatch
    ):
        _login(client)
        _steps, captured = widgets_transport
        widget_id = self._open(client)
        _dispatch_stub(monkeypatch, WidgetOutcome(status="cancelled"), captured)

        body = client.post(f"/chat-ui/widgets/{widget_id}/submit",
                           data={"__widget_action": "cancel"}).json()

        assert "dashboard" not in body


class TestCardContract:
    """The card and the routes must agree on one id.

    Regression (found live 2026-09-24): the state carried its own freshly
    generated uuid while the step had another, so the card's rendered action URL
    addressed a row that did not exist — every submit and cancel 404'd. The
    earlier tests all used the id the *route* returned, so none of them followed
    the id the card actually emits.
    """

    def _open(self, client):
        return client.post("/chat-ui/threads/thread-1/widgets",
                           json={"name": "add_supplier"}).json()["widget_id"]

    def test_the_card_posts_to_the_id_the_routes_serve(
        self, client, widgets_transport, monkeypatch
    ):
        _login(client)
        _steps, captured = widgets_transport
        widget_id = self._open(client)
        _dispatch_stub(monkeypatch, WidgetOutcome(status="pending"), captured)

        html = client.get(f"/chat-ui/widgets/{widget_id}/render").json()["html"]
        card_id = re.search(r'data-widget-id="([^"]+)"', html).group(1)
        action = re.search(r'action="([^"]+)"', html).group(1)

        assert card_id == widget_id, (
            "the id in the markup must be the id the routes resolve; a second "
            "generated uuid makes every request the card makes 404"
        )
        assert action == f"/chat-ui/widgets/{widget_id}/submit"
        # Post to exactly what the browser would post to.
        assert client.post(action, data={"name": "Acme"}).status_code == 200
        assert client.post(
            action, data={"__widget_action": "cancel"}
        ).status_code in (200, 409)

    def test_the_open_response_carries_the_same_id_it_persisted(
        self, client, widgets_transport
    ):
        _login(client)
        _steps, captured = widgets_transport
        body = client.post("/chat-ui/threads/thread-1/widgets",
                           json={"name": "add_supplier"}).json()

        card_id = re.search(r'data-widget-id="([^"]+)"', body["html"]).group(1)
        assert card_id == body["widget_id"]
        assert body["widget_id"] == captured["created_steps"][0]["id"]


class TestToolsMenu:
    def test_widgets_are_offered_separately_from_commands(self):
        """Commands route a message to an agent; widgets open a card. The
        standalone /chat-ui page consumes ``commands`` and has no renderer, so
        the two lists must stay apart."""
        from includes.dashboard.routes.chat_ui import (
            _command_data,
            _widget_commands,
        )

        widget_names = [entry["name"] for entry in _widget_commands()]
        assert "add_supplier" in widget_names
        assert all(entry["type"] == "widget" for entry in _widget_commands())
        assert "add_supplier" not in [entry["name"] for entry in _command_data()]

    def test_the_embed_payload_carries_the_widget_list(self, monkeypatch):
        """The menu is built client-side from this payload — a widget missing
        here is invisible in the UI no matter how well it works."""
        import includes.dashboard.routes.chat_ui as chat_ui

        monkeypatch.setattr(chat_ui, "_rfq_binding_map", lambda email: {})
        monkeypatch.setattr(chat_ui, "_get_current_thread_id", lambda email: None)

        payload = chat_ui._embed_data({"email": OWNER, "name": "Tom"}, [], None, [])

        names = [entry["name"] for entry in payload["widgets"]]
        assert names == ["add_supplier"]
        assert "commands" in payload      # unchanged for the standalone UI
