#!/usr/bin/env python3
"""Quick exploration of NetSuite Departments API.

Tests both SuiteQL and REST API to find the correct way to query
the department list from NetSuite.

Usage:
    uv run python -m scripts.explore_netsuite_departments
"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from includes.netsuite.client import NetSuiteClient


def try_suiteql(client: NetSuiteClient, table_name: str):
    """Try a SuiteQL query against a table name."""
    query = f"SELECT * FROM {table_name} ORDER BY id"
    print(f"\n── SuiteQL: {table_name} ──")
    try:
        resp = client.post(
            "query/v1/suiteql",
            json={"q": query},
            params={"limit": 50, "offset": 0},
        )
        data = resp.json()
        items = data.get("items", [])
        if items:
            print(f"✓ SUCCESS — {len(items)} records")
            print(f"  Fields: {list(items[0].keys())}")
            print(f"\n  First 3 records:")
            for item in items[:3]:
                print(f"  {json.dumps(item, default=str)}")
        else:
            print("  Query succeeded but returned 0 records")
    except Exception as e:
        err = str(e)[:300]
        print(f"✗ FAILED: {err}")


def try_rest(client: NetSuiteClient, record_type: str):
    """Try a REST API GET against a record type."""
    print(f"\n── REST API: /records/v1/{record_type} ──")
    try:
        resp = client.get(f"/records/v1/{record_type}", params={"limit": 5})
        data = resp.json()
        items = data.get("items", [])
        if items:
            print(f"✓ SUCCESS — {len(items)} records")
            print(f"  Fields: {list(items[0].keys())}")
            print(f"\n  First 3 records:")
            for item in items[:3]:
                print(f"  {json.dumps(item, default=str)[:500]}")
        else:
            print("  Request succeeded but returned 0 items")
            print(f"  Response keys: {list(data.keys())}")
    except Exception as e:
        err = str(e)[:300]
        print(f"✗ FAILED: {err}")


def try_suiteql_alt_names(client: NetSuiteClient):
    """Try alternative SuiteQL table names."""
    alt_names = [
        "departments",
        "classification",
        "class",
        "segment",
        "customlist",
        "customrecord",
    ]
    for name in alt_names:
        try_suiteql(client, name)


def main():
    print("=" * 60)
    print("NetSuite Departments API Exploration")
    print("=" * 60)

    client = NetSuiteClient()
    print(f"Account: {client._auth.account_id}")

    # 1. Try SuiteQL with 'department'
    try_suiteql(client, "department")

    # 2. Try REST API with 'department'
    try_rest(client, "department")

    # 3. Try alternative SuiteQL table names
    print("\n\n── Trying alternative SuiteQL table names ──")
    try_suiteql_alt_names(client)

    # 4. Try REST API alternatives
    print("\n── Trying alternative REST API record types ──")
    for name in ["departments", "classification", "class"]:
        try_rest(client, name)

    print("\n" + "=" * 60)
    print("Done.")
    print("=" * 60)


if __name__ == "__main__":
    main()
