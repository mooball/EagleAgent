"""Fetch swagger+json for inventoryitem — operations listing (READ-ONLY)."""
import json
from includes.netsuite.client import NetSuiteClient
c = NetSuiteClient()
headers = c._headers()
headers["Accept"] = "application/swagger+json"
r = c._session.get(f"{c._base_url}/record/v1/metadata-catalog/inventoryitem",
                   headers=headers, timeout=300)
print("HTTP", r.status_code, "bytes", len(r.content))
with open("_ns_inventoryitem_swagger.json", "w") as f:
    f.write(r.text)
data = r.json()
paths = data.get("paths", {})
for p, ops in paths.items():
    print(p, "->", list(ops.keys()))
