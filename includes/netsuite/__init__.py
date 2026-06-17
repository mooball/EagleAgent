"""NetSuite ERP integration.

Provides OAuth 1.0 authentication and a REST API client for syncing
supplier, product, customer, contact, opportunity, and transaction data
from NetSuite into the EagleAgent database.

Exports:
    NetSuiteAuth - OAuth 1.0 token-based authentication
    NetSuiteClient - REST client with rate-limit handling and retries
"""

from .auth import NetSuiteAuth
from .client import NetSuiteClient

__all__ = ["NetSuiteAuth", "NetSuiteClient"]
