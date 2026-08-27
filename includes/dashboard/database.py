"""
Shared database session factories for FastAPI dashboard routes.

Provides both async (for FastAPI route handlers) and sync (for legacy
tool compatibility) session factories using the same DATABASE_URL.
"""

import logging
import threading
import time
from urllib.parse import urlparse

from sqlalchemy import create_engine, func, or_, literal, text
from sqlalchemy.orm import sessionmaker, Session

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


def get_session() -> Session:
    """Return a new sync SQLAlchemy session (caller must close)."""
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(_sync_url(), pool_pre_ping=True, pool_size=10, max_overflow=20)
        _SessionLocal = sessionmaker(bind=_engine)
    return _SessionLocal()


def match_supplier_by_name(name: str, session=None) -> "Supplier | None":
    """Find the best DB match for a supplier name.

    Three-pass strategy:
    1. Containment — DB name in input or input in DB name (prefer closest length)
    2. Alt names — check if input matches any entry in the alt_names JSONB array
    3. pg_trgm similarity fallback (threshold 0.8)

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
                .filter(sim > 0.8)
                .order_by(sim.desc())
                .first()
            )
        return row
    finally:
        if own_session:
            session.close()


def match_suppliers_by_names(names: list[str], session=None) -> dict[str, "Supplier | None"]:
    """Batch version of match_supplier_by_name for many names at once.

    Mirrors the three-pass strategy (containment → alt_names → trigram) but
    runs it with a handful of queries instead of ~3 per name. Semantics match
    the per-name function; only the ordering of otherwise-equal candidate
    ties may differ. Results are cached in-process for a short TTL because
    the same RFQ names re-resolve on every tab render.

    Args:
        names: Supplier names to match (case-insensitive).
        session: Optional SQLAlchemy session (caller-managed).

    Returns:
        Dict mapping each lowercased input name to the matched Supplier or None.
    """
    import json

    from includes.dashboard.models import Supplier
    from sqlalchemy import cast, String

    names_lower = sorted({n.strip().lower() for n in names if n and n.strip()})
    if not names_lower:
        return {}

    own_session = session is None
    if own_session:
        session = get_session()
    try:
        results: dict[str, "Supplier | None"] = {n: None for n in names_lower}

        # ---- short-TTL cache: name -> (expiry, supplier_id str | None) ----
        now = time.time()
        to_resolve: list[str] = []
        cached_ids: dict[str, str | None] = {}
        with _NAME_MATCH_CACHE_LOCK:
            for n in names_lower:
                entry = _NAME_MATCH_CACHE.get(n)
                if entry is not None and entry[0] > now:
                    cached_ids[n] = entry[1]
                else:
                    to_resolve.append(n)

        pending = to_resolve

        # Pass 1: containment (both directions) — one query for all names
        if pending:
            conds = []
            for n in pending:
                lname = func.lower(Supplier.name)
                conds.append(lname.contains(n))
                conds.append(literal(n).contains(lname))
            rows = session.query(Supplier).filter(or_(*conds)).all()
            candidates: dict[str, list] = {n: [] for n in pending}
            for row in rows:
                rn = (row.name or "").strip().lower()
                rlen = len((row.name or "").strip())
                for n in pending:
                    if n in rn or rn in n:
                        candidates[n].append((abs(rlen - len(n)), row))
            for n in pending:
                if candidates[n]:
                    # closest-length match wins, mirroring order_by(length diff).first()
                    results[n] = min(candidates[n], key=lambda t: t[0])[1]
            pending = [n for n in pending if results[n] is None]

        # Pass 2: alt_names JSONB substring match — one query for remaining names
        if pending:
            conds = [
                func.lower(cast(Supplier.alt_names, String)).contains(n)
                for n in pending
            ]
            rows = session.query(Supplier).filter(or_(*conds)).all()
            blobs = {}
            for row in rows:
                alt = row.alt_names
                try:
                    blobs[id(row)] = json.dumps(alt).lower() if alt is not None else None
                except (TypeError, ValueError):
                    blobs[id(row)] = str(alt).lower() if alt is not None else None
            for n in pending:
                for row in rows:
                    blob = blobs.get(id(row))
                    if blob and n in blob:
                        results[n] = row
                        break
            pending = [n for n in pending if results[n] is None]

        # Pass 3: trigram similarity fallback — ONE cross-join query for all names
        if pending:
            value_rows = ", ".join(
                f"('{n.replace(chr(39), chr(39) * 2)}')" for n in pending
            )
            q = text(
                f"""
                WITH v(n) AS (VALUES {value_rows})
                SELECT s.id, v.n, similarity(lower(s.name), v.n) AS sim
                FROM suppliers s, v
                WHERE similarity(lower(s.name), v.n) > 0.8
                ORDER BY v.n, sim DESC
                """
            )
            ids_by_name: dict[str, str] = {}
            for r in session.execute(q).all():
                ids_by_name.setdefault(r.n, str(r.id))
            hit_ids = [ids_by_name[n] for n in pending if n in ids_by_name]
            if hit_ids:
                rows_by_id = {str(r.id): r for r in session.query(Supplier).filter(Supplier.id.in_(hit_ids)).all()}
                for n in pending:
                    sid = ids_by_name.get(n)
                    if sid:
                        results[n] = rows_by_id.get(sid)

        # ---- update cache with fresh results ----
        with _NAME_MATCH_CACHE_LOCK:
            for n in to_resolve:
                row = results.get(n)
                _NAME_MATCH_CACHE[n] = (
                    time.time() + _NAME_MATCH_CACHE_TTL,
                    str(row.id) if row is not None else None,
                )

        # ---- resolve cache hits back into Supplier objects ----
        cached_hits = list({sid for sid in cached_ids.values() if sid})
        if cached_hits:
            rows_by_id = {
                str(r.id): r
                for r in session.query(Supplier).filter(Supplier.id.in_(cached_hits)).all()
            }
            for n, sid in cached_ids.items():
                if sid and results[n] is None:
                    results[n] = rows_by_id.get(sid)

        return results
    finally:
        if own_session:
            session.close()


# In-process name-match cache (name -> (expiry, supplier_id or None))
_NAME_MATCH_CACHE: dict[str, tuple[float, str | None]] = {}
_NAME_MATCH_CACHE_LOCK = threading.Lock()
_NAME_MATCH_CACHE_TTL = 120.0


# Second-level labels that are part of the public suffix, never the
# registrable name — 'pngaf.com.pg' must not reduce to 'com.pg'.
GENERIC_SLD_LABELS = {
    "com", "net", "org", "gov", "edu", "co", "ac", "asn", "id", "mil", "sch",
}


def _extract_domain(url: str) -> str | None:
    """Extract the registrable (root) domain from a URL, stripping subdomains.

    Heuristic: if the second-to-last label is a public-suffix label such as
    'com', 'co' or 'gov' (e.g. .com.au, .co.uk, .com.pg) keep the last 3
    labels, otherwise keep the last 2.

    Examples:
        'https://www.abcparts.com.au/contact' → 'abcparts.com.au'
        'https://my.komatsu.com.au'           → 'komatsu.com.au'
        'http://sleatorplant.com'              → 'sleatorplant.com'
        'https://shop.example.co.uk'           → 'example.co.uk'
        'https://www.pngaf.com.pg'             → 'pngaf.com.pg'
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
        if len(parts) >= 3 and parts[-2] in GENERIC_SLD_LABELS:
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
    name_lower = name.strip().lower()
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
                    # Domain match is strong, but verify names are at least loosely compatible
                    s_name = (s.name or "").strip().lower()
                    if s_name and name_lower not in s_name and s_name not in name_lower:
                        from difflib import SequenceMatcher
                        name_sim = SequenceMatcher(None, name_lower, s_name).ratio()
                        if name_sim < 0.3:
                            logger.info(
                                f"[supplier-match] '{name}' domain-matched '{s.name}' "
                                f"({incoming_domain}) but REJECTED: names too different (sim={name_sim:.2f})"
                            )
                            continue
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


def update_supplier(supplier_id: str, updates: dict, modified_by: str) -> None:
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
        # Keep the dedup match-key index in sync (local import avoids a cycle:
        # supplier_matching imports _extract_domain from this module)
        from includes.dashboard.supplier_matching import rebuild_match_keys
        rebuild_match_keys(session, supplier)
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
