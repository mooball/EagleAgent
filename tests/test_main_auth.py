"""Tests for FastAPI auth: inject_chainlit_auth middleware, session helpers,
dashboard context API, and logout flow.

These tests build a small FastAPI app that replicates the middleware and
endpoints from main.py without importing main.py (which triggers Chainlit
mount and Google SSO env var requirements).
"""

import json
import pytest
from unittest.mock import patch, MagicMock

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware


# ============================================================================
# Build a lightweight app that replicates the auth middleware from main.py
# ============================================================================

def _make_auth_test_app():
    app = FastAPI()

    @app.middleware("http")
    async def inject_chainlit_auth(request: Request, call_next):
        """Replica of main.py's inject_chainlit_auth middleware."""
        if request.url.path.startswith("/chat"):
            raw_headers = [
                (k, v) for k, v in request.headers.raw
                if not k.lower().startswith(b"x-chainlit-user-")
            ]
            user = request.session.get("user")
            if user:
                raw_headers.append((b"x-chainlit-user-email", user["email"].encode()))
                raw_headers.append((b"x-chainlit-user-name", user.get("name", "").encode()))
                raw_headers.append((b"x-chainlit-user-given-name", user.get("given_name", "").encode()))
                raw_headers.append((b"x-chainlit-user-family-name", user.get("family_name", "").encode()))
                raw_headers.append((b"x-chainlit-user-picture", user.get("picture", "").encode()))
                raw_headers.append((b"x-chainlit-user-hd", user.get("hd", "").encode()))
            scope = request.scope
            scope["headers"] = raw_headers
        return await call_next(request)

    app.add_middleware(
        SessionMiddleware,
        secret_key="test-secret",
        session_cookie="eagleagent_session",
    )

    # Test endpoint: inject session
    @app.get("/_test/login")
    async def _login(request: Request, email: str = "admin@eagle.com"):
        request.session["user"] = {
            "email": email,
            "name": "Admin User",
            "given_name": "Admin",
            "family_name": "User",
            "picture": "https://example.com/pic.jpg",
            "hd": email.split("@")[-1],
        }
        return Response(status_code=200)

    @app.get("/_test/logout")
    async def _logout(request: Request):
        request.session.clear()
        return Response(status_code=200)

    # Diagnostic endpoint: echo headers received by the downstream handler
    @app.get("/chat/test-headers")
    async def echo_headers(request: Request):
        """Return all X-Chainlit-User-* headers as JSON."""
        cl_headers = {
            k: v for k, v in request.headers.items()
            if k.startswith("x-chainlit-user-")
        }
        return Response(
            content=json.dumps(cl_headers),
            media_type="application/json",
        )

    # Non-chat endpoint: should not have headers injected
    @app.get("/api/test-headers")
    async def echo_api_headers(request: Request):
        cl_headers = {
            k: v for k, v in request.headers.items()
            if k.startswith("x-chainlit-user-")
        }
        return Response(
            content=json.dumps(cl_headers),
            media_type="application/json",
        )

    return app


@pytest.fixture
def auth_client():
    app = _make_auth_test_app()
    return TestClient(app)


def _login(client, email="admin@eagle.com"):
    client.get(f"/_test/login?email={email}")


# ============================================================================
# Middleware: inject_chainlit_auth
# ============================================================================

class TestInjectChainlitAuth:
    def test_injects_headers_for_chat_routes(self, auth_client):
        """Authenticated user should get X-Chainlit-User-* headers on /chat paths."""
        _login(auth_client)
        resp = auth_client.get("/chat/test-headers")
        headers = resp.json()
        assert headers["x-chainlit-user-email"] == "admin@eagle.com"
        assert headers["x-chainlit-user-name"] == "Admin User"
        assert headers["x-chainlit-user-given-name"] == "Admin"
        assert headers["x-chainlit-user-family-name"] == "User"
        assert headers["x-chainlit-user-hd"] == "eagle.com"

    def test_strips_spoofed_headers(self, auth_client):
        """Externally-provided X-Chainlit-User-* headers must be stripped."""
        _login(auth_client)
        resp = auth_client.get(
            "/chat/test-headers",
            headers={"X-Chainlit-User-Email": "hacker@evil.com"},
        )
        headers = resp.json()
        # Should have the real session email, not the spoofed one
        assert headers["x-chainlit-user-email"] == "admin@eagle.com"

    def test_unauthenticated_no_headers(self, auth_client):
        """Unauthenticated requests to /chat should not get injected headers."""
        resp = auth_client.get("/chat/test-headers")
        headers = resp.json()
        assert "x-chainlit-user-email" not in headers

    def test_spoofed_headers_stripped_when_unauthenticated(self, auth_client):
        """Even without a session, spoofed headers must be removed."""
        resp = auth_client.get(
            "/chat/test-headers",
            headers={"X-Chainlit-User-Email": "hacker@evil.com"},
        )
        headers = resp.json()
        assert "x-chainlit-user-email" not in headers

    def test_non_chat_routes_unaffected(self, auth_client):
        """Non-/chat routes should pass headers through unchanged."""
        resp = auth_client.get(
            "/api/test-headers",
            headers={"X-Chainlit-User-Email": "external@test.com"},
        )
        headers = resp.json()
        # Middleware should NOT strip these for non-chat paths
        assert headers.get("x-chainlit-user-email") == "external@test.com"


# ============================================================================
# Session: login / logout
# ============================================================================

class TestSessionFlow:
    def test_login_sets_session(self, auth_client):
        _login(auth_client)
        resp = auth_client.get("/chat/test-headers")
        assert resp.json()["x-chainlit-user-email"] == "admin@eagle.com"

    def test_logout_clears_session(self, auth_client):
        _login(auth_client)
        auth_client.get("/_test/logout")
        resp = auth_client.get("/chat/test-headers")
        assert "x-chainlit-user-email" not in resp.json()


# ============================================================================
# Dashboard context API (replicates the main.py endpoints)
# ============================================================================

def _make_context_api_app():
    """App with the dashboard-context POST/GET endpoints."""
    from includes.dashboard_context import set_context, get_context, format_context_for_prompt

    app = FastAPI()
    app.add_middleware(
        SessionMiddleware,
        secret_key="test-secret",
        session_cookie="eagleagent_session",
    )

    def _get_current_user(request: Request):
        return request.session.get("user")

    @app.get("/_test/login")
    async def _login(request: Request, email: str = "user@eagle.com"):
        request.session["user"] = {"email": email, "name": "Test"}
        return Response(status_code=200)

    @app.post("/api/dashboard-context")
    async def update_context(request: Request):
        user = _get_current_user(request)
        if not user:
            return Response(status_code=401)
        body = await request.json()
        set_context(user["email"], body)
        return Response(status_code=204)

    @app.get("/api/dashboard-context")
    async def get_context_api(request: Request):
        user = _get_current_user(request)
        if not user:
            return Response(status_code=401)
        ctx = get_context(user["email"])
        formatted = format_context_for_prompt(user["email"])
        return Response(
            content=json.dumps({"context": ctx, "formatted": formatted}),
            media_type="application/json",
        )

    return app


@pytest.fixture
def ctx_client():
    return TestClient(_make_context_api_app())


class TestDashboardContextAPI:
    def test_post_unauthenticated_returns_401(self, ctx_client):
        resp = ctx_client.post(
            "/api/dashboard-context",
            json={"view": "supplier_list"},
        )
        assert resp.status_code == 401

    def test_get_unauthenticated_returns_401(self, ctx_client):
        resp = ctx_client.get("/api/dashboard-context")
        assert resp.status_code == 401

    def test_post_and_get_roundtrip(self, ctx_client):
        ctx_client.get("/_test/login?email=ctx@eagle.com")
        resp = ctx_client.post(
            "/api/dashboard-context",
            json={"view": "supplier_detail", "entity": "supplier", "id": "42"},
        )
        assert resp.status_code == 204

        resp = ctx_client.get("/api/dashboard-context")
        assert resp.status_code == 200
        data = resp.json()
        assert data["context"]["view"] == "supplier_detail"
        assert "supplier_detail" in data["formatted"]

    def test_context_overwrite(self, ctx_client):
        ctx_client.get("/_test/login?email=over@eagle.com")
        ctx_client.post("/api/dashboard-context", json={"view": "a"})
        ctx_client.post("/api/dashboard-context", json={"view": "b"})
        resp = ctx_client.get("/api/dashboard-context")
        assert resp.json()["context"]["view"] == "b"
