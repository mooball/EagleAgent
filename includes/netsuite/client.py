"""
NetSuite REST API client wrapper.

Handles authorization headers, base URL construction, SuiteQL queries
with pagination, and record retrieval.
"""

import logging
from collections.abc import Iterator
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config.settings import Config
from .auth import NetSuiteAuth, get_shared_auth

logger = logging.getLogger(__name__)

# Default timeout for API requests (seconds)
_DEFAULT_TIMEOUT = 60

# SuiteQL page size
_PAGE_LIMIT = 1000

# Retry configuration for transient failures
_RETRY_STRATEGY = Retry(
    total=3,
    backoff_factor=2,  # 2s, 4s, 8s
    status_forcelist=[502, 503, 504, 520, 521, 522],
    allowed_methods=["GET", "POST"],
    raise_on_status=False,
)


class NetSuiteClient:
    """HTTP client for the NetSuite REST API."""

    def __init__(self, auth: NetSuiteAuth | None = None):
        self._auth = auth or get_shared_auth()
        account_id = self._auth.account_id
        self._base_url = f"https://{account_id}.suitetalk.api.netsuite.com/services/rest"
        self._session = requests.Session()
        adapter = HTTPAdapter(max_retries=_RETRY_STRATEGY)
        self._session.mount("https://", adapter)

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
        response = self._session.get(url, headers=self._headers(), **kwargs)
        response.raise_for_status()
        return response

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        """POST request against the REST API."""
        url = f"{self._base_url}/{path.lstrip('/')}"
        kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
        response = self._session.post(url, headers=self._headers(), **kwargs)
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

    def suiteql_iter(self, query: str, limit: int = _PAGE_LIMIT) -> Iterator[list[dict]]:
        """
        Run a SuiteQL query and yield pages of results as they arrive.

        This avoids loading the entire result set into memory at once,
        making it suitable for large datasets (100K+ rows).

        Args:
            query: The SuiteQL SELECT statement.
            limit: Page size per request (max 1000).

        Yields:
            Lists of row dicts, one per API page.
        """
        offset = 0
        total = 0

        while True:
            response = self.post(
                "query/v1/suiteql",
                json={"q": query},
                params={"limit": limit, "offset": offset},
            )
            data = response.json()
            items = data.get("items", [])
            total += len(items)

            if items:
                yield items

            if not data.get("hasMore", False):
                break

            offset += limit
            logger.debug("SuiteQL pagination: fetched %d rows so far", total)

        logger.info("SuiteQL returned %d total rows (streamed)", total)

    # ── Record access ────────────────────────────────────────────

    def create_record(self, record_type: str, data: dict) -> str:
        """Create a record in NetSuite via REST API.

        Args:
            record_type: e.g. "opportunity", "customer", "contact"
            data: JSON body per NetSuite REST API schema

        Returns:
            The NetSuite internal ID of the created record.

        Raises:
            requests.HTTPError: on 4xx/5xx responses
        """
        url = f"{self._base_url}/record/v1/{record_type}"
        headers = self._headers()
        headers["Content-Type"] = "application/json"

        response = self._session.post(url, headers=headers, json=data, timeout=_DEFAULT_TIMEOUT)
        if not response.ok:
            logger.error(
                "CREATE %s → %s: %s", record_type, response.status_code, response.text[:500]
            )
            response.raise_for_status()

        # 204 No Content — ID is in the Location header
        location = response.headers.get("Location", "")
        netsuite_id = location.rstrip("/").split("/")[-1]
        logger.info("Created %s/%s", record_type, netsuite_id)
        return netsuite_id

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
