"""
NetSuite OAuth2 token acquisition and caching.

Uses the client_credentials grant with PS256-signed JWTs.
Tokens are cached in memory and auto-refreshed 5 minutes before expiry.
"""

import base64
import logging
import time

import jwt
import requests

from config.settings import Config

logger = logging.getLogger(__name__)

# Refresh token 5 minutes before actual expiry
_REFRESH_BUFFER_SECONDS = 300


class NetSuiteAuth:
    """Manages OAuth2 bearer tokens for NetSuite API access."""

    def __init__(
        self,
        account_id: str | None = None,
        client_id: str | None = None,
        certificate_id: str | None = None,
        private_key_b64: str | None = None,
    ):
        self.account_id = account_id or Config.NETSUITE_ACCOUNT_ID
        self.client_id = client_id or Config.NETSUITE_CLIENT_ID
        self.certificate_id = certificate_id or Config.NETSUITE_CERTIFICATE_ID

        key_b64 = private_key_b64 or Config.NETSUITE_PRIVATE_KEY_B64
        if not key_b64:
            raise ValueError("NETSUITE_PRIVATE_KEY_B64 is not configured")
        if not self.client_id:
            raise ValueError("NETSUITE_CLIENT_ID is not configured")
        if not self.certificate_id:
            raise ValueError("NETSUITE_CERTIFICATE_ID is not configured")

        self._private_key = base64.b64decode(key_b64)
        self._token_url = (
            f"https://{self.account_id}.suitetalk.api.netsuite.com"
            f"/services/rest/auth/oauth2/v1/token"
        )

        # Cached token state
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    def get_token(self) -> str:
        """Return a valid bearer token, refreshing if necessary."""
        if self._access_token and time.time() < (self._expires_at - _REFRESH_BUFFER_SECONDS):
            return self._access_token

        self._refresh_token()
        return self._access_token  # type: ignore[return-value]

    def _refresh_token(self) -> None:
        """Sign a JWT and exchange it for a new bearer token."""
        now = int(time.time())

        claims = {
            "iss": self.client_id,
            "scope": ["restlets", "rest_webservices"],
            "aud": self._token_url,
            "iat": now,
            "exp": now + 3600,
        }

        signed_jwt = jwt.encode(
            claims,
            self._private_key,
            algorithm="PS256",
            headers={"kid": self.certificate_id, "typ": "JWT"},
        )

        response = requests.post(
            self._token_url,
            data={
                "grant_type": "client_credentials",
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": signed_jwt,
            },
            timeout=30,
        )

        if response.status_code != 200:
            logger.error("NetSuite token request failed: %s %s", response.status_code, response.text)
            raise RuntimeError(f"NetSuite token request failed ({response.status_code}): {response.text}")

        data = response.json()
        self._access_token = data["access_token"]
        self._expires_at = now + data.get("expires_in", 3600)
        logger.info("NetSuite token acquired (expires in %ss)", data.get("expires_in"))
