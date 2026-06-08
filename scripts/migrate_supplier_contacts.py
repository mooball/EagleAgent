"""
migrate_supplier_contacts.py

One-time migration: moves supplier contacts from JSONB (suppliers.contacts field)
into the new unified contacts table.

After running this, suppliers.contacts can continue to be used as a cache, or 
the field can be deprecated.

Usage:
  uv run python -m scripts.migrate_supplier_contacts --dry-run
  uv run python -m scripts.migrate_supplier_contacts
"""

import argparse
import logging
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from config.settings import Config
from includes.dashboard.models import Supplier, Contact

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


def migrate_supplier_contacts(dry_run: bool = False):
    """Migrate supplier contacts from JSONB to unified contacts table.
    
    Args:
        dry_run: If True, show what would be migrated without writing to DB
    """
    engine = get_engine()
    Session = sessionmaker(bind=engine)
    session = Session()
    
    print("\n📥 Migrating supplier contacts from JSONB to unified contacts table...")
    
    try:
        # Find all suppliers with contacts
        suppliers = session.execute(
            select(Supplier).where(Supplier.contacts.isnot(None))
        ).scalars().all()
        
        print(f"   Found {len(suppliers)} suppliers with contacts")
        
        migrated = 0
        skipped = 0
        errors = 0
        
        for supplier in suppliers:
            try:
                contacts_list = supplier.contacts
                if not contacts_list or not isinstance(contacts_list, list):
                    continue
                
                for contact_data in contacts_list:
                    try:
                        # Check if already migrated
                        email = contact_data.get("email")
                        existing = None
                        if email:
                            existing = session.execute(
                                select(Contact).where(
                                    Contact.supplier_id == supplier.id,
                                    Contact.email == email,
                                )
                            ).scalars().first()
                        
                        if existing:
                            skipped += 1
                            continue
                        
                        # Create new contact
                        contact = Contact(
                            id=uuid.uuid4(),
                            supplier_id=supplier.id,
                            label=contact_data.get("label"),
                            fullname=contact_data.get("name"),
                            email=contact_data.get("email"),
                            phone=contact_data.get("phone"),
                            isinactive=False,
                        )
                        session.add(contact)
                        migrated += 1
                    
                    except Exception as e:
                        logger.error(f"Error migrating contact for supplier {supplier.id}: {e}")
                        errors += 1
            
            except Exception as e:
                logger.error(f"Error processing supplier {supplier.id}: {e}")
                errors += 1
        
        if not dry_run:
            session.commit()
            print(f"✅ Migration complete: {migrated} migrated, {skipped} skipped, {errors} errors")
        else:
            print(f"🔍 DRY RUN: Would migrate {migrated}, skip {skipped} ({errors} errors)")
    
    except Exception as e:
        logger.error(f"Fatal error during migration: {e}", exc_info=True)
        raise
    finally:
        session.close()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Migrate supplier contacts from JSONB to unified contacts table"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without writing to DB",
    )
    
    args = parser.parse_args()
    
    print("\n🔄 Supplier Contacts Migration")
    print("=" * 80)
    print(f"Dry run: {args.dry_run}")
    print("=" * 80)
    
    migrate_supplier_contacts(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
