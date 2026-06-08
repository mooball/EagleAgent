#!/usr/bin/env python3
"""
Explore NetSuite tables to discover available fields and sample data.

This script connects to NetSuite and queries the Opportunities, Customers, and
Employees tables to help understand what fields are available for local syncing.

Usage:
  uv run python -m scripts.explore_netsuite_tables
  uv run python -m scripts.explore_netsuite_tables --limit 1
"""

import json
import logging
from datetime import datetime, timedelta

from includes.netsuite.client import NetSuiteClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def date_to_netsuite(date_obj: datetime) -> str:
    """Convert datetime to NetSuite date format (d/m/yyyy)."""
    return f"{date_obj.day}/{date_obj.month}/{date_obj.year}"


def explore_opportunities(client: NetSuiteClient, limit: int = 10):
    """Explore the Opportunity table to see available fields and sample data."""
    print("\n" + "=" * 80)
    print("OPPORTUNITIES TABLE")
    print("=" * 80)

    # Try different possible table names for Opportunities
    table_names = ["opportunity", "opportunities", "transaction", "estimate"]
    
    for table in table_names:
        # Start with a basic query to see what fields are available
        # We'll get a small sample first
        query = f"""
            SELECT id
            FROM {table} 
            LIMIT 1
        """

        try:
            results = client.suiteql(query)
            print(f"\n✓ Found table '{table}'")
            
            # Now try to get more fields
            query = f"""
                SELECT *
                FROM {table}
                WHERE isinactive = 'F'
                ORDER BY lastmodified DESC
                LIMIT {limit}
            """
            results = client.suiteql(query)
            if results:
                print(f"\nFound {len(results)} records:")
                print(json.dumps(results[0], indent=2))
                print(f"\nAvailable fields: {list(results[0].keys())}")
            break
        except Exception as e:
            if "not found" in str(e).lower() or "unknown" in str(e).lower():
                continue
            else:
                # This is a different error, log it
                logger.debug(f"Error with table '{table}': {str(e)[:100]}")


def explore_customers(client: NetSuiteClient, limit: int = 10):
    """Explore the Customer table."""
    print("\n" + "=" * 80)
    print("CUSTOMERS TABLE")
    print("=" * 80)

    query = f"""
        SELECT * 
        FROM customer
        WHERE isinactive = 'F'
        ORDER BY lastmodified DESC
        LIMIT {limit}
    """

    try:
        results = client.suiteql(query)
        if results:
            print(f"\nFound {len(results)} customers:")
            print(json.dumps(results[0], indent=2))
            print(f"\nAvailable fields: {list(results[0].keys())}")
        else:
            print("No customers found.")
    except Exception as e:
        logger.error(f"Error querying customers: {e}")
        print(f"Error: {e}")


def explore_employees(client: NetSuiteClient, limit: int = 10):
    """Explore the Employee table."""
    print("\n" + "=" * 80)
    print("EMPLOYEES TABLE")
    print("=" * 80)

    query = f"""
        SELECT * 
        FROM employee
        WHERE isinactive = 'F'
        ORDER BY lastmodified DESC
        LIMIT {limit}
    """

    try:
        results = client.suiteql(query)
        if results:
            print(f"\nFound {len(results)} employees:")
            print(json.dumps(results[0], indent=2))
            print(f"\nAvailable fields: {list(results[0].keys())}")
        else:
            print("No employees found.")
    except Exception as e:
        logger.error(f"Error querying employees: {e}")
        print(f"Error: {e}")


def main():
    """Main entry point."""
    print("\n🔍 NetSuite Table Reconnaissance")
    print("=" * 80)

    try:
        client = NetSuiteClient()
        print(f"Connected to NetSuite account: {client._auth.account_id}")

        explore_opportunities(client)
        explore_customers(client)
        explore_employees(client)

        print("\n" + "=" * 80)
        print("✅ Reconnaissance complete!")
        print("=" * 80 + "\n")

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
