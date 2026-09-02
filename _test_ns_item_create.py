"""Test the NetSuite item creation flow against the live account.

Creates/updates EGTEST-* items linked to the test vendor, verifies via
SuiteQL, then cleans up the local DB rows it wrote (NetSuite items are
kept — DELETE isn't supported via REST; clean them in the UI if needed).

Run: uv run python _test_ns_item_create.py
"""

from sqlalchemy import text

from includes.dashboard.database import get_session
from includes.netsuite.departments import Department
from includes.netsuite.records.item import (
    ensure_item_with_vendor,
    find_item_by_part_number,
    find_brand_by_name,
    get_or_create_brand,
)

TEST_VENDOR_ID = "6730"  # "Test Supplier Bill"
TEST_BRAND = "EGTEST Brand"
TEST_ITEMS = [
    {
        "part_number": "EGTEST-ITEM-001",
        "description": "EGTEST Test Item 1",
        "purchase_price": 123.45,
        "price_currency": "AUD",
    },
    {
        "part_number": "EGTEST-ITEM-002",
        "description": "EGTEST Test Item 2",
        "purchase_price": 88.00,
        "price_currency": "AUD",
    },
]


def ns_verify(part_number: str) -> None:
    from includes.netsuite.client import NetSuiteClient
    client = NetSuiteClient()
    rows = client.suiteql(
        "SELECT id, itemid, class, department, custitem_brand "
        "FROM item WHERE UPPER(itemid) = UPPER('" + part_number.replace("'", "''") + "')",
        limit=5,
    )
    for row in rows:
        print("  NetSuite:", row)
        price_rows = client.suiteql(
            "SELECT vendor, preferredvendor, purchaseprice "
            f"FROM itemvendor WHERE item = '{row['id']}'",
            limit=5,
        )
        for p in price_rows:
            print("  vendor line:", p)
    if not rows:
        print("  NetSuite: NOT FOUND")


def main() -> None:
    print("== 1. Brand ==")
    brand_existed = find_brand_by_name(TEST_BRAND) is not None
    brand_result = get_or_create_brand(TEST_BRAND)
    print(f"  get_or_create_brand({TEST_BRAND!r}) -> {brand_result}")
    if not brand_result.success:
        raise SystemExit("Brand setup failed")
    print(f"  (brand existed before run: {brand_existed})")

    print("\n== 2. Items ==")
    created_or_updated = []
    for spec in TEST_ITEMS:
        pn = spec["part_number"]
        existing = find_item_by_part_number(pn)
        print(f"\n-- {pn} (existing NS id: {existing})")
        result = ensure_item_with_vendor(
            part_number=pn,
            description=spec["description"],
            brand_name=TEST_BRAND,
            vendor_netsuite_id=TEST_VENDOR_ID,
            purchase_price=spec["purchase_price"],
            price_currency=spec["price_currency"],
            department_id=Department.OTHER_PARTS.netsuite_id,
            external_id=pn,
            writeback_local=True,
        )
        print(f"  ensure_item_with_vendor -> success={result.success} "
              f"ns_id={result.netsuite_id} error={result.error} error_code={result.error_code}")
        created_or_updated.append((pn, result))
        ns_verify(pn)

    print("\n== 3. Re-run one item to exercise the update path ==")
    pn = TEST_ITEMS[0]["part_number"]
    existing = find_item_by_part_number(pn)
    print(f"  {pn} ns id before re-run: {existing}")
    result = ensure_item_with_vendor(
        part_number=pn,
        description=TEST_ITEMS[0]["description"],
        brand_name=TEST_BRAND,
        vendor_netsuite_id=TEST_VENDOR_ID,
        purchase_price=123.99,
        price_currency="AUD",
        external_id=pn,
        writeback_local=True,
    )
    print(f"  re-run -> success={result.success} ns_id={result.netsuite_id} "
          f"error={result.error} error_code={result.error_code}")
    ns_verify(pn)

    print("\n== 4. Local DB cleanup (products/brands written back) ==")
    session = get_session()
    try:
        del_p = session.execute(
            text("DELETE FROM products WHERE part_number LIKE 'EGTEST-%'")
        ).rowcount
        del_b = 0
        if not brand_existed:
            del_b = session.execute(
                text("DELETE FROM brands WHERE name = 'EGTEST Brand'")
            ).rowcount
        session.commit()
        print(f"  deleted {del_p} local product row(s), {del_b} local brand row(s)")
    finally:
        session.close()

    print("\nDone. NetSuite keeps the EGTEST-* items (REST has no delete).")


if __name__ == "__main__":
    main()
