"""
list_netsuite_employees.py

Discovers and lists employee records from NetSuite.
Useful for identifying who is active and for maintaining employee mappings.

Usage:
  uv run python -m scripts.list_netsuite_employees
  uv run python -m scripts.list_netsuite_employees --export employees.csv
"""

import argparse
import csv
import logging
from datetime import datetime

from sqlalchemy import create_engine, select, func, text
from sqlalchemy.orm import sessionmaker

from config.settings import Config
from includes.dashboard.models import NetSuiteEmployeeMapping

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


def get_engine():
    """Get database engine."""
    db_url = Config.DATABASE_URL
    if not db_url:
        raise ValueError("DATABASE_URL is empty. Check your `.env` settings.")

    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return create_engine(db_url)


def list_employees(export_csv: str | None = None):
    """List all known employee mappings from the database.
    
    Args:
        export_csv: Optional path to export CSV file
    """
    engine = get_engine()
    Session = sessionmaker(bind=engine)

    print("\n👥 NetSuite Employee Mappings")
    print("=" * 100)

    with Session() as session:
        mappings = session.execute(
            select(NetSuiteEmployeeMapping).order_by(
                NetSuiteEmployeeMapping.is_active.desc(),
                NetSuiteEmployeeMapping.name.asc()
            )
        ).scalars().all()

        if not mappings:
            print("No employee mappings found in database.")
            return

        active = [m for m in mappings if m.is_active]
        inactive = [m for m in mappings if not m.is_active]

        print(f"\nActive Employees: {len(active)}")
        print("-" * 100)
        print(f"{'NetSuite ID':<15} {'Name':<30} {'Email':<40}")
        print("-" * 100)
        for emp in active:
            email = emp.email or "(no email)"
            print(f"{emp.netsuite_employee_id:<15} {emp.name:<30} {email:<40}")

        if inactive:
            print(f"\n\nInactive Employees: {len(inactive)}")
            print("-" * 100)
            print(f"{'NetSuite ID':<15} {'Name':<30} {'Email':<40}")
            print("-" * 100)
            for emp in inactive:
                email = emp.email or "(no email)"
                print(f"{emp.netsuite_employee_id:<15} {emp.name:<30} {email:<40}")

        print("\n" + "=" * 100)
        print(f"Total: {len(mappings)} employees ({len(active)} active, {len(inactive)} inactive)")

        if export_csv:
            export_to_csv(mappings, export_csv)


def export_to_csv(mappings, filepath: str):
    """Export employee mappings to CSV file.
    
    Args:
        mappings: List of NetSuiteEmployeeMapping objects
        filepath: Path to write CSV to
    """
    try:
        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["NetSuite ID", "Name", "Email", "Is Active"])
            for emp in mappings:
                writer.writerow([
                    emp.netsuite_employee_id,
                    emp.name,
                    emp.email or "",
                    "Yes" if emp.is_active else "No"
                ])
        print(f"\n✅ Exported {len(mappings)} employees to {filepath}")
    except Exception as e:
        logger.error(f"Error exporting to CSV: {e}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="List employee mappings from NetSuite"
    )
    parser.add_argument(
        "--export",
        type=str,
        default=None,
        help="Export mappings to CSV file",
    )

    args = parser.parse_args()

    list_employees(export_csv=args.export)


if __name__ == "__main__":
    main()
