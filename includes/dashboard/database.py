"""
Shared database session factories for FastAPI dashboard routes.

Provides both async (for FastAPI route handlers) and sync (for legacy
tool compatibility) session factories using the same DATABASE_URL.
"""

import logging

from sqlalchemy import create_engine, func, or_, literal
from sqlalchemy.orm import sessionmaker

from config import config

logger = logging.getLogger(__name__)


def _sync_url() -> str:
    """Convert the async DATABASE_URL to a sync psycopg URL."""
    url = config.DATABASE_URL
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    return url


_engine = None
_SessionLocal = None


def get_session():
    """Return a new sync SQLAlchemy session (caller must close)."""
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(_sync_url(), pool_pre_ping=True)
        _SessionLocal = sessionmaker(bind=_engine)
    return _SessionLocal()


def match_supplier_by_name(name: str, session=None) -> "Supplier | None":
    """Find the best DB match for a supplier name.

    Two-pass strategy:
    1. Containment — DB name in input or input in DB name
    2. pg_trgm similarity fallback (threshold 0.4)

    Returns the Supplier row or None. Caller manages the session.
    """
    from includes.dashboard.models import Supplier

    name_lower = name.strip().lower()
    if not name_lower:
        return None

    own_session = session is None
    if own_session:
        session = get_session()
    try:
        # Pass 1: containment check
        row = (
            session.query(Supplier)
            .filter(
                or_(
                    func.lower(Supplier.name).contains(name_lower),
                    literal(name_lower).contains(func.lower(Supplier.name)),
                )
            )
            .order_by(func.length(Supplier.name).desc())
            .first()
        )
        # Pass 2: trigram similarity fallback
        if not row:
            sim = func.similarity(func.lower(Supplier.name), name_lower)
            row = (
                session.query(Supplier)
                .filter(sim > 0.4)
                .order_by(sim.desc())
                .first()
            )
        return row
    finally:
        if own_session:
            session.close()


def merge_supplier_contacts(sup: dict, db_contacts: list) -> None:
    """Merge DB contacts into a supplier dict, preserving existing data."""
    existing = sup.get("contacts") or []
    if not isinstance(existing, list):
        existing = []
    existing_emails = {
        c.get("email") for c in existing if isinstance(c, dict) and c.get("email")
    }
    existing_phones = {
        c.get("phone") for c in existing if isinstance(c, dict) and c.get("phone")
    }
    for db_c in db_contacts:
        if not isinstance(db_c, dict):
            continue
        email = db_c.get("email")
        phone = db_c.get("phone")
        if (email and email not in existing_emails) or (
            phone and phone not in existing_phones
        ):
            existing.append(db_c)
            if email:
                existing_emails.add(email)
            if phone:
                existing_phones.add(phone)
    sup["contacts"] = existing


# --- Supplier update helpers ---------------------------------------------------

# Fields that can be edited through the UI form
_SUPPLIER_EDITABLE = {"name", "url", "address_1", "city", "country", "notes", "terms", "supply_chain_position"}


def update_supplier(supplier_id: str, updates: dict, modified_by: str):
    """Update allowed supplier fields and set modified_at/modified_by.

    Returns the updated Supplier row or None if not found.
    """
    from datetime import datetime, timezone
    from includes.dashboard.models import Supplier

    session = get_session()
    try:
        supplier = session.query(Supplier).filter(Supplier.id == supplier_id).first()
        if not supplier:
            return None
        for key, value in updates.items():
            if key in _SUPPLIER_EDITABLE:
                if key == "supply_chain_position" and isinstance(value, dict):
                    # Merge with existing JSONB to preserve AI-set fields
                    existing = dict(supplier.supply_chain_position or {})
                    existing.update(value)
                    setattr(supplier, key, existing)
                else:
                    setattr(supplier, key, value or None)
        supplier.modified_at = datetime.now(timezone.utc)
        supplier.modified_by = modified_by
        session.commit()
        session.refresh(supplier)
        return supplier
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def add_supplier_comment(supplier_id: str, author: str, comment: str) -> list:
    """Append a comment to the supplier's comments JSONB list.

    Returns the updated comments list.
    """
    from datetime import datetime, timezone
    from includes.dashboard.models import Supplier

    session = get_session()
    try:
        supplier = session.query(Supplier).filter(Supplier.id == supplier_id).first()
        if not supplier:
            return []
        existing = list(supplier.comments or [])
        existing.append({
            "author": author,
            "comment": comment,
            "ts": datetime.now(timezone.utc).strftime("%d %b %Y %H:%M"),
        })
        supplier.comments = existing
        supplier.modified_at = datetime.now(timezone.utc)
        supplier.modified_by = f"user:{author}"
        session.commit()
        return existing
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
