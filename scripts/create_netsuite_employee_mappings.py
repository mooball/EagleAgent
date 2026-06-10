"""
create_netsuite_employee_mappings.py

Create initial NetSuite employee mappings from verified Google Workspace matches.

This populates the netsuite_employee_mappings table with the 10 active employees
verified via Google Workspace directory.

Usage:
  uv run python -m scripts.create_netsuite_employee_mappings --dry-run
  uv run python -m scripts.create_netsuite_employee_mappings
"""

import argparse
import logging

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from config.settings import Config
from includes.dashboard.models import NetSuiteEmployeeMapping

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


# Verified employee mappings from Google Workspace directory
VERIFIED_MAPPINGS = [
    {"netsuite_id": "-5", "name": "Bill Watt", "email": "bill@eagle-exports.com", "is_active": True},
    {"netsuite_id": "8145", "name": "Darren Whiting", "email": "darren@eagle-exports.com", "is_active": True},
    {"netsuite_id": "9768", "name": "Tomoko Matsuba", "email": "tomoko@eagle-exports.com", "is_active": True},
    {"netsuite_id": "17625", "name": "Annabelle Watt", "email": "annabelle@eagle-exports.com", "is_active": True},
    {"netsuite_id": "32463", "name": "Angie Bonus", "email": "angie@eagle-exports.com", "is_active": True},
    {"netsuite_id": "33331", "name": "Harry Busacay", "email": "harry@eagle-exports.com", "is_active": True},
    {"netsuite_id": "524093", "name": "Sandy Smith", "email": "sandy@eagle-exports.com", "is_active": True},
    {"netsuite_id": "1886802", "name": "Matt Davis", "email": "matt@eagle-exports.com", "is_active": True},
    {"netsuite_id": "7401040", "name": "Bernard Saw", "email": "bernard@eagle-exports.com", "is_active": True},
    {"netsuite_id": "7965937", "name": "Harry Watt", "email": "hwatt@eagle-exports.com", "is_active": True},
]

# Inactive employees (no Google Workspace account)
INACTIVE_MAPPINGS = [
    {"netsuite_id": "406", "name": "Guillaume Amiot", "email": None, "is_active": False},
    {"netsuite_id": "16966", "name": "Jarred Parkinson", "email": None, "is_active": False},
    {"netsuite_id": "30631", "name": "Robert Cuddeford", "email": None, "is_active": False},
    {"netsuite_id": "33431", "name": "Myles Garner", "email": None, "is_active": False},
    {"netsuite_id": "35472", "name": "Nathan Eacott", "email": None, "is_active": False},
    {"netsuite_id": "2847756", "name": "Fletcher Watt", "email": None, "is_active": False},
    {"netsuite_id": "7300978", "name": "Daniel Lakay", "email": None, "is_active": False},
]


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


def create_mappings(include_inactive: bool = True, dry_run: bool = False):
    """Create NetSuite employee mappings.
    
    Args:
        include_inactive: If True, also add inactive employees (no Google Workspace account)
        dry_run: If True, show what would be created without writing to DB
    """
    engine = get_engine()
    Session = sessionmaker(bind=engine)
    session = Session()
    
    print(f"\n📥 Creating NetSuite employee mappings...")
    
    mappings = VERIFIED_MAPPINGS.copy()
    if include_inactive:
        mappings.extend(INACTIVE_MAPPINGS)
    
    print(f"   Found {len(mappings)} total mappings ({len(VERIFIED_MAPPINGS)} active, {len(INACTIVE_MAPPINGS)} inactive)")
    
    try:
        created = 0
        skipped = 0
        
        for mapping_data in mappings:
            try:
                # Check if already exists
                existing = session.execute(
                    select(NetSuiteEmployeeMapping).where(
                        NetSuiteEmployeeMapping.netsuite_employee_id == mapping_data["netsuite_id"]
                    )
                ).scalars().first()
                
                if existing:
                    skipped += 1
                    continue
                
                # Create new mapping
                mapping = NetSuiteEmployeeMapping(
                    netsuite_employee_id=mapping_data["netsuite_id"],
                    name=mapping_data["name"],
                    email=mapping_data.get("email"),
                    is_active=mapping_data.get("is_active", True),
                )
                session.add(mapping)
                created += 1
            
            except Exception as e:
                logger.error(f"Error creating mapping for {mapping_data.get('name')}: {e}")
        
        if not dry_run:
            session.commit()
            print(f"✅ Done: {created} created, {skipped} skipped")
        else:
            print(f"🔍 DRY RUN: Would create {created}, skip {skipped}")
    
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        raise
    finally:
        session.close()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Create initial NetSuite employee mappings"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created without writing to DB",
    )
    parser.add_argument(
        "--active-only",
        action="store_true",
        help="Only create mappings for active employees (skip inactive)",
    )
    
    args = parser.parse_args()
    
    print("\n🔄 NetSuite Employee Mappings Setup")
    print("=" * 80)
    print(f"Dry run: {args.dry_run}")
    print(f"Include inactive: {not args.active_only}")
    print("=" * 80)
    
    create_mappings(
        include_inactive=not args.active_only,
        dry_run=args.dry_run
    )


if __name__ == "__main__":
    main()
