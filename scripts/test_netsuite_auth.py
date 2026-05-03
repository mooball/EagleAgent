"""
Test script: Prove NetSuite OAuth2 client_credentials auth works.

Uses the includes/netsuite module to acquire a token and run a test query.

Usage:
    uv run python scripts/test_netsuite_auth.py
"""

import sys

# Add project root to path so we can import config/includes
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from config.settings import Config
from includes.netsuite import NetSuiteAuth, NetSuiteClient


if __name__ == "__main__":
    print("=" * 50)
    print("NetSuite OAuth2 Authentication Test")
    print("=" * 50)
    print(f"Account: {Config.NETSUITE_ACCOUNT_ID}")
    print(f"Client ID: {Config.NETSUITE_CLIENT_ID[:12]}...")
    print(f"Key loaded from: NETSUITE_PRIVATE_KEY_B64 env var")
    print()

    # Test auth
    auth = NetSuiteAuth()
    token = auth.get_token()
    print(f"Token acquired: {token[:10]}...{token[-10:]}")
    print()

    # Test client
    client = NetSuiteClient(auth)
    result = client.test_connection()
    print(f"Connection test: {result['message']}")
    print()

    # Test a sample query
    print("Sample query: 5 customers")
    rows = client.suiteql("SELECT id, companyName FROM customer WHERE rownum <= 5")
    for row in rows:
        print(f"  {row.get('id')}: {row.get('companyname')}")

    print("\nDone.")
