"""Create NetSuite vendors (suppliers) via REST, link local suppliers.

Mirrors the behaviour needed to push local suppliers into NetSuite, with
conservative defaults — lookups (category, terms) are only sent when the
caller provides them.

Account facts (probed 2026-09-03, account 794882):
  - POST /record/v1/vendor is supported.
  - DELETE /record/v1/vendor/{id} route exists but the current integration
    role lacks the "Lists → Suppliers" permission → returns 403. Test
    vendors must be cleaned up in the NetSuite UI (or the role upgraded).
  - companyName is the required field; subsidiary is set explicitly (1).
  - vendor "Tax Code" in the UI is the tax ITEM (taxItem ref).
  - SuiteQL/REST cannot list term/vendorCategory/currency/taxitem record
    types; lookup id↔name maps are derived from existing vendors via
    BUILTIN.DF (see vendor_lookup_options).
  - The Go Source custom field IDs contain the literal typo "souce".
"""

import logging
from typing import Optional

from sqlalchemy.exc import SQLAlchemyError

from includes.dashboard.database import get_session
from includes.dashboard.models import Supplier
from includes.netsuite.client import NetSuiteClient
from .base import CreateResult

logger = logging.getLogger(__name__)

#: NetSuite subsidiary internal id (single-subsidiary account).
SUBSIDIARY_ID = "1"

#: Vendor custom form — "1 Master Supplier Form". NetSuite defaults to
#: "JCurve Vendor Form" (16) when omitted, which makes the record layout
#: look odd. Form ids from the NetSuite dropdown: 97 Master Supplier,
#: 114 Tyre Supplier, 55 JCurve Payroll, 16 JCurve Vendor,
#: -20 Standard Supplier, 30 Wizard|Vendor.
#: NOTE: form 97 requires the integration role to be in the form's
#: allowed roles (verified working 2026-09-03 after the role was added).
CUSTOM_FORM_MASTER_SUPPLIER: Optional[str] = "97"

#: Base currency (AUD) internal id.
CURRENCY_AUD = "1"

#: Tax items — local vendors get GST, international vendors FREE.
TAX_ITEM_GST = "15"
TAX_ITEM_FREE = "9"

#: Known currency symbols → internal IDs (probed from vendor data).
_CURRENCY_IDS = {
    "AUD": "1", "USD": "2", "CAD": "3", "EUR": "4",
    "NZD": "5", "GBP": "6", "JPY": "8", "SGD": "9",
    "PHP": "10", "ZAR": "11",
}


def _suiteql_literal(value: str) -> str:
    """Escape a string literal for inlining into SuiteQL."""
    return value.replace("'", "''")


# ── Lookups ────────────────────────────────────────────────────────────────

def vendor_lookup_options() -> dict:
    """id↔name dictionaries for vendor lookup fields, derived from existing
    vendor data (the underlying record types aren't directly queryable).

    Returns {"terms": {name: id}, "categories": {name: id},
             "tax_items": {name: id}, "currencies": {name: id}}.
    """
    client = NetSuiteClient()
    options = {
        "terms": {},
        "categories": {},
        "tax_items": {},
        "currencies": {},
    }
    for key, field in [
        ("terms", "terms"),
        ("categories", "category"),
        ("tax_items", "taxitem"),
        ("currencies", "currency"),
    ]:
        try:
            rows = client.suiteql(
                f"SELECT DISTINCT {field}, BUILTIN.DF({field}) AS name "
                f"FROM vendor WHERE {field} IS NOT NULL"
            )
        except Exception:
            continue
        for row in rows:
            name = (row.get("name") or "").strip()
            rid = row.get(field)
            if name and rid:
                options[key].setdefault(name, str(rid))
    return options


def resolve_tax_item(country: Optional[str]) -> str:
    """Tax item for a vendor: GST for AU, FREE for international."""
    if country and country.strip().upper() == "AU":
        return TAX_ITEM_GST
    return TAX_ITEM_FREE


def resolve_currency(symbol: Optional[str]) -> str:
    """Currency internal id for an ISO symbol (default AUD)."""
    if not symbol:
        return CURRENCY_AUD
    sym = symbol.strip().upper()
    if sym in _CURRENCY_IDS:
        return _CURRENCY_IDS[sym]
    compact = sym.replace(" ", "")
    for name, cid in vendor_lookup_options().get("currencies", {}).items():
        if name.upper() == sym or name.upper().replace(" ", "") == compact:
            return cid
    return CURRENCY_AUD


def find_vendor_by_entity_id(company_name: str) -> Optional[str]:
    """NetSuite internal ID of the vendor whose entityId equals name."""
    client = NetSuiteClient()
    rows = client.suiteql(
        "SELECT id FROM vendor "
        f"WHERE UPPER(entityid) = UPPER('{_suiteql_literal(company_name)}')",
        limit=5,
    )
    return rows[0].get("id") if rows else None


# ── Create ─────────────────────────────────────────────────────────────────

def create_vendor(
    company_name: str,
    *,
    is_person: bool = False,
    url: Optional[str] = None,
    category_id: Optional[str] = None,
    terms_id: Optional[str] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    currency_id: Optional[str] = None,
    legal_name: Optional[str] = None,
    tax_item_id: Optional[str] = None,
    country: Optional[str] = None,
    go_source_email: Optional[str] = None,
    external_id: Optional[str] = None,
    custom_form_id: Optional[str] = None,
    writeback_local: bool = True,
) -> CreateResult:
    """Create a vendor in NetSuite.

    Only companyName is required. Category and terms are only sent when
    explicitly provided. Defaults: Master Supplier custom form (97),
    Company type, AUD currency, legal name = company name, tax item GST
    (AU) / FREE (international). The Go Source custom email fields default
    from ``go_source_email`` or ``email``.
    """
    if not company_name or not company_name.strip():
        return CreateResult(
            success=False,
            error="company_name is required to create a vendor",
            record_type="vendor",
        )

    client = NetSuiteClient()

    payload: dict = {
        "companyName": company_name.strip(),
        "subsidiary": {"id": SUBSIDIARY_ID},
        "isPerson": bool(is_person),
        "legalName": (legal_name or company_name).strip(),
        "taxItem": {"id": str(tax_item_id or resolve_tax_item(country))},
        "currency": {"id": str(currency_id or CURRENCY_AUD)},
        "currencyList": {
            "items": [{"currency": {"id": str(currency_id or CURRENCY_AUD)}}]
        },
    }
    if url:
        payload["url"] = url.strip()
    form_id = custom_form_id or CUSTOM_FORM_MASTER_SUPPLIER
    if form_id:
        payload["customForm"] = {"id": str(form_id)}
    if category_id:
        payload["category"] = {"id": str(category_id)}
    if terms_id:
        payload["terms"] = {"id": str(terms_id)}
    if phone:
        payload["phone"] = phone.strip()
    if email:
        payload["email"] = email.strip()
    go_source = (go_source_email or email or "").strip()
    if go_source:
        payload["custentity_go_souce_email_name"] = go_source
        payload["custentity_go_souce_email_address"] = go_source
    if external_id:
        payload["externalId"] = external_id

    try:
        netsuite_id = client.create_record("vendor", payload)
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error("Failed to create vendor %s: %s", company_name, exc)
        return CreateResult(
            success=False, error=str(exc), error_code=status_code,
            record_type="vendor",
        )

    if writeback_local:
        _writeback_supplier_sync(
            company_name, netsuite_id,
            url=url, country=country,
            currency_symbol=_currency_symbol(currency_id),
        )

    logger.info("Created vendor %s (NS id %s)", company_name, netsuite_id)
    return CreateResult(success=True, netsuite_id=netsuite_id, record_type="vendor")


def ensure_vendor(company_name: str, **kwargs) -> CreateResult:
    """Find-or-create a vendor by exact entityId match."""
    existing = find_vendor_by_entity_id(company_name)
    if existing:
        if kwargs.pop("writeback_local", True):
            _writeback_supplier_sync(
                company_name, existing,
                url=kwargs.get("url"),
                country=kwargs.get("country"),
                currency_symbol=_currency_symbol(kwargs.get("currency_id")),
            )
        return CreateResult(success=True, netsuite_id=existing, record_type="vendor")
    return create_vendor(company_name, **kwargs)


# ── Local writeback ────────────────────────────────────────────────────────

def _currency_symbol(currency_id: Optional[str]) -> Optional[str]:
    if not currency_id:
        return "AUD"
    for sym, cid in _CURRENCY_IDS.items():
        if cid == str(currency_id):
            return sym
    for name, cid in vendor_lookup_options().get("currencies", {}).items():
        if cid == str(currency_id):
            return name
    return None


def _writeback_supplier_sync(
    company_name: str,
    netsuite_id: str,
    url: Optional[str] = None,
    country: Optional[str] = None,
    currency_symbol: Optional[str] = None,
) -> None:
    """Record the new vendor ID on the matching local supplier row
    (creating a minimal row if none exists)."""
    session = get_session()
    try:
        existing = (
            session.query(Supplier)
            .filter(Supplier.netsuite_id == str(netsuite_id))
            .first()
        )
        if existing is None:
            existing = (
                session.query(Supplier)
                .filter(
                    Supplier.name.ilike(company_name.strip()),
                    Supplier.netsuite_id.is_(None),
                )
                .first()
            )
        if existing is None:
            existing = Supplier(
                netsuite_id=str(netsuite_id),
                name=company_name.strip(),
                source="netsuite",
            )
            session.add(existing)
        else:
            existing.netsuite_id = str(netsuite_id)
        if url and not existing.url:
            existing.url = url.strip()
        if country and not existing.country:
            existing.country = country.strip()
        if currency_symbol and not existing.currency:
            existing.currency = currency_symbol
        existing.source = "netsuite"
        existing.modified_by = "netsuite"
        session.commit()
    except SQLAlchemyError as e:
        session.rollback()
        logger.warning("Supplier writeback failed for %s: %s", company_name, e)
    finally:
        session.close()
