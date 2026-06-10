#!/usr/bin/env python3
"""
Explore NetSuite tables using REST API record endpoints.

This gets actual records to discover what fields are available.
"""

import json
import logging

from includes.netsuite.client import NetSuiteClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def explore_records(client: NetSuiteClient):
    """Explore available records using REST API."""
    
    print("\n" + "=" * 80)
    print("EXPLORING NETSUITE RECORDS VIA REST API")
    print("=" * 80)
    
    # Try to get some records directly via REST API
    # First, let's try opportunities
    print("\n--- OPPORTUNITIES ---")
    try:
        response = client.get("/records/v1/opportunity?limit=1")
        data = response.json()
        if "items" in data and data["items"]:
            record = data["items"][0]
            print(f"✓ Found opportunity records")
            print(f"Fields available: {list(record.keys())}")
            print(json.dumps(record, indent=2)[:500])
    except Exception as e:
        print(f"✗ Error: {e}")
    
    # Try customers
    print("\n--- CUSTOMERS ---")
    try:
        response = client.get("/records/v1/customer?limit=1")
        data = response.json()
        if "items" in data and data["items"]:
            record = data["items"][0]
            print(f"✓ Found customer records")
            print(f"Fields available: {list(record.keys())}")
            print(json.dumps(record, indent=2)[:500])
    except Exception as e:
        print(f"✗ Error: {e}")
    
    # Try employees
    print("\n--- EMPLOYEES ---")
    try:
        response = client.get("/records/v1/employee?limit=1")
        data = response.json()
        if "items" in data and data["items"]:
            record = data["items"][0]
            print(f"✓ Found employee records")
            print(f"Fields available: {list(record.keys())}")
            print(json.dumps(record, indent=2)[:500])
    except Exception as e:
        print(f"✗ Error: {e}")


def explore_suiteql(client: NetSuiteClient):
    """Try simple SuiteQL queries without formatting issues."""
    
    print("\n" + "=" * 80)
    print("EXPLORING NETSUITE VIA SUITEQL")
    print("=" * 80)
    
    # Simple one-line queries (no LIMIT - pagination is handled by client)
    queries = [
        ("opportunity", "SELECT * FROM opportunity ORDER BY id"),
        ("customer", "SELECT * FROM customer WHERE isinactive = 'F' ORDER BY id"),
        ("employee", "SELECT * FROM employee ORDER BY id"),
        # Try alternative names
        ("contact", "SELECT * FROM contact WHERE isinactive = 'F' ORDER BY id"),
        ("employee_vendor", "SELECT * FROM vendor WHERE custvendor_type = 'EMPLOYEE' ORDER BY id"),
    ]
    
    for table_name, query in queries:
        print(f"\n--- {table_name.upper()} ---")
        try:
            # Use limit=1 in the client call, not in SQL
            response = client.post(
                "query/v1/suiteql",
                json={"q": query},
                params={"limit": 5, "offset": 0},
            )
            data = response.json()
            results = data.get("items", [])
            
            if results:
                record = results[0]
                print(f"✓ Found {len(results)} {table_name} records")
                print(f"Fields: {list(record.keys())}")
                print("\nFirst record sample:")
                print(json.dumps(record, indent=2)[:1000])
            else:
                print(f"No {table_name} records found")
        except Exception as e:
            error_msg = str(e)[:300]
            print(f"✗ Error: {error_msg}")


def explore_employees_from_transactions(client: NetSuiteClient):
    """Find unique employees referenced in transactions to understand employee data."""
    print("\n" + "=" * 80)
    print("EMPLOYEES (from transaction references)")
    print("=" * 80)
    
    print("\n--- Getting unique employees from transaction table ---")
    try:
        query = "SELECT DISTINCT BUILTIN.DF(employee) AS employee_name, employee FROM transaction WHERE employee IS NOT NULL ORDER BY employee"
        response = client.post(
            "query/v1/suiteql",
            json={"q": query},
            params={"limit": 20, "offset": 0},
        )
        data = response.json()
        results = data.get("items", [])
        
        if results:
            print(f"✓ Found {len(results)} unique employees:")
            for emp in results[:5]:
                print(f"  - {emp.get('employee_name')} (ID: {emp.get('employee')})")
        else:
            print("No employees found")
    except Exception as e:
        print(f"✗ Error: {str(e)[:300]}")


def main():
    """Main entry point."""
    print("\n🔍 NetSuite Reconnaissance")
    
    try:
        client = NetSuiteClient()
        print(f"Connected to NetSuite account: {client._auth.account_id}")
        
        explore_suiteql(client)
        explore_employees_from_transactions(client)
        
        print("\n" + "=" * 80)
        print("✅ Done!")
        print("=" * 80 + "\n")
        
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
