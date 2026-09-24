"""Create a supplier in the local database — the service behind quick-add.

**Local-first, deliberately.** A new supplier is written to the ``suppliers``
table only; pushing it to NetSuite stays a separate, deliberate step (the "NS+"
flow on the RFQ quotation tab already does exactly that, and it reads the local
row this module produces). Two reasons that ordering is right:

* NetSuite needs fields that a quick add should not be asking for (category,
  tax item, custom form) — see ``includes/netsuite/records/vendor.py``.
* Nothing downstream requires a NetSuite id: ``_rfq_sync_readiness`` treats
  "not in NetSuite" as a warning, never a stopper, so a supplier created here
  can be quoted against immediately.

The interesting part of this module is not the INSERT. It is that a supplier row
*on its own* is nearly useless:

* ``find_all_matches`` (inbound email → RFQ/supplier matching) builds its domain
  index from DB rows — the ``contacts`` table, ``customers.email`` and
  ``suppliers.url``/``alt_domains``. A supplier with no Contact row cannot be
  matched to anything, which is the defect that left ~900 web-discovered
  suppliers permanently unlinked.
* ``supplier_match_keys`` derives searchable keys from ``suppliers.contacts``
  (the JSONB column) and ``url``/``alt_domains``.

So creation writes **both** the Contact row and the JSONB contacts list, and
rebuilds the match keys. The JSONB half is not redundant: the RFQ quotation tab
and its NetSuite modal prefill from ``suppliers.contacts[0]``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "SUPPLIER_FIELDS",
    "DuplicateReport",
    "SupplierField",
    "create_local_supplier",
    "describe_matches",
    "field_options",
    "validate",
]

#: Deliberately permissive: the job is to catch typos ("acme.com" with no @),
#: not to adjudicate RFC 5322. A supplier whose email is odd but real must not
#: be blocked from being created.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_MAX_NAME = 200
_MAX_FIELD = 500


@dataclass(frozen=True)
class SupplierField:
    """One form field, shared by every renderer of this form.

    The chat widget renders these in a narrow two-column card; a wide dashboard
    form would lay the same list out differently. Validation, option sources and
    labels stay here so the two can never disagree about what "valid" means.
    """

    key: str
    label: str
    kind: str = "text"          # text | email | tel | url | select
    required: bool = False
    placeholder: str = ""
    hint: str = ""
    options: str = ""           # key into field_options()
    maxlength: int = _MAX_FIELD
    group: str = "main"         # "main" = above the fold | "more" = optional


#: The local supplier columns, in form order. NetSuite-only fields (category,
#: tax item, subsidiary, custom form) are absent on purpose — they belong to the
#: promotion step, not to a quick add.
SUPPLIER_FIELDS: list[SupplierField] = [
    SupplierField("name", "Company name", required=True,
                  placeholder="e.g. Acme Fasteners Pty Ltd", maxlength=_MAX_NAME),
    SupplierField("contact_name", "Contact name", placeholder="Who we deal with"),
    SupplierField("email", "Contact email", kind="email", required=True,
                  placeholder="sales@acme.com.au",
                  hint="Used to match their replies to RFQs automatically"),
    SupplierField("phone", "Phone", kind="tel"),
    SupplierField("url", "Website", kind="url", group="more",
                  placeholder="https://…"),
    SupplierField("country", "Country", kind="select", options="countries",
                  group="more"),
    SupplierField("currency", "Currency", kind="select", options="currencies",
                  group="more"),
    SupplierField("terms", "Payment terms", kind="select", options="terms",
                  group="more"),
    SupplierField("address_1", "Address line 1", group="more"),
    SupplierField("address_2", "Address line 2", group="more"),
    SupplierField("city", "City", group="more"),
    SupplierField("state", "State", group="more"),
    SupplierField("postcode", "Postcode", group="more"),
]

_FIELDS_BY_KEY = {f.key: f for f in SUPPLIER_FIELDS}

#: Columns copied straight from the clean form data.
_COLUMN_KEYS = (
    "name", "url", "country", "currency", "terms",
    "address_1", "address_2", "city", "state", "postcode",
)


def field_options() -> dict[str, Any]:
    """Option lists for the ``select`` fields.

    Sourced from the NetSuite lookup tables the promotion step uses, so a value
    chosen here is already valid there. ``terms`` stores the term *name* — that
    is what the local column holds and what ``resolve_*``/TERM_OPTIONS maps back
    to an id when the vendor is created.
    """
    from includes.netsuite.countries import country_option_groups
    from includes.netsuite.records.vendor import CURRENCY_OPTIONS, TERM_OPTIONS

    return {
        "countries": country_option_groups(),          # [{label, options:[{code,name}]}]
        "currencies": [{"value": k, "label": k} for k in CURRENCY_OPTIONS],
        "terms": [{"value": name, "label": name} for name in TERM_OPTIONS],
    }


def validate(data: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Normalise a submission. Returns ``(clean, errors_by_field)``.

    Errors are returned per field rather than as one string so the form can mark
    the offending input instead of making the user re-read a paragraph.
    """
    from includes.netsuite.countries import COUNTRIES

    clean: dict[str, str] = {}
    errors: dict[str, str] = {}

    for spec in SUPPLIER_FIELDS:
        raw = data.get(spec.key)
        if isinstance(raw, list):
            raw = raw[0] if raw else ""
        value = str(raw or "").strip()
        if len(value) > spec.maxlength:
            value = value[: spec.maxlength]
        clean[spec.key] = value

    if not clean["name"]:
        errors["name"] = "Company name is required."
    if not clean["email"]:
        errors["email"] = "Contact email is required."
    elif not _EMAIL_RE.match(clean["email"]):
        errors["email"] = "That does not look like an email address."

    clean["currency"] = clean["currency"].upper()
    if clean["currency"]:
        from includes.netsuite.records.vendor import CURRENCY_OPTIONS

        if clean["currency"] not in CURRENCY_OPTIONS:
            errors["currency"] = f"Unknown currency: {clean['currency']}."
    else:
        # NetSuite's base currency — the sane default for a local supplier, and
        # what the promotion step assumes when none is given.
        clean["currency"] = "AUD"

    clean["country"] = clean["country"].upper()
    if clean["country"]:
        known = {code for code, _name in COUNTRIES}
        if clean["country"] not in known:
            errors["country"] = f"Unknown country code: {clean['country']}."

    if clean["url"] and not re.match(r"^https?://", clean["url"], re.I):
        clean["url"] = "https://" + clean["url"]

    return clean, errors


@dataclass
class DuplicateReport:
    """What the duplicate check found, in two shapes: the raw near-misses that
    ``nominate_near_misses`` consumes, and display rows for the confirm step."""

    has_match: bool = False
    display: list[dict] = field(default_factory=list)
    near_misses: list[dict] = field(default_factory=list)


def describe_matches(match: Any) -> DuplicateReport:
    """Turn a ``SupplierMatch`` into display rows + raw near-misses.

    A confident match and a rejected near-miss are different warnings — "this
    already exists" versus "this looks similar" — so they keep their labels all
    the way to the UI instead of being flattened into one list of names.
    """
    report = DuplicateReport(near_misses=list(getattr(match, "near_misses", []) or []))
    existing = getattr(match, "supplier", None)
    if existing is not None:
        report.has_match = True
        report.display.append({
            "id": str(existing.id),
            "name": existing.name,
            "confidence": 100,
            "kind": "existing",
            "reasons": ["already in the database"],
        })
    for nm in report.near_misses:
        other = nm.get("supplier")
        if other is None:
            continue
        report.display.append({
            "id": str(other.id),
            "name": other.name,
            "confidence": round((nm.get("confidence") or 0) * 100),
            "kind": "similar",
            "reasons": [nm.get("rejected_because") or "similar name"],
        })
    return report


def find_duplicates(
    name: str,
    *,
    url: Optional[str] = None,
    country: Optional[str] = None,
    session: Any = None,
) -> DuplicateReport:
    """Check a *proposed* supplier against the database before it exists.

    ``match_supplier`` is the same gate the web-discovery path uses, so a
    hand-typed supplier is held to the same standard as a scraped one — and the
    rejected near-misses feed the existing dedup review queue, not the bin.
    """
    from includes.dashboard.database import match_supplier

    match = match_supplier(name, url=url, country=country, session=session)
    return describe_matches(match)


def create_local_supplier(
    clean: dict[str, str],
    *,
    user_email: str,
    near_misses: Optional[list[dict]] = None,
    session: Any = None,
) -> Any:
    """Insert the supplier, its main contact, and its match keys.

    ``clean`` is the output of :func:`validate` — this function does not
    re-validate, so an invalid country cannot reach here by accident. Commits
    unless a session was supplied (then it only flushes, leaving the caller's
    transaction intact).
    """
    from includes.dashboard.database import get_session
    from includes.dashboard.models import Contact, Supplier
    from includes.dashboard.supplier_matching import rebuild_match_keys

    own_session = session is None
    if own_session:
        session = get_session()
    try:
        supplier = Supplier(
            name=clean["name"],
            source="manual",                       # 'netsuite' | 'web' | 'manual'
            modified_by=f"user:{user_email}",
            modified_at=datetime.now(timezone.utc),
            **{
                key: (clean.get(key) or None)
                for key in _COLUMN_KEYS
                if key != "name"
            },
        )
        session.add(supplier)
        session.flush()                            # assign supplier.id

        contact_name = clean.get("contact_name") or ""
        email = clean.get("email") or ""
        phone = clean.get("phone") or ""
        if contact_name or email or phone:
            # Both halves are needed: this row is what inbound-email matching
            # indexes, the JSONB below is what the RFQ/NetSuite forms prefill.
            session.add(Contact(
                supplier_id=supplier.id,
                label="Main",
                fullname=contact_name or None,
                email=email or None,
                phone=phone or None,
            ))
            supplier.contacts = [{
                "name": contact_name,
                "email": email,
                "phone": phone,
                "label": "Main",
            }]

        rebuild_match_keys(session, supplier)

        if near_misses:
            # Queue what we rejected so the dedup review can merge it later —
            # the same treatment the web-discovery path gives its rejects.
            from includes.dashboard.supplier_dedup import nominate_near_misses

            try:
                # Savepoint: a failure here must not poison the transaction that
                # is holding the user's new supplier, nor lose the row with it.
                with session.begin_nested():
                    nominate_near_misses(session, supplier, near_misses)
            except Exception:
                logger.exception("[supplier-create] could not nominate near misses")

        session.flush()
        if own_session:
            session.commit()
        logger.info(
            "[supplier-create] created %r (id=%s) by %s",
            supplier.name, supplier.id, user_email,
        )
        return supplier
    except Exception:
        session.rollback()
        raise
    finally:
        if own_session:
            session.close()
