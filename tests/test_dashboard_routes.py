"""Tests for FastAPI dashboard routes, auth, and middleware.

Uses a lightweight test FastAPI app that mounts the dashboard router
with mocked DB sessions — avoids importing main.py (which triggers
Chainlit mount and Google SSO init).
"""

import pytest
import uuid
from unittest.mock import patch, MagicMock, PropertyMock, AsyncMock
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
                   contacts=None, notes=None, embedding=None, use_instead=None,
                   url=None):
    s = MagicMock()
    s.id = id
    s.name = name
    s.country = country
    s.city = city
    s.contacts = contacts
    s.notes = notes
    s.embedding = embedding
    s.use_instead = use_instead
    s.url = url
    s.supply_chain_position = {}
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
        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs, \
             patch("includes.dashboard.routes.rfqs._get_store", return_value=None):
            session = MagicMock()
            session.query.return_value.scalar.return_value = 0
            mock_gs.return_value = session
            resp = client.get("/")
            assert resp.status_code == 200


# ============================================================================
# Auth: require_role / require_admin
# ============================================================================

class TestRequireRole:
    @patch("includes.dashboard.routes._helpers.config")
    def test_admin_can_access_users(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        mock_config.TIMEZONE = "Australia/Brisbane"
        _login(client, email="admin@eagle.com")

        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs, \
             patch("includes.dashboard.routes.rfqs._get_store", return_value=None):
            session = MagicMock()
            # _USER_STATS_SQL returns rows
            session.execute.return_value.fetchall.return_value = []
            mock_gs.return_value = session
            resp = client.get("/users")
            assert resp.status_code == 200

    @patch("includes.dashboard.routes._helpers.config")
    def test_staff_cannot_access_users(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client, email="staff@eagle.com")
        resp = client.get("/users", follow_redirects=False)
        assert resp.status_code == 403

    @patch("includes.dashboard.routes._helpers.config")
    def test_staff_can_access_rfqs(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client, email="staff@eagle.com")

        with patch("includes.dashboard.routes.rfqs._get_store", return_value=None):
            resp = client.get("/rfqs")
            assert resp.status_code == 200

    @patch("includes.dashboard.routes._helpers.config")
    def test_admin_can_access_rfqs(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client, email="admin@eagle.com")

        with patch("includes.dashboard.routes.rfqs._get_store", return_value=None):
            resp = client.get("/rfqs")
            assert resp.status_code == 200

    @patch("includes.dashboard.routes._helpers.config")
    def test_staff_can_access_suppliers(self, mock_config, client):
        """Staff role should be able to access non-admin routes."""
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client, email="staff@eagle.com")

        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            qm = session.query.return_value.outerjoin.return_value.group_by.return_value
            qm.filter.return_value.count.return_value = 0
            qm.filter.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []
            mock_gs.return_value = session
            resp = client.get("/suppliers")
            assert resp.status_code == 200


# ============================================================================
# HTMX vs full-page rendering
# ============================================================================

class TestHtmxRendering:
    @patch("includes.dashboard.routes._helpers.config")
    def test_htmx_request_returns_partial(self, mock_config, client):
        """An HX-Request header should return the partial template."""
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            qm = session.query.return_value.outerjoin.return_value.group_by.return_value
            qm.filter.return_value.count.return_value = 0
            qm.filter.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []
            mock_gs.return_value = session

            resp = client.get("/suppliers", headers={"HX-Request": "true"})
            assert resp.status_code == 200
            # Partial should NOT contain <html> or base layout
            assert "<html" not in resp.text

    @patch("includes.dashboard.routes._helpers.config")
    def test_full_page_request_returns_full_template(self, mock_config, client):
        """A normal request (no HX-Request) should return the full page."""
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            qm = session.query.return_value.outerjoin.return_value.group_by.return_value
            qm.filter.return_value.count.return_value = 0
            qm.filter.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []
            mock_gs.return_value = session

            resp = client.get("/suppliers")
            assert resp.status_code == 200
            assert "<html" in resp.text


# ============================================================================
# Dashboard home — stats
# ============================================================================

class TestDashboardHome:
    @patch("includes.dashboard.routes._helpers.config")
    def test_home_renders_stats(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            # supplier, product, purchase, sub-stats(3), product_embedding, customers, contacts, opportunities, rfq
            session.query.return_value.scalar.side_effect = [42, 100, 55, 30, 20, 10, 80, 200, 50, 75, 8]
            session.query.return_value.filter.return_value.scalar.return_value = 5
            mock_gs.return_value = session

            resp = client.get("/")
            assert resp.status_code == 200
            assert "42" in resp.text
            assert "100" in resp.text

    @patch("includes.dashboard.routes._helpers.config")
    def test_home_zero_stats(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            session.query.return_value.scalar.return_value = 0
            mock_gs.return_value = session

            resp = client.get("/")
            assert resp.status_code == 200


# ============================================================================
# Supplier routes
# ============================================================================

class TestSupplierRoutes:
    @patch("includes.dashboard.routes._helpers.config")
    def test_supplier_list_empty(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            qm = session.query.return_value.outerjoin.return_value.group_by.return_value
            qm.filter.return_value.count.return_value = 0
            qm.filter.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []
            mock_gs.return_value = session

            resp = client.get("/suppliers")
            assert resp.status_code == 200
            # Should show empty state
            assert "No suppliers" in resp.text or "suppliers" in resp.text.lower()

    @patch("includes.dashboard.routes._helpers.config")
    def test_supplier_list_with_search(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            qm = session.query.return_value.outerjoin.return_value.group_by.return_value
            qm_filtered = qm.filter.return_value.filter.return_value
            qm_filtered.count.return_value = 0
            qm_filtered.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []
            mock_gs.return_value = session

            resp = client.get("/suppliers?q=test")
            assert resp.status_code == 200

    @patch("includes.dashboard.routes._helpers.config")
    def test_supplier_detail_not_found_redirects(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            session.query.return_value.filter.return_value.first.return_value = None
            mock_gs.return_value = session

            resp = client.get("/suppliers/999", follow_redirects=False)
            assert resp.status_code == 307
            assert "/suppliers" in resp.headers.get("location", "")

    @patch("includes.dashboard.routes._helpers.config")
    def test_supplier_detail_found(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        supplier = _make_supplier(id=1, name="Acme Corp")

        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
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

    @patch("includes.dashboard.routes._helpers.config")
    def test_supplier_list_marks_duplicates(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        dup = _make_supplier(id=1, name="The Billiard Shop", use_instead="2")
        normal = _make_supplier(id=2, name="Billiard Shop")

        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            qm = session.query.return_value.outerjoin.return_value.group_by.return_value
            qm.filter.return_value.count.return_value = 2
            qm.filter.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = [
                (dup, 5), (normal, 3)
            ]
            mock_gs.return_value = session

            resp = client.get("/suppliers")
            assert resp.status_code == 200
            assert "duplicate" in resp.text

    @patch("includes.dashboard.routes._helpers.config")
    def test_supplier_detail_shows_use_instead_banner(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        target = _make_supplier(id=1, name="Billiard Shop")
        dup = _make_supplier(id=2, name="The Billiard Shop", use_instead="1")

        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            # First .first(): the requested supplier; second: the use_instead target
            session.query.return_value.filter.return_value.first.side_effect = [dup, target]

            # Brands query
            brand_query = session.query.return_value.join.return_value.filter.return_value
            brand_query.filter.return_value.order_by.return_value.all.return_value = []

            # Purchases query
            purchase_query = session.query.return_value.join.return_value.filter.return_value
            purchase_query.order_by.return_value.limit.return_value.all.return_value = []

            mock_gs.return_value = session

            resp = client.get("/suppliers/2")
            assert resp.status_code == 200
            assert "This supplier is a duplicate." in resp.text
            assert "Billiard Shop" in resp.text


# ============================================================================
# Product routes
# ============================================================================

class TestProductRoutes:
    @patch("includes.dashboard.routes._helpers.config")
    def test_product_list_empty(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            qm = session.query.return_value.outerjoin.return_value.group_by.return_value
            qm.count.return_value = 0
            qm.filter.return_value.count.return_value = 0
            qm.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []
            mock_gs.return_value = session

            resp = client.get("/products")
            assert resp.status_code == 200

    @patch("includes.dashboard.routes._helpers.config")
    def test_product_detail_not_found_redirects(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
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
    @patch("includes.dashboard.routes._helpers.config")
    def test_partial_suppliers_returns_html_fragment(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            qm = session.query.return_value.outerjoin.return_value.group_by.return_value
            qm.filter.return_value.count.return_value = 0
            qm.filter.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []
            mock_gs.return_value = session

            resp = client.get("/partial/suppliers")
            assert resp.status_code == 200
            assert "<html" not in resp.text  # should be a fragment

    @patch("includes.dashboard.routes._helpers.config")
    def test_partial_products_returns_html_fragment(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            qm = session.query.return_value.outerjoin.return_value.group_by.return_value
            qm.count.return_value = 0
            qm.filter.return_value.count.return_value = 0
            qm.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []
            mock_gs.return_value = session

            resp = client.get("/partial/products")
            assert resp.status_code == 200
            assert "<html" not in resp.text

    @patch("includes.dashboard.routes._helpers.config")
    def test_partial_supplier_detail_not_found(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            session.query.return_value.filter.return_value.first.return_value = None
            mock_gs.return_value = session

            resp = client.get("/partial/suppliers/999")
            assert resp.status_code == 200
            assert "not found" in resp.text.lower()

    @patch("includes.dashboard.routes._helpers.config")
    def test_partial_rfqs_accessible_to_staff(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client, email="staff@eagle.com")
        with patch("includes.dashboard.routes.rfqs._get_store", return_value=None):
            resp = client.get("/partial/rfqs")
            assert resp.status_code == 200

    @patch("includes.dashboard.routes._helpers.config")
    def test_partial_users_admin_only(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client, email="staff@eagle.com")
        resp = client.get("/partial/users", follow_redirects=False)
        assert resp.status_code == 403


# ============================================================================
# RFQ ↔ Thread binding API
# ============================================================================

class TestRFQThreadAPI:
    """Tests for GET/POST /api/rfq-thread endpoints."""

    def test_get_rfq_thread_creates_new_when_none_exists(self, client):
        """When no binding exists, the route auto-creates a thread and returns its ID."""
        _login(client)
        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            # RFQThread query returns None (no existing binding)
            session.query.return_value.filter.return_value.first.return_value = None
            # Thread creation flow: user lookup + insert
            session.execute.return_value.scalar.return_value = "user-uuid-123"
            mock_gs.return_value = session

            resp = client.get("/api/rfq-thread?rfq_id=RFQ-2026-0001")
            assert resp.status_code == 200
            data = resp.json()
            # Route auto-creates a new thread — should return a UUID, not None
            assert data["thread_id"] is not None
            assert len(data["thread_id"]) > 0

    def test_get_rfq_thread_returns_existing_binding(self, client):
        """When a valid binding exists, the route returns the existing thread_id."""
        _login(client)
        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            row = MagicMock()
            row.thread_id = "abc-123"
            session.query.return_value.filter.return_value.first.return_value = row
            # Thread ownership check — confirm owner matches user
            session.execute.return_value.scalar.return_value = "admin@eagle.com"
            mock_gs.return_value = session

            resp = client.get("/api/rfq-thread?rfq_id=RFQ-2026-0001")
            assert resp.status_code == 200
            assert resp.json() == {"thread_id": "abc-123"}

    def test_post_rfq_thread_binds_new(self, client):
        _login(client)
        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            session.query.return_value.filter.return_value.first.return_value = None
            mock_gs.return_value = session

            resp = client.post("/api/rfq-thread", json={
                "rfq_id": "RFQ-2026-0001",
                "thread_id": "thread-new-123",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["rfq_id"] == "RFQ-2026-0001"
            assert data["thread_id"] == "thread-new-123"
            session.add.assert_called_once()
            session.commit.assert_called_once()

    def test_post_rfq_thread_rebinds_existing(self, client):
        _login(client)
        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            existing = MagicMock()
            existing.thread_id = "old-thread"
            existing.rfq_number = "RFQ-2026-0001"
            # First query: check if thread is already bound (returns None — not bound)
            # Second query: check if RFQ already has a binding (returns existing)
            session.query.return_value.filter.return_value.first.side_effect = [None, existing]
            mock_gs.return_value = session

            resp = client.post("/api/rfq-thread", json={
                "rfq_id": "RFQ-2026-0001",
                "thread_id": "new-thread-456",
            })
            assert resp.status_code == 200
            assert existing.thread_id == "new-thread-456"
            session.add.assert_not_called()
            session.commit.assert_called_once()

    def test_post_rfq_thread_rejects_thread_bound_to_other_rfq(self, client):
        """A thread already bound to a different RFQ must not be hijacked."""
        _login(client)
        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            other_binding = MagicMock()
            other_binding.rfq_number = "RFQ-2026-0099"
            # First query: thread is already bound to a different RFQ
            session.query.return_value.filter.return_value.first.return_value = other_binding
            mock_gs.return_value = session

            resp = client.post("/api/rfq-thread", json={
                "rfq_id": "RFQ-2026-0001",
                "thread_id": "already-bound-thread",
            })
            assert resp.status_code == 409
            assert resp.json()["error"] == "thread_already_bound"
            assert resp.json()["bound_to"] == "RFQ-2026-0099"
            session.commit.assert_not_called()

    def test_post_rfq_thread_requires_both_fields(self, client):
        _login(client)
        resp = client.post("/api/rfq-thread", json={"rfq_id": "RFQ-1"})
        assert resp.status_code == 400

        resp = client.post("/api/rfq-thread", json={"thread_id": "t-1"})
        assert resp.status_code == 400

    def test_get_rfq_thread_unauthenticated(self, client):
        resp = client.get("/api/rfq-thread?rfq_id=RFQ-1", follow_redirects=False)
        assert resp.status_code == 303

    def test_post_rfq_thread_unauthenticated(self, client):
        resp = client.post("/api/rfq-thread", json={
            "rfq_id": "RFQ-1", "thread_id": "t-1"
        }, follow_redirects=False)
        assert resp.status_code == 303


class TestLookupRFQThreadId:
    """Tests for the _lookup_rfq_thread_id helper."""

    def test_returns_thread_id_when_found(self):
        from includes.dashboard.routes import _lookup_rfq_thread_id
        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            row = MagicMock()
            row.thread_id = "thread-xyz"
            session.query.return_value.filter.return_value.first.return_value = row
            # Ownership check returns the same user
            session.execute.return_value.scalar.return_value = "user@eagle.com"
            mock_gs.return_value = session

            result = _lookup_rfq_thread_id("RFQ-2026-0001", "user@eagle.com")
            assert result == "thread-xyz"

    def test_returns_none_when_not_found(self):
        from includes.dashboard.routes import _lookup_rfq_thread_id
        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            session.query.return_value.filter.return_value.first.return_value = None
            # No binding → creates a new thread; mock execute for INSERT + user lookup
            session.execute.return_value.scalar.return_value = "user-uuid-123"
            mock_gs.return_value = session

            result = _lookup_rfq_thread_id("RFQ-2026-0001", "user@eagle.com")
            # Should return a new UUID (auto-created thread)
            assert result is not None
            assert len(result) == 36  # UUID format

    def test_returns_thread_id_even_without_steps(self):
        """Thread bound but no steps yet — should still return it (auto-bind creates before interaction)."""
        from includes.dashboard.routes import _lookup_rfq_thread_id
        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            row = MagicMock()
            row.thread_id = "new-thread"
            session.query.return_value.filter.return_value.first.return_value = row
            # Ownership check returns the same user
            session.execute.return_value.scalar.return_value = "user@eagle.com"
            mock_gs.return_value = session

            result = _lookup_rfq_thread_id("RFQ-2026-0001", "user@eagle.com")
            assert result == "new-thread"
            session.delete.assert_not_called()

    def test_stale_binding_with_missing_thread_is_repaired(self):
        """Binding exists but the thread row is gone — remove and recreate."""
        from includes.dashboard.routes import _lookup_rfq_thread_id
        with patch("includes.dashboard.routes._helpers.get_session") as mock_gs:
            session = MagicMock()
            row = MagicMock()
            row.thread_id = "ghost-thread"
            session.query.return_value.filter.return_value.first.return_value = row
            # Ownership check: thread row missing → None
            session.execute.return_value.scalar.return_value = None
            mock_gs.return_value = session

            result = _lookup_rfq_thread_id("RFQ-2026-0001", "user@eagle.com")
            # Stale binding removed, fresh thread created
            session.delete.assert_called_once_with(row)
            assert result is not None
            assert result != "ghost-thread"


# ============================================================================
# Admin supplier-dedup queue (A2)
# ============================================================================

class TestAdminDuplicatesQueue:
    """Candidate queue: list rendering, merge, reject, scan trigger."""

    def _candidate(self):
        from datetime import datetime, timezone
        cand = MagicMock()
        cand.id = uuid.uuid4()
        cand.status = "proposed"
        cand.confidence = 0.98
        cand.reasons = ["normalised_name_identical"]
        cand.primary_id = uuid.uuid4()
        cand.duplicate_id = uuid.uuid4()
        cand.created_at = datetime(2026, 8, 26, 4, 15, tzinfo=timezone.utc)
        cand.decided_by = None
        cand.decided_at = None
        return cand

    def _supplier(self, supplier_id):
        sup = MagicMock()
        sup.id = supplier_id
        sup.name = "Acme Pty Ltd"
        sup.netsuite_id = "NS-1"
        sup.url = "https://acme.com.au"
        sup.country = "AU"
        sup.source = "netsuite"
        return sup

    def _session(self, cands, suppliers=None):
        cand_q = MagicMock()
        cand_q.filter.return_value = cand_q
        cand_q.order_by.return_value = cand_q
        cand_q.all.return_value = cands

        sup_q = MagicMock()
        sup_q.filter.return_value = sup_q
        sup_q.all.return_value = suppliers or []

        session = MagicMock()
        session.query = MagicMock(side_effect=lambda model: cand_q if model.__name__ == "SupplierDuplicateCandidate" else sup_q)
        session.get.return_value = cands[0] if cands else None
        return session

    @patch("includes.dashboard.routes._helpers.config")
    def test_list_renders_candidates(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        mock_config.TIMEZONE = "Australia/Brisbane"
        _login(client)

        cand = self._candidate()
        session = self._session([cand], [self._supplier(cand.primary_id), self._supplier(cand.duplicate_id)])
        with patch("includes.dashboard.routes._helpers.get_session", return_value=session):
            resp = client.get("/partial/admin/duplicates/list?tier=all&page=1")

        assert resp.status_code == 200
        assert "Acme Pty Ltd" in resp.text
        assert "98%" in resp.text
        assert "certain" in resp.text

    @patch("includes.dashboard.routes._helpers.config")
    def test_merge_marks_candidate_merged(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        mock_config.TIMEZONE = "Australia/Brisbane"
        _login(client)

        cand = self._candidate()
        session = self._session([cand])
        result = MagicMock(use_instead_set=False, counts={"rfq_items": 2, "email_tracking": 1, "contacts": 0, "transactions": 0})
        with patch("includes.dashboard.routes._helpers.get_session", return_value=session), \
             patch("includes.dashboard.routes.admin.merge_suppliers", return_value=result):
            resp = client.post(
                f"/admin/duplicates/{cand.id}/merge",
                data={"merge_contacts": "1", "merge_domains": "1", "merge_names": "1", "page": "1", "tier": "all"},
            )

        assert resp.status_code == 200
        assert cand.status == "merged"
        assert cand.decided_by == "admin"
        # success renders the queue without a flash
        assert "Merged (" not in resp.text

    @patch("includes.dashboard.routes._helpers.config")
    def test_merge_applies_client_keep_flip(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        mock_config.TIMEZONE = "Australia/Brisbane"
        _login(client)

        cand = self._candidate()
        old_primary, old_duplicate = cand.primary_id, cand.duplicate_id
        primary = self._supplier(old_primary)      # netsuite
        duplicate = self._supplier(old_duplicate)  # netsuite
        session = self._session([cand])
        session.get.side_effect = lambda model, key: (
            cand if key == cand.id
            else primary if key == old_primary
            else duplicate
        )
        result = MagicMock(use_instead_set=False, counts={})
        with patch("includes.dashboard.routes._helpers.get_session", return_value=session), \
             patch("includes.dashboard.routes.admin.merge_suppliers", return_value=result) as mock_merge:
            resp = client.post(
                f"/admin/duplicates/{cand.id}/merge",
                data={"merge_contacts": "1", "merge_domains": "1", "merge_names": "1",
                      "page": "1", "tier": "all", "keep_first": "0"},
            )

        assert resp.status_code == 200
        assert cand.status == "merged"
        # merge_suppliers was called with the flipped direction
        _, call_primary, call_duplicate, _config = mock_merge.call_args.args
        assert call_primary == old_duplicate
        assert call_duplicate == old_primary

    @patch("includes.dashboard.routes._helpers.config")
    def test_merge_keep_flip_blocked_when_web_duplicate_is_netsuite(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        mock_config.TIMEZONE = "Australia/Brisbane"
        _login(client)

        cand = self._candidate()
        ns_id, web_id = cand.primary_id, cand.duplicate_id
        ns = self._supplier(ns_id)
        web = self._supplier(web_id)
        web.netsuite_id = None
        session = self._session([cand])
        session.get.side_effect = lambda model, key: (
            cand if key == cand.id
            else ns if key == ns_id
            else web
        )
        with patch("includes.dashboard.routes._helpers.get_session", return_value=session), \
             patch("includes.dashboard.routes.admin.merge_suppliers") as mock_merge:
            resp = client.post(
                f"/admin/duplicates/{cand.id}/merge",
                data={"merge_contacts": "1", "merge_domains": "1", "merge_names": "1",
                      "page": "1", "tier": "all", "keep_first": "0"},
            )

        assert resp.status_code == 200
        assert cand.status == "proposed"          # untouched
        mock_merge.assert_not_called()
        assert "Cannot keep the web supplier" in resp.text

    @patch("includes.dashboard.routes._helpers.config")
    def test_reject_marks_candidate_rejected(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        mock_config.TIMEZONE = "Australia/Brisbane"
        _login(client)

        cand = self._candidate()
        session = self._session([cand])
        with patch("includes.dashboard.routes._helpers.get_session", return_value=session):
            resp = client.post(
                f"/admin/duplicates/{cand.id}/reject",
                data={"page": "1", "tier": "all"},
            )

        assert resp.status_code == 200
        assert cand.status == "rejected"
        assert "Marked as not a duplicate." not in resp.text

    @patch("includes.dashboard.routes._helpers.config")
    def test_scan_triggers_background_job(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        job = MagicMock()
        job.id = "job-1234567890"
        mock_job_runner = MagicMock()
        mock_job_runner.run_script = AsyncMock(return_value=job)
        with patch("includes.graph.job_runner", mock_job_runner):
            resp = client.post("/admin/duplicates/scan")

        assert resp.status_code == 200
        assert "Scan started" in resp.text

    @patch("includes.dashboard.routes._helpers.config")
    def test_swap_flips_keep_direction(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        mock_config.TIMEZONE = "Australia/Brisbane"
        _login(client)

        cand = self._candidate()
        old_primary, old_duplicate = cand.primary_id, cand.duplicate_id
        primary = self._supplier(old_primary)      # netsuite
        duplicate = self._supplier(old_duplicate)  # netsuite
        session = self._session([cand])
        session.get.side_effect = lambda model, key: (
            cand if key == cand.id
            else primary if key == old_primary
            else duplicate
        )

        with patch("includes.dashboard.routes._helpers.get_session", return_value=session):
            resp = client.post(
                f"/admin/duplicates/{cand.id}/swap",
                data={"page": "1", "tier": "all"},
            )

        assert resp.status_code == 200
        assert cand.primary_id == old_duplicate
        assert cand.duplicate_id == old_primary
        assert "Swapped" in resp.text

    @patch("includes.dashboard.routes._helpers.config")
    def test_swap_blocked_when_web_would_keep_netsuite(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        mock_config.TIMEZONE = "Australia/Brisbane"
        _login(client)

        cand = self._candidate()
        ns_id, web_id = cand.primary_id, cand.duplicate_id
        ns = self._supplier(ns_id)          # netsuite_id set
        web = self._supplier(web_id)
        web.netsuite_id = None
        session = self._session([cand])
        session.get.side_effect = lambda model, key: (
            cand if key == cand.id
            else ns if key == ns_id
            else web
        )

        with patch("includes.dashboard.routes._helpers.get_session", return_value=session):
            resp = client.post(
                f"/admin/duplicates/{cand.id}/swap",
                data={"page": "1", "tier": "all"},
            )

        assert resp.status_code == 200
        assert cand.primary_id == ns_id          # unchanged
        assert cand.duplicate_id == web_id
        assert "Cannot keep the web supplier" in resp.text


class TestAdminDuplicatesManualNominate:
    """Manual nomination: source='manual' candidate rows via the supplier page."""

    def _supplier(self, supplier_id, netsuite_id="NS-1"):
        sup = MagicMock()
        sup.id = supplier_id
        sup.name = "Acme Pty Ltd"
        sup.netsuite_id = netsuite_id
        sup.url = "https://acme.com.au"
        sup.country = "AU"
        sup.source = "netsuite" if netsuite_id else "web"
        return sup

    def _nom_session(self, sup_a, sup_b, existing=None):
        q = MagicMock()
        q.filter.return_value = q
        q.first.return_value = existing
        session = MagicMock()
        session.query.return_value = q
        session.get.side_effect = lambda model, key: (
            sup_a if str(key) == str(sup_a.id)
            else sup_b if str(key) == str(sup_b.id)
            else None
        )
        return session

    @patch("includes.dashboard.routes._helpers.config")
    def test_creates_manual_proposed_row(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        a, b = self._supplier(uuid.uuid4()), self._supplier(uuid.uuid4())
        session = self._nom_session(a, b)
        with patch("includes.dashboard.routes._helpers.get_session", return_value=session):
            resp = client.post("/admin/duplicates/manual",
                               data={"a_id": str(a.id), "b_id": str(b.id)})

        assert resp.status_code == 200
        assert "Duplicate nominated" in resp.text
        assert "Review in the duplicates queue" in resp.text
        row = session.add.call_args[0][0]
        assert row.status == "proposed"
        assert row.source == "manual"
        assert row.confidence == 1.0
        assert row.reasons == ["manual"]

    @patch("includes.dashboard.routes._helpers.config")
    def test_netsuite_side_always_primary(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        ns = self._supplier(uuid.uuid4(), netsuite_id="NS-1")
        web = self._supplier(uuid.uuid4(), netsuite_id=None)
        session = self._nom_session(web, ns)   # web posted as side a
        with patch("includes.dashboard.routes._helpers.get_session", return_value=session):
            resp = client.post("/admin/duplicates/manual",
                               data={"a_id": str(web.id), "b_id": str(ns.id)})

        assert resp.status_code == 200
        row = session.add.call_args[0][0]
        assert row.primary_id == ns.id
        assert row.duplicate_id == web.id

    @patch("includes.dashboard.routes._helpers.config")
    def test_self_pair_rejected(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        a = self._supplier(uuid.uuid4())
        resp = client.post("/admin/duplicates/manual",
                           data={"a_id": str(a.id), "b_id": str(a.id)})

        assert resp.status_code == 200
        assert "cannot be a duplicate of itself" in resp.text

    @patch("includes.dashboard.routes._helpers.config")
    def test_reopens_decided_pair(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        a, b = self._supplier(uuid.uuid4()), self._supplier(uuid.uuid4())
        existing = MagicMock()
        existing.status = "rejected"
        session = self._nom_session(a, b, existing=existing)
        with patch("includes.dashboard.routes._helpers.get_session", return_value=session):
            resp = client.post("/admin/duplicates/manual",
                               data={"a_id": str(a.id), "b_id": str(b.id)})

        assert resp.status_code == 200
        assert existing.status == "proposed"
        assert existing.source == "manual"
        assert existing.decided_by is None
        session.add.assert_not_called()

    @patch("includes.dashboard.routes._helpers.config")
    def test_existing_proposed_pair_rejected(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        a, b = self._supplier(uuid.uuid4()), self._supplier(uuid.uuid4())
        existing = MagicMock()
        existing.status = "proposed"
        session = self._nom_session(a, b, existing=existing)
        with patch("includes.dashboard.routes._helpers.get_session", return_value=session):
            resp = client.post("/admin/duplicates/manual",
                               data={"a_id": str(a.id), "b_id": str(b.id)})

        assert resp.status_code == 200
        assert "already in the duplicates queue" in resp.text
        session.add.assert_not_called()


class TestSupplierSearchOptions:
    """Typeahead lookup backing the nomination picker."""

    @patch("includes.dashboard.routes._helpers.config")
    def test_renders_matching_suppliers(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        row = MagicMock()
        row.id = uuid.uuid4()
        row.name = "Acme Pty Ltd"
        row.netsuite_id = "NS-1"
        row.source = "netsuite"

        q = MagicMock()
        q.filter.return_value = q
        q.order_by.return_value = q
        q.limit.return_value = q
        q.all.return_value = [row]

        session = MagicMock()
        session.query.return_value = q
        with patch("includes.dashboard.routes._helpers.get_session", return_value=session):
            resp = client.get("/partial/suppliers/search?q=acme")

        assert resp.status_code == 200
        assert "Acme Pty Ltd" in resp.text
        assert "pick-supplier" in resp.text

    @patch("includes.dashboard.routes._helpers.config")
    def test_short_query_returns_nothing(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        resp = client.get("/partial/suppliers/search?q=a")
        assert resp.status_code == 200
        assert "Acme" not in resp.text

    @patch("includes.dashboard.routes._helpers.config")
    def test_exclude_filters_current_supplier(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        exclude_id = uuid.uuid4()
        excluded = MagicMock()
        excluded.id = exclude_id
        excluded.name = "Acme Excluded"
        excluded.netsuite_id = "NS-1"
        excluded.source = "netsuite"
        excluded.use_instead = None
        kept = MagicMock()
        kept.id = uuid.uuid4()
        kept.name = "Acme Kept"
        kept.netsuite_id = None
        kept.source = "web"
        kept.use_instead = None

        q = MagicMock()
        q.filter.return_value = q
        q.order_by.return_value = q
        q.limit.return_value = q
        q.all.return_value = [excluded, kept]

        session = MagicMock()
        session.query.return_value = q
        with patch("includes.dashboard.routes._helpers.get_session", return_value=session):
            resp = client.get(f"/partial/suppliers/search?q=acme&exclude={exclude_id}")

        assert resp.status_code == 200
        assert "Acme Kept" in resp.text
        assert "Acme Excluded" not in resp.text

    @patch("includes.dashboard.routes._helpers.config")
    def test_flags_duplicates_in_results(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        dup = MagicMock()
        dup.id = uuid.uuid4()
        dup.name = "Commander (Old)"
        dup.netsuite_id = None
        dup.source = "web"
        dup.use_instead = uuid.uuid4()

        q = MagicMock()
        q.filter.return_value = q
        q.order_by.return_value = q
        q.limit.return_value = q
        q.all.return_value = [dup]

        session = MagicMock()
        session.query.return_value = q
        with patch("includes.dashboard.routes._helpers.get_session", return_value=session):
            resp = client.get("/partial/suppliers/search?q=commander")

        assert resp.status_code == 200
        assert "Commander (Old)" in resp.text
        assert "duplicate" in resp.text


class TestAdminEmailLinkLookup:
    """Email-log linking lookups must hide flagged duplicates."""

    @patch("includes.dashboard.routes._helpers.config")
    def test_supplier_search_sql_hides_flagged(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        result_rows = MagicMock()
        result_rows.mappings.return_value.all.return_value = []
        session = MagicMock()
        session.execute.return_value = result_rows
        with patch("includes.dashboard.routes._helpers.get_session", return_value=session):
            resp = client.get("/api/admin/search-entities?type=supplier&q=commander")

        assert resp.status_code == 200
        assert resp.json() == {"results": []}
        sql = str(session.execute.call_args[0][0])
        assert "use_instead IS NULL" in sql

    @patch("includes.dashboard.routes._helpers.config")
    def test_short_query_returns_empty(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        resp = client.get("/api/admin/search-entities?type=supplier&q=c")
        assert resp.status_code == 200
        assert resp.json() == {"results": []}

    @patch("includes.dashboard.routes._helpers.config")
    def test_link_supplier_resolves_flagged_to_primary(self, mock_config, client):
        mock_config.get_admin_emails.return_value = ["admin@eagle.com"]
        _login(client)

        tracking = MagicMock()
        tracking.id = "email-1"
        tracking.gmail_thread_id = "thread-1"
        tracking.rfq_token = None
        tracking.rfq_id = None
        tracking.direction = "sent"

        supplier = MagicMock()
        supplier.name = "Primary Co"

        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = tracking
        session.query.return_value.filter.return_value.first.return_value = supplier

        primary = uuid.uuid4()
        with patch("includes.dashboard.routes._helpers.get_session", return_value=session), \
             patch("includes.dashboard.supplier_dedup.resolve_supplier_id", return_value=primary):
            resp = client.post("/api/admin/link-email", json={
                "email_id": "email-1",
                "link_type": "supplier",
                "entity_id": str(uuid.uuid4()),
                "save_domain": False,
            })

        assert resp.status_code == 200
        assert "Primary Co" in resp.text
        # The UPDATE must reference the resolved primary, not the flagged id
        params = session.execute.call_args[0][1]
        assert str(primary) in str(params)

