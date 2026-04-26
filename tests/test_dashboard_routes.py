"""Tests for FastAPI dashboard routes, auth, and middleware.

Uses a lightweight test FastAPI app that mounts the dashboard router
with mocked DB sessions — avoids importing main.py (which triggers
Chainlit mount and Google SSO init).
"""

import pytest
import uuid
from unittest.mock import patch, MagicMock, PropertyMock
from collections import namedtuple

from fastapi import FastAPI, Request, Depends
from fastapi.responses import Response
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware


# ============================================================================
# Fixtures: lightweight test app with the dashboard router
# ============================================================================

def _make_test_app():
    """Create a minimal FastAPI app with the dashboard router for testing."""
    app = FastAPI()
    app.add_middleware(
        SessionMiddleware,
        secret_key="test-secret-key-for-testing",
        session_cookie="eagleagent_session",
    )

    # Mount dashboard routes
    from includes.dashboard.routes import router
    app.include_router(router)

    # Utility endpoint to inject a user session (test-only)
    @app.get("/_test/login")
    async def _test_login(request: Request, email: str = "admin@eagle.com",
                          name: str = "Test Admin"):
        request.session["user"] = {
            "email": email,
            "name": name,
            "given_name": name.split()[0] if name else "",
            "family_name": name.split()[-1] if name else "",
            "picture": "",
            "hd": email.split("@")[-1],
        }
        return Response(status_code=200)

    @app.get("/_test/logout")
    async def _test_logout(request: Request):
        request.session.clear()
        return Response(status_code=200)

    return app


@pytest.fixture
def app():
    return _make_test_app()


@pytest.fixture
def client(app):
    return TestClient(app)


def _login(client, email="admin@eagle.com", name="Test Admin"):
    """Helper: hit the test login endpoint so the session is populated."""
    client.get(f"/_test/login?email={email}&name={name}")


# ============================================================================
# Fake DB data builders
# ============================================================================

def _make_supplier(id=1, name="Acme Corp", country="AU", city="Brisbane",
                   contacts=None, notes=None, embedding=None):
    s = MagicMock()
    s.id = id
    s.name = name
    s.country = country
    s.city = city
    s.contacts = contacts
    s.notes = notes
    s.embedding = embedding
    return s


def _make_product(id=1, part_number="ABC-123", brand="TestBrand",
                  description="A widget", product_type="Part"):
    p = MagicMock()
    p.id = id
    p.part_number = part_number
    p.brand = brand
    p.description = description
    p.product_type = product_type
    return p


# ============================================================================
# Auth: require_user redirects unauthenticated requests
# ============================================================================

class TestRequireUser:
    def test_unauthenticated_redirects_to_login(self, client):
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_unauthenticated_suppliers_redirects(self, client):
        resp = client.get("/suppliers", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_authenticated_user_can_access_home(self, client):
        _login(client)
        with patch("includes.dashboard.routes.get_session") as mock_gs, \
             patch("includes.dashboard.routes._get_store", return_value=None):
            session = MagicMock()
            session.query.return_value.scalar.return_value = 0
            mock_gs.return_value = session
            resp = client.get("/")
            assert resp.status_code == 200


# ============================================================================
# Auth: require_role / require_admin
# ============================================================================

class TestRequireRole:
    @patch("includes.dashboard.routes.config")
    def test_admin_can_access_users(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        mock_config.TIMEZONE = "Australia/Brisbane"
        _login(client, email="admin@eagle.com")

        with patch("includes.dashboard.routes.get_session") as mock_gs, \
             patch("includes.dashboard.routes._get_store", return_value=None):
            session = MagicMock()
            # _USER_STATS_SQL returns rows
            session.execute.return_value.fetchall.return_value = []
            mock_gs.return_value = session
            resp = client.get("/users")
            assert resp.status_code == 200

    @patch("includes.dashboard.routes.config")
    def test_staff_cannot_access_users(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client, email="staff@eagle.com")
        resp = client.get("/users", follow_redirects=False)
        assert resp.status_code == 403

    @patch("includes.dashboard.routes.config")
    def test_staff_cannot_access_rfqs(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client, email="staff@eagle.com")
        resp = client.get("/rfqs", follow_redirects=False)
        assert resp.status_code == 403

    @patch("includes.dashboard.routes.config")
    def test_admin_can_access_rfqs(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client, email="admin@eagle.com")

        with patch("includes.dashboard.routes._get_store", return_value=None):
            resp = client.get("/rfqs")
            assert resp.status_code == 200

    @patch("includes.dashboard.routes.config")
    def test_staff_can_access_suppliers(self, mock_config, client):
        """Staff role should be able to access non-admin routes."""
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client, email="staff@eagle.com")

        with patch("includes.dashboard.routes.get_session") as mock_gs:
            session = MagicMock()
            session.query.return_value.outerjoin.return_value.group_by.return_value = session.query.return_value
            session.query.return_value.count.return_value = 0
            session.query.return_value.outerjoin.return_value.group_by.return_value.count.return_value = 0
            session.query.return_value.outerjoin.return_value.group_by.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []
            mock_gs.return_value = session
            resp = client.get("/suppliers")
            assert resp.status_code == 200


# ============================================================================
# HTMX vs full-page rendering
# ============================================================================

class TestHtmxRendering:
    @patch("includes.dashboard.routes.config")
    def test_htmx_request_returns_partial(self, mock_config, client):
        """An HX-Request header should return the partial template."""
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes.get_session") as mock_gs:
            session = MagicMock()
            qm = session.query.return_value.outerjoin.return_value.group_by.return_value
            qm.count.return_value = 0
            qm.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []
            mock_gs.return_value = session

            resp = client.get("/suppliers", headers={"HX-Request": "true"})
            assert resp.status_code == 200
            # Partial should NOT contain <html> or base layout
            assert "<html" not in resp.text

    @patch("includes.dashboard.routes.config")
    def test_full_page_request_returns_full_template(self, mock_config, client):
        """A normal request (no HX-Request) should return the full page."""
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes.get_session") as mock_gs:
            session = MagicMock()
            qm = session.query.return_value.outerjoin.return_value.group_by.return_value
            qm.count.return_value = 0
            qm.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []
            mock_gs.return_value = session

            resp = client.get("/suppliers")
            assert resp.status_code == 200
            assert "<html" in resp.text


# ============================================================================
# Dashboard home — stats
# ============================================================================

class TestDashboardHome:
    @patch("includes.dashboard.routes.config")
    def test_home_renders_stats(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes.get_session") as mock_gs, \
             patch("includes.dashboard.routes._get_store", return_value=None):
            session = MagicMock()
            # First call returns supplier count, second returns product count
            session.query.return_value.scalar.side_effect = [42, 100]
            mock_gs.return_value = session

            resp = client.get("/")
            assert resp.status_code == 200
            assert "42" in resp.text
            assert "100" in resp.text

    @patch("includes.dashboard.routes.config")
    def test_home_zero_stats(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes.get_session") as mock_gs, \
             patch("includes.dashboard.routes._get_store", return_value=None):
            session = MagicMock()
            session.query.return_value.scalar.return_value = 0
            mock_gs.return_value = session

            resp = client.get("/")
            assert resp.status_code == 200


# ============================================================================
# Supplier routes
# ============================================================================

class TestSupplierRoutes:
    @patch("includes.dashboard.routes.config")
    def test_supplier_list_empty(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes.get_session") as mock_gs:
            session = MagicMock()
            qm = session.query.return_value.outerjoin.return_value.group_by.return_value
            qm.count.return_value = 0
            qm.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []
            mock_gs.return_value = session

            resp = client.get("/suppliers")
            assert resp.status_code == 200
            # Should show empty state
            assert "No suppliers" in resp.text or "suppliers" in resp.text.lower()

    @patch("includes.dashboard.routes.config")
    def test_supplier_list_with_search(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes.get_session") as mock_gs:
            session = MagicMock()
            qm = session.query.return_value.outerjoin.return_value.group_by.return_value
            qm_filtered = qm.filter.return_value
            qm_filtered.count.return_value = 0
            qm_filtered.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []
            mock_gs.return_value = session

            resp = client.get("/suppliers?q=test")
            assert resp.status_code == 200

    @patch("includes.dashboard.routes.config")
    def test_supplier_detail_not_found_redirects(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes.get_session") as mock_gs:
            session = MagicMock()
            session.query.return_value.filter.return_value.first.return_value = None
            mock_gs.return_value = session

            resp = client.get("/suppliers/999", follow_redirects=False)
            assert resp.status_code == 307
            assert "/suppliers" in resp.headers.get("location", "")

    @patch("includes.dashboard.routes.config")
    def test_supplier_detail_found(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        supplier = _make_supplier(id=1, name="Acme Corp")

        with patch("includes.dashboard.routes.get_session") as mock_gs:
            session = MagicMock()
            session.query.return_value.filter.return_value.first.return_value = supplier

            # Brands query
            brand_query = session.query.return_value.join.return_value.filter.return_value
            brand_query.filter.return_value.order_by.return_value.all.return_value = []

            # Purchases query
            purchase_query = session.query.return_value.join.return_value.filter.return_value
            purchase_query.order_by.return_value.limit.return_value.all.return_value = []

            mock_gs.return_value = session

            resp = client.get("/suppliers/1")
            assert resp.status_code == 200
            assert "Acme Corp" in resp.text


# ============================================================================
# Product routes
# ============================================================================

class TestProductRoutes:
    @patch("includes.dashboard.routes.config")
    def test_product_list_empty(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes.get_session") as mock_gs:
            session = MagicMock()
            session.query.return_value.count.return_value = 0
            session.query.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []
            mock_gs.return_value = session

            resp = client.get("/products")
            assert resp.status_code == 200

    @patch("includes.dashboard.routes.config")
    def test_product_detail_not_found_redirects(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes.get_session") as mock_gs:
            session = MagicMock()
            session.query.return_value.filter.return_value.first.return_value = None
            mock_gs.return_value = session

            resp = client.get("/products/999", follow_redirects=False)
            assert resp.status_code == 307
            assert "/products" in resp.headers.get("location", "")


# ============================================================================
# Partial routes (HTMX fragments)
# ============================================================================

class TestPartialRoutes:
    @patch("includes.dashboard.routes.config")
    def test_partial_suppliers_returns_html_fragment(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes.get_session") as mock_gs:
            session = MagicMock()
            qm = session.query.return_value.outerjoin.return_value.group_by.return_value
            qm.count.return_value = 0
            qm.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []
            mock_gs.return_value = session

            resp = client.get("/partial/suppliers")
            assert resp.status_code == 200
            assert "<html" not in resp.text  # should be a fragment

    @patch("includes.dashboard.routes.config")
    def test_partial_products_returns_html_fragment(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes.get_session") as mock_gs:
            session = MagicMock()
            session.query.return_value.count.return_value = 0
            session.query.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []
            mock_gs.return_value = session

            resp = client.get("/partial/products")
            assert resp.status_code == 200
            assert "<html" not in resp.text

    @patch("includes.dashboard.routes.config")
    def test_partial_supplier_detail_not_found(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes.get_session") as mock_gs:
            session = MagicMock()
            session.query.return_value.filter.return_value.first.return_value = None
            mock_gs.return_value = session

            resp = client.get("/partial/suppliers/999")
            assert resp.status_code == 200
            assert "not found" in resp.text.lower()

    @patch("includes.dashboard.routes.config")
    def test_partial_rfqs_admin_only(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client, email="staff@eagle.com")
        resp = client.get("/partial/rfqs", follow_redirects=False)
        assert resp.status_code == 403

    @patch("includes.dashboard.routes.config")
    def test_partial_users_admin_only(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client, email="staff@eagle.com")
        resp = client.get("/partial/users", follow_redirects=False)
        assert resp.status_code == 403
