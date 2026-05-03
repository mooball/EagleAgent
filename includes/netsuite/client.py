"""
NetSuite REST API client wrapper.

Handles authorization headers, base URL construction, SuiteQL queries
with pagination, and record retrieval.
"""

import logging
from typing import Any

import requests

from config.settings import Config
from .auth import NetSuiteAuth

logger = logging.getLogger(__name__)

# Default timeout for API requests (seconds)
_DEFAULT_TIMEOUT = 60

# SuiteQL page size
_PAGE_LIMIT = 1000


class NetSuiteClient:
    """HTTP client for the NetSuite REST API."""

    def __init__(self, auth: NetSuiteAuth | None = None):
        self._auth = auth or NetSuiteAuth()
        account_id = self._auth.account_id
        self._base_url = f"https://{account_id}.suitetalk.api.netsuite.com/services/rest"

    # ── HTTP helpers ─────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._auth.get_token()}",
            "Accept": "application/json",
            "Prefer": "transient",
        }

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        """GET request against the REST API. `path` is relative to the base URL."""
        url = f"{self._base_url}/{path.lstrip('/')}"
        kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
        response = requests.get(url, headers=self._headers(), **kwargs)
        response.raise_for_status()
        return response

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        """POST request against the REST API."""
        url = f"{self._base_url}/{path.lstrip('/')}"
        kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
        response = requests.post(url, headers=self._headers(), **kwargs)
        if not response.ok:
            logger.error("POST %s → %s: %s", path, response.status_code, response.text[:500])
        response.raise_for_status()
        return response

    # ── SuiteQL ──────────────────────────────────────────────────

    def suiteql(self, query: str, limit: int = _PAGE_LIMIT) -> list[dict]:
        """
        Run a SuiteQL query and return all result rows, handling pagination.

        Args:
            query: The SuiteQL SELECT statement.
            limit: Page size per request (max 1000).

        Returns:
            List of row dicts from all pages.
        """
        all_items: list[dict] = []
        offset = 0

        while True:
            response = self.post(
                "query/v1/suiteql",
                json={"q": query},
                params={"limit": limit, "offset": offset},
            )
            data = response.json()
            items = data.get("items", [])
            all_items.extend(items)

            if not data.get("hasMore", False):
                break

            offset += limit
            logger.debug("SuiteQL pagination: fetched %d rows so far", len(all_items))

        logger.info("SuiteQL returned %d total rows", len(all_items))
        return all_items

    # ── Record access ────────────────────────────────────────────

    def get_record(self, record_type: str, record_id: str) -> dict:
        """
        Fetch a single record by type and internal ID.

        Args:
            record_type: e.g. "vendor", "customer", "purchaseOrder"
            record_id: NetSuite internal ID

        Returns:
            Record dict.
        """
        response = self.get(f"record/v1/{record_type}/{record_id}")
        return response.json()

    # ── Connection test ──────────────────────────────────────────

    def test_connection(self) -> dict:
        """
        Run a lightweight query to verify the connection works.

        Returns:
            Dict with 'ok' (bool), 'message' (str), and optionally 'vendor_count'.
        """
        try:
            self._auth.get_token()
            rows = self.suiteql("SELECT count(*) AS cnt FROM vendor")
            count = rows[0]["cnt"] if rows else "unknown"
            return {"ok": True, "message": f"Connected — {count} vendors in NetSuite"}
        except Exception as e:
            logger.exception("NetSuite connection test failed")
            return {"ok": False, "message": str(e)}
