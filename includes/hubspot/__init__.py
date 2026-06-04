"""HubSpot integration client.

Provides a thin wrapper around the hubspot-api-client SDK
using a Service Key access token for authentication.
"""

import logging

import httpx
from hubspot import HubSpot
from hubspot.crm.deals import SimplePublicObjectInput, ApiException as DealsApiException
from hubspot.crm.contacts import ApiException as ContactsApiException

from config.settings import Config

logger = logging.getLogger(__name__)

_client: HubSpot | None = None


def get_client() -> HubSpot:
    """Get or create the singleton HubSpot client."""
    global _client
    if _client is None:
        token = Config.HUBSPOT_ACCESS_TOKEN
        if not token:
            raise RuntimeError("HUBSPOT_ACCESS_TOKEN is not set. Configure it in .env or environment.")
        _client = HubSpot(access_token=token)
    return _client


def test_connection() -> dict:
    """Test connectivity by making a simple CRM API call.

    Returns dict with:
        - status: "ok" | "error"
        - message: human-readable summary
        - details: raw response data (on success)
    """
    try:
        client = get_client()
        # Verify token by fetching account info via REST
        resp = httpx.get(
            "https://api.hubapi.com/account-info/v3/details",
            headers={"Authorization": f"Bearer {Config.HUBSPOT_ACCESS_TOKEN}"},
        )
        resp.raise_for_status()
        info = resp.json()
        return {
            "status": "ok",
            "message": f"Connected to HubSpot (portal: {info.get('portalId')}, company: {info.get('companyCurrency', 'N/A')})",
            "details": info,
        }
    except Exception as e:
        logger.error(f"HubSpot connection test failed: {e}")
        return {
            "status": "error",
            "message": f"Connection failed: {e}",
            "details": None,
        }
