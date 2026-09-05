"""Test the NetSuite vendor creation flow against the live account.

Creates EGTEST vendors (AU + international), verifies fields, exercises
find/ensure paths, then DELETES them via REST (vendor delete is supported)
and cleans up the local supplier writeback rows.

Run: uv run python _test_ns_vendor_create.py
"""

from sqlalchemy import text

from includes.dashboard.database import get_session
from includes.netsuite.client import NetSuiteClient
from includes.netsuite.records.vendor import (
    create_vendor,
    ensure_vendor,
    find_vendor_by_entity_id,
    resolve_tax_item,
)

TEST_VENDORS = [
    {"name": "EGTEST VENDOR AU", "country": "AU", "email": "egtest-au@example.com", "url": "https://example.com.au"},
    {"name": "EGTEST VENDOR INT", "country": "US", "email": "egtest-int@example.com"},
]


def ns_verify(company_name: str) -> None:
    client = NetSuiteClient()
    ns_id = find_vendor_by_entity_id(company_name)
    if not ns_id:
        print(f"  NetSuite: NOT FOUND for {company_name}")
        return
    v = client.get_record("vendor", ns_id)
    print(f"  NetSuite {ns_id}: companyName={v.get('companyName')} "
          f"isPerson={v.get('isPerson')} taxItem={v.get('taxItem')} "
          f"currency={v.get('currency')} currencyList={v.get('currencyList')}")


def main() -> None:
    client = NetSuiteClient()

    print("== 1. Tax rule ==")
    print(f"  resolve_tax_item('AU') -> {resolve_tax_item('AU')}")
    print(f"  resolve_tax_item('US') -> {resolve_tax_item('US')}")

    created_ids = []
    print("\n== 2. Create test vendors ==")
    for spec in TEST_VENDORS:
        result = create_vendor(
            company_name=spec["name"],
            country=spec["country"],
            email=spec.get("email"),
            url=spec.get("url"),
            external_id=spec["name"],
            writeback_local=True,
        )
        print(f"  create_vendor({spec['name']!r}) -> success={result.success} "
              f"ns_id={result.netsuite_id} error={result.error} error_code={result.error_code}")
        if result.success:
            created_ids.append(result.netsuite_id)
        ns_verify(spec["name"])

    print("\n== 3. ensure_vendor (should find existing, not duplicate) ==")
    for spec in TEST_VENDORS:
        result = ensure_vendor(spec["name"], country=spec["country"], email=spec.get("email"))
        print(f"  ensure_vendor({spec['name']!r}) -> success={result.success} "
              f"ns_id={result.netsuite_id} error={result.error}")

    print("\n== 4. Cleanup: DELETE test vendors via REST ==")
    for ns_id in created_ids:
        r = client._session.delete(
            f"{client._base_url}/record/v1/vendor/{ns_id}",
            headers=client._headers(), timeout=60,
        )
        print(f"  DELETE vendor/{ns_id} -> HTTP {r.status_code}")

    print("\n== 5. Local DB cleanup (supplier writeback rows) ==")
    session = get_session()
    try:
        deleted = session.execute(
            text("DELETE FROM suppliers WHERE name LIKE 'EGTEST VENDOR %'")
        ).rowcount
        session.commit()
        print(f"  deleted {deleted} local supplier row(s)")
    finally:
        session.close()

    print("\nDone.")


if __name__ == "__main__":
    main()
