#!/usr/bin/env python3
"""
Generate employee mapping data for manual matching between NetSuite and local users.

This script:
1. Extracts all unique employees from NetSuite transactions
2. Fetches email, name, and active status from the employee entity table
3. Displays them in a format for manual email/name matching
4. Can export to CSV for spreadsheet matching

Usage:
  uv run python -m scripts.generate_employee_mapping_data
  uv run python -m scripts.generate_employee_mapping_data --export mapping.csv
  uv run python -m scripts.generate_employee_mapping_data --local-users
"""

import argparse
import csv
import json
import logging
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from config.settings import Config
from includes.netsuite.client import NetSuiteClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def get_db_engine():
    """Get database engine."""
    db_url = Config.DATABASE_URL
    if not db_url:
        raise ValueError("DATABASE_URL is empty. Check your `.env` settings.")

    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return create_engine(db_url)


def get_netsuite_employees(client: NetSuiteClient) -> list[dict]:
    """
    Extract all unique employees from NetSuite transactions.

    Note: NetSuite's SuiteQL and REST API do not expose the employee entity
    table directly. We can only get employee ID + display name from the
    transaction table. Emails must be maintained manually in
    create_netsuite_employee_mappings.py.

    Returns list of dicts with:
    - employee_id: NetSuite employee ID
    - employee_name: Display name from BUILTIN.DF
    """
    print("\n📥 Fetching employees from NetSuite transactions...")

    tx_query = (
        "SELECT DISTINCT "
        "employee, "
        "BUILTIN.DF(employee) AS employee_name "
        "FROM transaction "
        "WHERE employee IS NOT NULL "
        "ORDER BY employee"
    )

    try:
        response = client.post(
            "query/v1/suiteql",
            json={"q": tx_query},
            params={"limit": 1000, "offset": 0},
        )
        data = response.json()
        items = data.get("items", [])

        employees = []
        for item in items:
            employees.append({
                "employee_id": item.get("employee"),
                "employee_name": item.get("employee_name"),
            })

        print(f"✓ Found {len(employees)} unique NetSuite employees in transactions")
        return employees

    except Exception as e:
        logger.error(f"Error fetching employees from transactions: {e}")
        raise


def get_local_users(engine) -> list[dict]:
    """
    Get existing employee mappings from the local database for comparison.
    Uses netsuite_employee_mappings table (the canonical source).
    """
    print("\n📥 Fetching existing employee mappings from local database...")

    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT
                        id,
                        netsuite_employee_id,
                        name,
                        email,
                        is_active
                    FROM netsuite_employee_mappings
                    ORDER BY name
                """)
            )

            users = []
            for row in result:
                users.append({
                    "mapping_id": row[0],
                    "netsuite_employee_id": row[1],
                    "name": row[2],
                    "email": row[3],
                    "is_active": row[4],
                })

            print(f"✓ Found {len(users)} existing employee mappings")
            return users

    except Exception as e:
        logger.warning(f"Error fetching local mappings: {e}")
        return []


def display_netsuite_employees(employees: list[dict], local_users: list[dict] | None = None):
    """Display NetSuite employees, cross-referenced with existing local mappings."""
    # Build lookup of existing mappings by netsuite_employee_id
    existing: dict[str, dict] = {}
    if local_users:
        for u in local_users:
            ns_id = str(u.get("netsuite_employee_id", ""))
            if ns_id:
                existing[ns_id] = u

    print("\n" + "=" * 100)
    print("NETSUITE EMPLOYEES (from transactions)")
    print("=" * 100)

    mapped = 0
    unmapped = 0
    for emp in employees:
        emp_id = emp.get("employee_id", "N/A")
        emp_name = emp.get("employee_name", "Unknown")
        local = existing.get(str(emp_id))

        if local:
            mapped += 1
            status = "✓ mapped"
            email = local.get("email") or "(no email)"
            name = local.get("name", "")
            active = "active" if local.get("is_active") else "INACTIVE"
            print(f"  NS ID: {emp_id:8} | {status:10} | Name: {name:25} | Email: {email}  [{active}]")
        else:
            unmapped += 1
            status = "✗ UNMAPPED"
            print(f"  NS ID: {emp_id:8} | {status:10} | Tx Name: {emp_name}")

    print(f"\n  Mapped: {mapped}  |  Unmapped: {unmapped}  |  Total: {len(employees)}")
    print()


def display_local_users(users: list[dict]):
    """Display existing employee mappings from local database."""
    if not users:
        print("\n⚠️  No existing employee mappings found.")
        return

    print("\n" + "=" * 100)
    print("EXISTING EMPLOYEE MAPPINGS (local DB)")
    print("=" * 100)
    print()

    for u in users:
        ns_id = u.get("netsuite_employee_id", "N/A")
        name = u.get("name", "Unknown")
        email = u.get("email") or "(no email)"
        active = "✓" if u.get("is_active") else "✗"
        print(f"  NS ID: {ns_id:8} | Active: {active} | Name: {name:25} | Email: {email}")

    print()


def display_comparison(netsuite_employees: list[dict], local_users: list[dict]):
    """Display unmapped NetSuite employees that need manual mapping entries."""
    # Build lookup of existing mappings
    existing_ids: set[str] = set()
    for u in local_users:
        ns_id = str(u.get("netsuite_employee_id", ""))
        if ns_id:
            existing_ids.add(ns_id)

    unmapped = [e for e in netsuite_employees if str(e.get("employee_id")) not in existing_ids]

    if not unmapped:
        print("\n✓ All NetSuite employees have existing mappings.")
        return

    print("\n" + "=" * 100)
    print(f"UNMAPPED EMPLOYEES ({len(unmapped)} need entries)")
    print("=" * 100)
    print("\nAdd these to VERIFIED_MAPPINGS in create_netsuite_employee_mappings.py:\n")

    for emp in unmapped:
        emp_id = emp.get("employee_id")
        emp_name = emp.get("employee_name")
        print(f'  {{"netsuite_id": "{emp_id}", "name": "{emp_name}", "email": "FIXME@eagle-exports.com", "is_active": True}},')

    print()


def export_to_csv(netsuite_employees: list[dict], local_users: list[dict], filename: str):
    """Export data to CSV for spreadsheet matching."""
    print(f"\n📤 Exporting to {filename}...")
    
    try:
        with open(filename, "w", newline="") as f:
            writer = csv.writer(f)
            
            # Write header
            writer.writerow([
                "NetSuite Employee ID",
                "NetSuite Employee Name",
                "Has Local Mapping",
                "Local Name",
                "Local Email",
                "Is Active",
            ])

            # Build lookup of existing mappings
            existing: dict[str, dict] = {}
            for u in local_users:
                ns_id = str(u.get("netsuite_employee_id", ""))
                if ns_id:
                    existing[ns_id] = u

            for emp in netsuite_employees:
                emp_id = str(emp.get("employee_id"))
                local = existing.get(emp_id)
                if local:
                    writer.writerow([
                        emp_id,
                        emp.get("employee_name"),
                        "Yes",
                        local.get("name"),
                        local.get("email") or "",
                        "Yes" if local.get("is_active") else "No",
                    ])
                else:
                    writer.writerow([
                        emp_id,
                        emp.get("employee_name"),
                        "No — needs mapping",
                        "",
                        "",
                        "",
                    ])
        
        print(f"✓ Exported to {filename}")
    
    except Exception as e:
        logger.error(f"Error exporting to CSV: {e}")
        raise


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate employee mapping data for NetSuite ↔ Local Users"
    )
    parser.add_argument(
        "--export",
        type=str,
        help="Export to CSV file (e.g., mapping.csv)",
    )
    parser.add_argument(
        "--local-users",
        action="store_true",
        help="Show local users for reference",
    )
    parser.add_argument(
        "--comparison",
        action="store_true",
        help="Show side-by-side comparison for manual matching",
    )
    
    args = parser.parse_args()
    
    print("\n🔍 Employee Mapping Data Generator")
    print("=" * 80)
    
    try:
        # Get NetSuite employees
        client = NetSuiteClient()
        print(f"Connected to NetSuite account: {client._auth.account_id}")
        netsuite_employees = get_netsuite_employees(client)
        
        # Get local users (if DB connection available)
        engine = get_db_engine()
        local_users = get_local_users(engine)
        
        # Display results
        display_netsuite_employees(netsuite_employees, local_users)
        
        if args.local_users:
            display_local_users(local_users)
        
        if args.comparison or (args.local_users and local_users):
            display_comparison(netsuite_employees, local_users)
        
        # Export if requested
        if args.export:
            export_to_csv(netsuite_employees, local_users, args.export)
        
        print("\n" + "=" * 80)
        print("✅ Done!")
        print("\nNext steps:")
        print("  1. Review the employee lists above")
        print("  2. Match NetSuite employees to local users by email/name")
        print("  3. Create mappings in the netsuite_employee_mappings table")
        print("  4. Or use --export option to generate a CSV for spreadsheet matching")
        print("=" * 80 + "\n")
    
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
