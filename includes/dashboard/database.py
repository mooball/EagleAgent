"""
Shared database session factories for FastAPI dashboard routes.

Provides both async (for FastAPI route handlers) and sync (for legacy
tool compatibility) session factories using the same DATABASE_URL.
"""

import logging
from urllib.parse import urlparse

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

    Three-pass strategy:
    1. Containment — DB name in input or input in DB name (prefer closest length)
    2. Alt names — check if input matches any entry in the alt_names JSONB array
    3. pg_trgm similarity fallback (threshold 0.6)

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
        # Pass 1: containment check — prefer the name closest in length to the input
        row = (
            session.query(Supplier)
            .filter(
                or_(
                    func.lower(Supplier.name).contains(name_lower),
                    literal(name_lower).contains(func.lower(Supplier.name)),
                )
            )
            .order_by(func.abs(func.length(Supplier.name) - len(name_lower)))
            .first()
        )
        # Pass 2: alt_names array match
        if not row:
            from sqlalchemy import cast, String
            from sqlalchemy.dialects.postgresql import JSONB
            # Check if any element in alt_names matches (case-insensitive)
            row = (
                session.query(Supplier)
                .filter(
                    Supplier.alt_names.isnot(None),
                    func.lower(cast(Supplier.alt_names, String)).contains(name_lower),
                )
                .first()
            )
        # Pass 3: trigram similarity fallback
        if not row:
            sim = func.similarity(func.lower(Supplier.name), name_lower)
            row = (
                session.query(Supplier)
                .filter(sim > 0.6)
                .order_by(sim.desc())
                .first()
            )
        return row
    finally:
        if own_session:
            session.close()


def _extract_domain(url: str) -> str | None:
    """Extract the registrable (root) domain from a URL, stripping subdomains.

    Uses a simple heuristic: if the TLD is a two-part ccTLD (e.g. .com.au,
    .co.uk, .co.nz) keep the last 3 labels, otherwise keep the last 2.

    Examples:
        'https://www.abcparts.com.au/contact' → 'abcparts.com.au'
        'https://my.komatsu.com.au'           → 'komatsu.com.au'
        'http://sleatorplant.com'              → 'sleatorplant.com'
        'https://shop.example.co.uk'           → 'example.co.uk'
    Returns None if the URL cannot be parsed.
    """
    if not url:
        return None
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            return None
        hostname = hostname.lower()
        parts = hostname.split(".")
        # Two-part ccTLDs where the registrable domain is 3 labels deep
        _TWO_PART_TLDS = {
            "com.au", "com.br", "com.cn", "com.hk", "com.my", "com.sg",
            "com.tw", "co.id", "co.in", "co.jp", "co.kr", "co.nz", "co.th",
            "co.uk", "co.za", "net.au", "org.au", "org.uk", "org.nz",
        }
        if len(parts) >= 3 and f"{parts[-2]}.{parts[-1]}" in _TWO_PART_TLDS:
            return ".".join(parts[-3:])
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return hostname
    except Exception:
        return None


def match_supplier(
    name: str,
    url: str | None = None,
    country: str | None = None,
    session=None,
) -> "Supplier | None":
    """Find the best DB match for a supplier, verifying with domain/country.

    1. Call match_supplier_by_name() to get a name-based candidate.
    2. If a candidate is found, verify it against corroborating attributes:
       - Domain: if both sides have a URL, domains must match.
       - Country: if both sides have a country, they must match.
       - If neither can be compared, accept only exact containment matches
         (not trigram-only).
    3. Return the verified candidate or None.
    """
    own_session = session is None
    if own_session:
        from includes.dashboard.database import get_session
        session = get_session()
    try:
        # --- Domain-first lookup ---
        # If a URL is provided, search for any existing supplier with the
        # same root domain.  A matching domain is a very strong signal that
        # it's the same business, even if the name differs significantly
        # (e.g. "Repco Australia" vs "Repco Export & Wholesale").
        incoming_domain = _extract_domain(url)
        if incoming_domain:
            from includes.dashboard.models import Supplier
            all_suppliers = session.query(Supplier).filter(
                Supplier.url.isnot(None)
            ).all()
            for s in all_suppliers:
                s_domain = _extract_domain(s.url)
                if s_domain and s_domain == incoming_domain:
                    logger.info(
                        f"[supplier-match] '{name}' → '{s.name}' via domain match ({incoming_domain})"
                    )
                    return s
                # Check alt_domains array
                if s.alt_domains:
                    for alt_d in s.alt_domains:
                        if alt_d and alt_d.lower() == incoming_domain:
                            logger.info(
                                f"[supplier-match] '{name}' → '{s.name}' via alt_domain match ({incoming_domain})"
                            )
                            return s
                # Also check contact URLs
                if s.contacts:
                    for c in s.contacts:
                        if isinstance(c, dict) and c.get("url"):
                            c_domain = _extract_domain(c["url"])
                            if c_domain and c_domain == incoming_domain:
                                logger.info(
                                    f"[supplier-match] '{name}' → '{s.name}' via contact domain match ({incoming_domain})"
                                )
                                return s

        # --- Name-based matching with verification ---
        candidate = match_supplier_by_name(name, session=session)
        if not candidate:
            return None

        # Extract domain from the incoming URL
        incoming_domain = _extract_domain(url)

        # Extract domain from the candidate's contacts or url field
        candidate_domain = _extract_domain(getattr(candidate, "url", None))
        if not candidate_domain and candidate.contacts:
            for c in candidate.contacts:
                if isinstance(c, dict) and c.get("url"):
                    candidate_domain = _extract_domain(c["url"])
                    if candidate_domain:
                        break

        # Also gather alt_domains for the candidate
        candidate_alt_domains = set()
        if candidate.alt_domains:
            candidate_alt_domains = {d.lower() for d in candidate.alt_domains if d}

        # Domain check: if both have domains, they must match (primary or alt)
        if incoming_domain and candidate_domain:
            if incoming_domain != candidate_domain and incoming_domain not in candidate_alt_domains:
                logger.info(
                    f"[supplier-match] '{name}' name-matched '{candidate.name}' "
                    f"but REJECTED: domain mismatch ({incoming_domain} vs {candidate_domain})"
                )
                return None

        # Country check: if both have countries, they must match
        incoming_country = (country or "").strip().upper()
        candidate_country = (getattr(candidate, "country", None) or "").strip().upper()
        if incoming_country and candidate_country:
            if incoming_country != candidate_country:
                logger.info(
                    f"[supplier-match] '{name}' name-matched '{candidate.name}' "
                    f"but REJECTED: country mismatch ({incoming_country} vs {candidate_country})"
                )
                return None

        # If neither domain nor country could be compared, only accept
        # high-confidence name matches (containment, not trigram-only).
        if not incoming_domain and not candidate_domain and not incoming_country and not candidate_country:
            name_lower = name.strip().lower()
            cand_lower = (candidate.name or "").strip().lower()
            if name_lower not in cand_lower and cand_lower not in name_lower:
                logger.info(
                    f"[supplier-match] '{name}' trigram-matched '{candidate.name}' "
                    f"but REJECTED: no corroborating attributes and not a containment match"
                )
                return None

        return candidate
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
_SUPPLIER_EDITABLE = {"name", "url", "address_1", "city", "country", "notes", "terms", "contacts", "supply_chain_position", "alt_names", "alt_domains"}


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
