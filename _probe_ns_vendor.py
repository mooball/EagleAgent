"""Probe NetSuite REST metadata for vendor + swagger operations (READ-ONLY)."""
import json
from includes.netsuite.client import NetSuiteClient

c = NetSuiteClient()

# 1) JSON schema
headers = c._headers()
headers["Accept"] = "application/schema+json"
r = c._session.get(f"{c._base_url}/record/v1/metadata-catalog/vendor",
                   headers=headers, timeout=(10, 300))
print("schema HTTP", r.status_code, "bytes", len(r.content))
r.raise_for_status()
data = r.json()
with open("_ns_vendor_schema.json", "w") as f:
    json.dump(data, f)
print("saved _ns_vendor_schema.json")

props = data.get("properties") or {}
print("top-level keys:", list(data.keys()))
print("total properties:", len(props))

# mandatory markers
mandatory = {k: v for k, v in props.items() if isinstance(v, dict) and v.get("mandatory")}
print("mandatory-flagged properties:", list(mandatory.keys()))
req = data.get("required")
print("top-level required:", req)

wanted = ["companyName", "isPerson", "url", "category", "terms", "phone", "email",
          "currency", "legalName", "taxItem", "currencyList", "subsidiary",
          "entityId", "custentity_go_souce_email_name", "custentity_go_souce_email_address"]
for name in wanted:
    v = props.get(name)
    if v is None:
        print(f"  [{name}]: NOT FOUND")
    else:
        t = v.get("type") or v.get("$ref") or ""
        print(f"  [{name}]: type={t} mandatory={v.get('mandatory')} "
              f"nullable={v.get('nullable')} readonly={v.get('readOnly')}")

# 2) swagger operations
h2 = c._headers()
h2["Accept"] = "application/swagger+json"
r2 = c._session.get(f"{c._base_url}/record/v1/metadata-catalog/vendor",
                    headers=h2, timeout=(10, 300))
print("\nswagger HTTP", r2.status_code)
r2.raise_for_status()
sw = r2.json()
for p, ops in sw.get("paths", {}).items():
    print(p, "->", list(ops.keys()))
