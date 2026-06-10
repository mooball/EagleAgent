#!/usr/bin/env python3
"""
Generate employee mapping data for manual matching between NetSuite and local users.

This script:
1. Extracts all unique employees from NetSuite transactions
2. Displays them in a format for manual email/name matching
3. Can export to CSV for spreadsheet matching

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
    
    Returns list of dicts with:
    - employee_id: NetSuite employee ID
    - employee_name: Display name from BUILTIN.DF
    """
    print("\n📥 Fetching employees from NetSuite transactions...")
    
    query = (
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
            json={"q": query},
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
        
        print(f"✓ Found {len(employees)} unique NetSuite employees")
        return employees
    
    except Exception as e:
        logger.error(f"Error fetching employees: {e}")
        raise


def get_local_users(engine) -> list[dict]:
    """
    Extract all users from local database.
    
    Assumes a 'users' table with email and full_name fields.
    Adjust query based on your actual schema.
    """
    print("\n📥 Fetching users from local database...")
    
    try:
        with engine.connect() as conn:
            # Query the users table - adjust column names as needed
            result = conn.execute(
                text("""
                    SELECT 
                        id,
                        email,
                        full_name
                    FROM users
                    WHERE email IS NOT NULL
                    ORDER BY full_name
                """)
            )
            
            users = []
            for row in result:
                users.append({
                    "user_id": row[0],
                    "email": row[1],
                    "full_name": row[2],
                })
            
            print(f"✓ Found {len(users)} local users")
            return users
    
    except Exception as e:
        logger.warning(f"Error fetching local users (table may not exist yet): {e}")
        return []


def display_netsuite_employees(employees: list[dict]):
    """Display NetSuite employees for manual review."""
    print("\n" + "=" * 80)
    print("NETSUITE EMPLOYEES (from transactions)")
    print("=" * 80)
    print()
    
    for emp in employees:
        emp_id = emp.get("employee_id", "N/A")
        emp_name = emp.get("employee_name", "Unknown")
        print(f"  ID: {emp_id:8} | Name: {emp_name}")
    
    print()


def display_local_users(users: list[dict]):
    """Display local users for manual review."""
    if not users:
        print("\n⚠️  No local users found. Check that 'users' table exists.")
        return
    
    print("\n" + "=" * 80)
    print("LOCAL USERS (for matching)")
    print("=" * 80)
    print()
    
    for user in users:
        user_id = user.get("user_id", "N/A")
        email = user.get("email", "N/A")
        full_name = user.get("full_name", "Unknown")
        print(f"  ID: {user_id:4} | Email: {email:30} | Name: {full_name}")
    
    print()


def display_comparison(netsuite_employees: list[dict], local_users: list[dict]):
    """Display side-by-side comparison for manual matching."""
    print("\n" + "=" * 80)
    print("MATCHING GUIDE (NetSuite → Local Users)")
    print("=" * 80)
    print("\nInstructions:")
    print("  1. Look at each NetSuite employee")
    print("  2. Find the matching local user by email or name")
    print("  3. Create a mapping: <NetSuite ID> → <Local User ID>")
    print("\n" + "-" * 80)
    
    for emp in netsuite_employees:
        emp_id = emp.get("employee_id")
        emp_name = emp.get("employee_name")
        
        print(f"\nNetSuite Employee:")
        print(f"  ID: {emp_id}")
        print(f"  Name: {emp_name}")
        print(f"  → Find matching user from list above")
        print(f"  → Store mapping: netsuite_employee_id={emp_id} → local_user_id=<YOUR_ID>")


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
                "Local User ID (to fill)",
                "Local User Email (reference)",
                "Local User Name (reference)",
            ])
            
            # Write NetSuite employees with blank local fields
            for emp in netsuite_employees:
                writer.writerow([
                    emp.get("employee_id"),
                    emp.get("employee_name"),
                    "",  # To be filled in
                    "",  # Reference
                    "",  # Reference
                ])
            
            # Write reference section
            writer.writerow([])
            writer.writerow(["=== LOCAL USERS REFERENCE ==="])
            writer.writerow(["User ID", "Email", "Full Name"])
            for user in local_users:
                writer.writerow([
                    user.get("user_id"),
                    user.get("email"),
                    user.get("full_name"),
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
        display_netsuite_employees(netsuite_employees)
        
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
