"""Read-only probe: full error body for DELETE on nonexistent inventoryitem ids."""
from includes.netsuite.client import NetSuiteClient
c = NetSuiteClient()
headers = c._headers()
for rid in ("999999999", "123456789"):
    r = c._session.delete(f"{c._base_url}/record/v1/inventoryitem/{rid}",
                          headers=headers, timeout=60)
    print(f"DELETE inventoryitem/{rid} -> HTTP {r.status_code}")
    print(r.text[:600])
    print("-" * 60)
# Also see if a plain GET on the metadata-catalog/inventoryitem (no schema header) shows operations
h2 = c._headers()
r2 = c._session.get(f"{c._base_url}/record/v1/metadata-catalog/inventoryitem", headers=h2, timeout=120)
print("plain catalog item:", r2.status_code, r2.text[:800])
