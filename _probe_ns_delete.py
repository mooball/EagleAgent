"""Safe probe: does REST support DELETE on inventoryitem?
DELETE a non-existent internalid — expect 404 (supported) or 405 (not supported).
Deletes nothing real."""
from includes.netsuite.client import NetSuiteClient
c = NetSuiteClient()
headers = c._headers()
for rid in ("999999999", "1"):
    r = c._session.delete(f"{c._base_url}/record/v1/inventoryitem/{rid}",
                          headers=headers, timeout=60)
    print(f"DELETE inventoryitem/{rid} -> HTTP {r.status_code}: {r.text[:200]}")
