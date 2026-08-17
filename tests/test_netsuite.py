"""Tests for includes/netsuite/ — constants, queries, auth, and client."""

import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from includes.netsuite.constants import (
    SALES_ORDER_STATUS,
    QUOTE_STATUS,
    get_status_label,
)
from includes.netsuite.queries import (
    suppliers_updated_since,
    all_brands,
    products_updated_since,
    sales_orders_updated_since,
    quotes_updated_since,
)


# ── Constants ────────────────────────────────────────────────────


class TestStatusConstants:
    def test_sales_order_known_code(self):
        assert get_status_label("SalesOrder", "B") == "Pending Fulfillment"

    def test_sales_order_unknown_code(self):
        assert get_status_label("SalesOrder", "Z") == "Z"

    def test_quote_known_code(self):
        assert get_status_label("Quote", "A") == "Open"

    def test_unknown_doc_type_returns_raw_code(self):
        assert get_status_label("PurchaseOrder", "X") == "X"

    def test_all_sales_order_codes_mapped(self):
        for code, label in SALES_ORDER_STATUS.items():
            assert get_status_label("SalesOrder", code) == label

    def test_all_quote_codes_mapped(self):
        for code, label in QUOTE_STATUS.items():
            assert get_status_label("Quote", code) == label


# ── Queries ──────────────────────────────────────────────────────


class TestQueries:
    """Verify SuiteQL query builders produce valid SQL with correct date formatting."""

    def test_suppliers_updated_since_date_format(self):
        sql = suppliers_updated_since("2026-03-15")
        # NetSuite format: d/m/yyyy (no zero-padding)
        assert "15/3/2026" in sql
        assert "FROM vendor" in sql

    def test_all_brands_without_date(self):
        sql = all_brands()
        assert "customrecord_brands" in sql
        assert "lastmodified >=" not in sql

    def test_all_brands_with_date(self):
        sql = all_brands("2026-01-10")
        assert "10/1/2026" in sql
        assert "lastmodified >=" in sql

    def test_products_updated_since(self):
        sql = products_updated_since("2026-06-01")
        assert "1/6/2026" in sql
        assert "InvtPart" in sql

    def test_sales_orders_updated_since(self):
        sql = sales_orders_updated_since("2026-02-28")
        assert "28/2/2026" in sql
        assert "SalesOrd" in sql

    def test_quotes_updated_since(self):
        sql = quotes_updated_since("2026-12-25")
        assert "25/12/2026" in sql
        assert "Estimate" in sql
        # Processed + Voided quotes should be excluded
        assert "NOT IN ('V', 'B')" in sql

    def test_all_queries_order_by_lastmodified(self):
        """All paginated queries must be sorted for resume support."""
        for fn in (suppliers_updated_since, products_updated_since,
                   sales_orders_updated_since, quotes_updated_since):
            sql = fn("2026-01-01")
            assert "ORDER BY" in sql


# ── Auth ─────────────────────────────────────────────────────────


class TestNetSuiteAuth:
    """Test token caching and refresh logic (no real HTTP calls)."""

    def _make_auth(self):
        """Create a NetSuiteAuth with dummy credentials."""
        import base64
        # Need a real RSA key for PyJWT PS256 signing — generate a minimal one
        from includes.netsuite.auth import NetSuiteAuth

        # Use a pre-generated test RSA key (2048-bit) in PEM format
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        key_b64 = base64.b64encode(pem).decode()

        return NetSuiteAuth(
            account_id="1234567",
            client_id="test-client-id",
            certificate_id="test-cert-id",
            private_key_b64=key_b64,
        )

    @patch("includes.netsuite.auth.Config")
    def test_missing_key_raises(self, mock_cfg):
        mock_cfg.NETSUITE_PRIVATE_KEY_B64 = ""
        mock_cfg.NETSUITE_CLIENT_ID = "x"
        mock_cfg.NETSUITE_CERTIFICATE_ID = "x"
        from includes.netsuite.auth import NetSuiteAuth
        with pytest.raises(ValueError, match="PRIVATE_KEY"):
            NetSuiteAuth(
                account_id="x", client_id="x",
                certificate_id="x", private_key_b64="",
            )

    @patch("includes.netsuite.auth.Config")
    def test_missing_client_id_raises(self, mock_cfg):
        mock_cfg.NETSUITE_CLIENT_ID = ""
        mock_cfg.NETSUITE_CERTIFICATE_ID = "x"
        mock_cfg.NETSUITE_PRIVATE_KEY_B64 = "dGVzdA=="
        from includes.netsuite.auth import NetSuiteAuth
        with pytest.raises(ValueError, match="CLIENT_ID"):
            NetSuiteAuth(
                account_id="x", client_id="",
                certificate_id="x", private_key_b64="dGVzdA==",
            )

    @patch("includes.netsuite.auth.requests.post")
    def test_get_token_caches(self, mock_post):
        auth = self._make_auth()
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"access_token": "tok-123", "expires_in": 3600},
        )

        t1 = auth.get_token()
        t2 = auth.get_token()  # should use cache
        assert t1 == "tok-123"
        assert t2 == "tok-123"
        assert mock_post.call_count == 1  # only one HTTP call

    @patch("includes.netsuite.auth.requests.post")
    def test_token_refresh_on_expiry(self, mock_post):
        auth = self._make_auth()
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"access_token": "tok-old", "expires_in": 3600},
        )

        auth.get_token()
        # Simulate token expiry
        auth._expires_at = time.time() - 1

        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"access_token": "tok-new", "expires_in": 3600},
        )
        t = auth.get_token()
        assert t == "tok-new"
        assert mock_post.call_count == 2

    @patch("includes.netsuite.auth.requests.post")
    def test_failed_token_request_raises(self, mock_post):
        auth = self._make_auth()
        mock_post.return_value = MagicMock(
            status_code=401, text="Unauthorized",
        )
        with pytest.raises(RuntimeError, match="401"):
            auth.get_token()


# ── Client ───────────────────────────────────────────────────────


class TestNetSuiteClient:
    """Test HTTP wrapper logic (mocked transport)."""

    def _make_client(self):
        from includes.netsuite.client import NetSuiteClient
        mock_auth = MagicMock()
        mock_auth.account_id = "1234567"
        mock_auth.get_token.return_value = "test-token"
        return NetSuiteClient(auth=mock_auth)

    @patch("includes.netsuite.client.requests.Session")
    def test_suiteql_single_page(self, _mock_session_cls):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "items": [{"id": "1"}, {"id": "2"}],
            "hasMore": False,
        }
        mock_resp.raise_for_status = MagicMock()
        client._session.post = MagicMock(return_value=mock_resp)

        rows = client.suiteql("SELECT id FROM vendor")
        assert len(rows) == 2

    @patch("includes.netsuite.client.requests.Session")
    def test_suiteql_pagination(self, _mock_session_cls):
        client = self._make_client()

        page1 = MagicMock()
        page1.ok = True
        page1.json.return_value = {"items": [{"id": "1"}], "hasMore": True}
        page1.raise_for_status = MagicMock()

        page2 = MagicMock()
        page2.ok = True
        page2.json.return_value = {"items": [{"id": "2"}], "hasMore": False}
        page2.raise_for_status = MagicMock()

        client._session.post = MagicMock(side_effect=[page1, page2])

        rows = client.suiteql("SELECT id FROM vendor")
        assert len(rows) == 2
        assert client._session.post.call_count == 2

    @patch("includes.netsuite.client.requests.Session")
    def test_suiteql_iter_yields_pages(self, _mock_session_cls):
        client = self._make_client()

        page1 = MagicMock()
        page1.ok = True
        page1.json.return_value = {"items": [{"id": "1"}], "hasMore": True}
        page1.raise_for_status = MagicMock()

        page2 = MagicMock()
        page2.ok = True
        page2.json.return_value = {"items": [{"id": "2"}], "hasMore": False}
        page2.raise_for_status = MagicMock()

        client._session.post = MagicMock(side_effect=[page1, page2])

        pages = list(client.suiteql_iter("SELECT id FROM vendor"))
        assert len(pages) == 2
        assert pages[0] == [{"id": "1"}]

    @patch("includes.netsuite.client.requests.Session")
    def test_get_record(self, _mock_session_cls):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "42", "entityid": "ACME"}
        mock_resp.raise_for_status = MagicMock()
        client._session.get = MagicMock(return_value=mock_resp)

        record = client.get_record("vendor", "42")
        assert record["id"] == "42"

    @patch("includes.netsuite.client.requests.Session")
    def test_test_connection_success(self, _mock_session_cls):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"items": [{"cnt": 500}], "hasMore": False}
        mock_resp.raise_for_status = MagicMock()
        client._session.post = MagicMock(return_value=mock_resp)

        result = client.test_connection()
        assert result["ok"] is True
        assert "500" in result["message"]

    @patch("includes.netsuite.client.requests.Session")
    def test_test_connection_failure(self, _mock_session_cls):
        client = self._make_client()
        client._session.post = MagicMock(side_effect=RuntimeError("connection refused"))

        result = client.test_connection()
        assert result["ok"] is False
