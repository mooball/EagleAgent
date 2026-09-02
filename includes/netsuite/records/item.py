"""Create and update NetSuite inventory items and brands (REST).

Replicates the behaviour of the legacy Suitelet import
(``ajg_sl_eagle_exp_import_create_items_with_currency_conversion.js``):

  - exact ``itemid`` (part number) lookup, create if missing
  - vendor pricing via the ``itemVendor`` sublist (preferred vendor)
  - brand via ``custitem_brand`` (ref into ``customrecord_brands``)
  - tax codes default from the vendor's ``taxitem``
  - purchase prices stored in the vendor's currency, converted from the
    caller's currency via :mod:`includes.currency`

Account facts (probed against the live REST API):

  - ``inventoryItem`` supports get/put/post/patch — **DELETE is not
    supported** (swagger: no delete operation; live DELETE returns 400
    "There are no records of this type").
  - The ``itemVendor`` sublist is exposed as ``itemVendor.items[]`` with
    fields ``vendor``, ``preferredVendor``, ``purchasePrice``.
  - ``externalId`` is writable — use it for idempotent test upserts.
"""

import logging
from typing import Optional

from sqlalchemy.exc import SQLAlchemyError

from includes.currency import convert
from includes.dashboard.database import get_session
from includes.dashboard.models import Brand, Product
from includes.netsuite.client import NetSuiteClient
from .base import CreateResult

logger = logging.getLogger(__name__)

#: Hard-coded item class used by the legacy Suitelet import.
ITEM_CLASS_ID = "1"

#: NetSuite base currency ISO code for this account (currency internal id 1).
BASE_CURRENCY = "AUD"


def _suiteql_literal(value: str) -> str:
    """Escape a string literal for inlining into SuiteQL."""
    return value.replace("'", "''")


# ── Lookups ────────────────────────────────────────────────────────────────

def find_item_by_part_number(part_number: str) -> Optional[str]:
    """Return the NetSuite internal ID of the item whose itemid equals
    ``part_number`` (case-insensitive, exact). None if not found.
    """
    client = NetSuiteClient()
    rows = client.suiteql(
        "SELECT id FROM item "
        f"WHERE UPPER(itemid) = UPPER('{_suiteql_literal(part_number)}')",
        limit=5,
    )
    return rows[0].get("id") if rows else None


def find_brand_by_name(name: str) -> Optional[str]:
    """Return the internal ID of a brand custom record by exact name."""
    client = NetSuiteClient()
    rows = client.suiteql(
        "SELECT id FROM customrecord_brands "
        f"WHERE UPPER(name) = UPPER('{_suiteql_literal(name)}') ORDER BY id",
        limit=5,
    )
    return rows[0].get("id") if rows else None


def get_vendor_context(vendor_netsuite_id: str) -> dict:
    """Fetch the fields needed for item creation from a vendor record.

    Returns ``{"tax_item_id": str | None, "currency": str}`` where
    ``currency`` is the vendor's currency ISO symbol (e.g. "AUD", "USD").
    """
    client = NetSuiteClient()
    vendor = client.get_record("vendor", str(vendor_netsuite_id))
    tax_item_id = (vendor.get("taxItem") or {}).get("id")
    currency_ref = vendor.get("currency") or {}
    symbol = (currency_ref.get("refName") or "").strip()
    if not (symbol and len(symbol) == 3 and symbol.isalpha()):
        symbol = ""
        if currency_ref.get("id"):
            rows = client.suiteql(
                "SELECT symbol FROM currency "
                f"WHERE id = '{_suiteql_literal(str(currency_ref['id']))}'",
                limit=1,
            )
            if rows:
                symbol = (rows[0].get("symbol") or "").strip()
        if not symbol:
            symbol = BASE_CURRENCY
    return {"tax_item_id": tax_item_id, "currency": symbol.upper()}


# ── Brand ──────────────────────────────────────────────────────────────────

def create_brand(name: str, writeback_local: bool = True) -> CreateResult:
    """Create a brand custom record in NetSuite.

    Args:
        name: Brand name (must not already exist — use get_or_create_brand).
        writeback_local: Also record the brand in the local ``brands`` table.

    Returns:
        CreateResult with ``netsuite_id`` on success.
    """
    client = NetSuiteClient()
    try:
        netsuite_id = client.create_record("customrecord_brands", {"name": name})
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error("Failed to create brand %s: %s", name, exc)
        return CreateResult(
            success=False, error=str(exc), error_code=status_code,
            record_type="brand",
        )

    if writeback_local:
        _writeback_brand_sync(name, netsuite_id)

    return CreateResult(success=True, netsuite_id=netsuite_id, record_type="brand")


def get_or_create_brand(name: str, writeback_local: bool = True) -> CreateResult:
    """Resolve a brand by exact name, creating it if missing."""
    existing = find_brand_by_name(name)
    if existing:
        if writeback_local:
            _writeback_brand_sync(name, existing)
        return CreateResult(success=True, netsuite_id=existing, record_type="brand")
    return create_brand(name, writeback_local=writeback_local)


# ── Item ───────────────────────────────────────────────────────────────────

def create_item(
    part_number: str,
    description: str,
    brand_netsuite_id: str,
    vendor_netsuite_id: str,
    purchase_price: float,
    price_currency: str = "AUD",
    tax_item_id: Optional[str] = None,
    department_id: Optional[str] = None,
    external_id: Optional[str] = None,
    writeback_local: bool = True,
) -> CreateResult:
    """Create an inventory item in NetSuite linked to a vendor.

    Vendor and brand are mandatory — if either is missing no item is
    created. The purchase price is converted into the vendor's currency
    (NetSuite stores item vendor prices in the vendor's currency). Tax
    codes default from the vendor's taxitem.

    Returns:
        CreateResult with ``netsuite_id`` on success.
    """
    if not part_number or not brand_netsuite_id or not vendor_netsuite_id:
        return CreateResult(
            success=False,
            error="part_number, brand and vendor are all required to create an item",
            record_type="inventoryitem",
        )

    client = NetSuiteClient()

    try:
        vendor_ctx = get_vendor_context(vendor_netsuite_id)
    except Exception as exc:
        logger.error("Failed to fetch vendor %s: %s", vendor_netsuite_id, exc)
        return CreateResult(
            success=False,
            error=f"Failed to fetch vendor {vendor_netsuite_id}: {exc}",
            record_type="inventoryitem",
        )

    try:
        converted = round(
            convert(float(purchase_price), price_currency.upper(), vendor_ctx["currency"]),
            2,
        )
    except Exception as exc:
        logger.error("Currency conversion failed for %s: %s", part_number, exc)
        return CreateResult(
            success=False,
            error=f"Currency conversion failed: {exc}",
            record_type="inventoryitem",
        )

    tax_item = tax_item_id or vendor_ctx.get("tax_item_id")

    payload: dict = {
        "itemId": part_number,
        "class": {"id": ITEM_CLASS_ID},
        "salesDescription": description,
        "purchaseDescription": description,
        "custitem_brand": {"id": str(brand_netsuite_id)},
        "itemVendor": {
            "items": [
                {
                    "vendor": {"id": str(vendor_netsuite_id)},
                    "preferredVendor": True,
                    "purchasePrice": converted,
                }
            ]
        },
    }
    if tax_item:
        payload["purchaseTaxCode"] = {"id": str(tax_item)}
        payload["salesTaxCode"] = {"id": str(tax_item)}
    if department_id:
        payload["department"] = {"id": str(department_id)}
    if external_id:
        payload["externalId"] = external_id

    try:
        netsuite_id = client.create_record("inventoryitem", payload)
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error("Failed to create inventoryitem %s: %s", part_number, exc)
        return CreateResult(
            success=False, error=str(exc), error_code=status_code,
            record_type="inventoryitem",
        )

    if writeback_local:
        _writeback_product_sync(part_number, description, brand_netsuite_id, netsuite_id)

    logger.info("Created inventoryitem %s (NS id %s)", part_number, netsuite_id)
    return CreateResult(success=True, netsuite_id=netsuite_id, record_type="inventoryitem")


def set_vendor_price(
    item_netsuite_id: str,
    vendor_netsuite_id: str,
    purchase_price: float,
    price_currency: str = "AUD",
) -> CreateResult:
    """Set/refresh the vendor purchase price on an existing item.

    Mirrors the Suitelet's delete-and-re-add of the vendor line: NetSuite
    REST ignores purchasePrice changes on existing itemVendor lines, so we
    clear the sublist and re-add all lines (keeping other vendors' lines
    and prices) with the target vendor's price updated.

    Requires two PATCH calls:
      1. PATCH ?replace=itemVendor with an empty items list (clears lines)
      2. PATCH with the rebuilt items list (adds lines, prices apply)
    """
    client = NetSuiteClient()
    try:
        vendor_ctx = get_vendor_context(vendor_netsuite_id)
        converted = round(
            convert(float(purchase_price), price_currency.upper(), vendor_ctx["currency"]),
            2,
        )
    except Exception as exc:
        return CreateResult(
            success=False,
            error=f"Vendor lookup/conversion failed: {exc}",
            record_type="inventoryitem",
        )

    # Read current vendor lines so we can preserve lines for other vendors
    try:
        item_data = client.get(
            f"record/v1/inventoryitem/{item_netsuite_id}?expandSubResources=true"
        ).json()
    except Exception as exc:
        return CreateResult(
            success=False,
            error=f"Failed to read item {item_netsuite_id}: {exc}",
            record_type="inventoryitem",
        )

    rebuilt = []
    for line in (item_data.get("itemVendor") or {}).get("items", []) or []:
        vid = str((line.get("vendor") or {}).get("id") or "")
        if not vid or vid == str(vendor_netsuite_id):
            continue
        entry: dict = {
            "vendor": {"id": vid},
            "preferredVendor": bool(line.get("preferredVendor")),
        }
        if line.get("purchasePrice") is not None:
            entry["purchasePrice"] = line["purchasePrice"]
        rebuilt.append(entry)

    rebuilt.append({
        "vendor": {"id": str(vendor_netsuite_id)},
        "preferredVendor": True,
        "purchasePrice": converted,
    })

    try:
        # Step 1: clear the sublist
        client.update_record(
            "inventoryitem",
            str(item_netsuite_id),
            {"itemVendor": {"items": []}},
            params={"replace": "itemVendor"},
        )
        # Step 2: re-add all lines (prices apply on add)
        client.update_record(
            "inventoryitem",
            str(item_netsuite_id),
            {"itemVendor": {"items": rebuilt}},
        )
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error("Failed to update vendor price on item %s: %s", item_netsuite_id, exc)
        return CreateResult(
            success=False, error=str(exc), error_code=status_code,
            record_type="inventoryitem",
        )

    logger.info("Updated vendor price on item %s", item_netsuite_id)
    return CreateResult(success=True, netsuite_id=str(item_netsuite_id), record_type="inventoryitem")


def ensure_item_with_vendor(
    part_number: str,
    description: str,
    brand_name: str,
    vendor_netsuite_id: str,
    purchase_price: float,
    price_currency: str = "AUD",
    tax_item_id: Optional[str] = None,
    department_id: Optional[str] = None,
    external_id: Optional[str] = None,
    writeback_local: bool = True,
) -> CreateResult:
    """High-level flow: make sure an item exists in NetSuite for
    ``part_number`` with the given brand/vendor pricing.

    1. Resolve the brand (create if missing — brand is mandatory).
    2. Find an existing item: local smart product match first, then a
       direct NetSuite itemid lookup.
    3. Existing → refresh vendor price. Missing → create the item.
    """
    if not part_number or not brand_name or not vendor_netsuite_id:
        return CreateResult(
            success=False,
            error="part_number, brand_name and vendor are all required",
            record_type="inventoryitem",
        )

    brand_result = get_or_create_brand(brand_name, writeback_local=writeback_local)
    if not brand_result.success:
        return brand_result
    brand_ns_id = brand_result.netsuite_id

    # 1) Local smart match (most-purchased product wins)
    existing_id = None
    if writeback_local:
        existing_id = _local_product_netsuite_id(part_number)

    # 2) NetSuite itemid lookup
    if not existing_id:
        existing_id = find_item_by_part_number(part_number)

    if existing_id:
        # Existing item: write back the link locally if missing
        if writeback_local:
            _writeback_product_sync(part_number, description, brand_ns_id, existing_id)
        return set_vendor_price(
            existing_id, vendor_netsuite_id, purchase_price,
            price_currency=price_currency,
        )

    return create_item(
        part_number=part_number,
        description=description,
        brand_netsuite_id=brand_ns_id,
        vendor_netsuite_id=vendor_netsuite_id,
        purchase_price=purchase_price,
        price_currency=price_currency,
        tax_item_id=tax_item_id,
        department_id=department_id,
        external_id=external_id,
        writeback_local=writeback_local,
    )


# ── Local writeback ────────────────────────────────────────────────────────

def _local_product_netsuite_id(part_number: str) -> Optional[str]:
    """NetSuite ID of the local product matching part_number (smart match)."""
    from includes.tools.product_tools import _find_product_by_code
    try:
        hit = _find_product_by_code(part_number)
    except Exception:
        return None
    if not hit:
        return None
    session = get_session()
    try:
        product = session.query(Product).get(hit["id"])
        return product.netsuite_id if product else None
    finally:
        session.close()


def _writeback_product_sync(
    part_number: str, description: str, brand_netsuite_id: str, netsuite_id: str
) -> None:
    """Record the new NetSuite item ID on the matching local product row
    (creating a minimal row if none exists)."""
    from includes.tools.product_tools import _find_product_by_code

    session = get_session()
    try:
        brand_name = None
        brand = session.query(Brand).filter(Brand.netsuite_id == str(brand_netsuite_id)).first()
        if brand:
            brand_name = brand.name

        product = None
        try:
            hit = _find_product_by_code(part_number)
            if hit:
                product = session.query(Product).get(hit["id"])
        except Exception:
            product = None

        if product is None:
            product = Product(part_number=part_number, netsuite_id=str(netsuite_id))
            session.add(product)
        elif not product.netsuite_id:
            product.netsuite_id = str(netsuite_id)
        if description and not product.description:
            product.description = description
        if brand_name and not product.brand:
            product.brand = brand_name
        session.commit()
    except SQLAlchemyError as e:
        session.rollback()
        logger.warning("Product writeback failed for %s: %s", part_number, e)
    finally:
        session.close()


def _writeback_brand_sync(name: str, netsuite_id: str) -> None:
    """Record the brand in the local brands table if not already present."""
    session = get_session()
    try:
        existing = session.query(Brand).filter(Brand.netsuite_id == str(netsuite_id)).first()
        if existing:
            return
        session.add(Brand(netsuite_id=str(netsuite_id), name=name))
        session.commit()
    except SQLAlchemyError as e:
        session.rollback()
        logger.warning("Brand writeback failed for %s: %s", name, e)
    finally:
        session.close()
