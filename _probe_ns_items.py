"""Probe NetSuite REST metadata for inventoryitem + customrecord_brands (READ-ONLY)."""
import json
from includes.netsuite.client import NetSuiteClient

c = NetSuiteClient()

def probe(record_type, timeout=300):
    print(f"\n{'='*70}\n{record_type}\n{'='*70}", flush=True)
    headers = c._headers()
    headers["Accept"] = "application/schema+json"
    r = c._session.get(f"{c._base_url}/record/v1/metadata-catalog/{record_type}",
                       headers=headers, timeout=(10, timeout))
    print("HTTP", r.status_code, "bytes:", len(r.content), flush=True)
    if r.status_code == 404:
        print("not found as", record_type)
        return None
    r.raise_for_status()
    data = r.json()
    fn = f"_ns_{record_type.replace('customrecord_','custom_')}_schema.json"
    with open(fn, "w") as f:
        json.dump(data, f)
    print("saved to", fn)
    ops = data.get("operations") or {}
    print("operations:", json.dumps(ops))
    return data

data = probe("inventoryitem")
if data:
    props = data.get("properties") or {}
    mandatory = {k: v for k, v in props.items()
                 if isinstance(v, dict) and v.get("mandatory")}
    print(f"\nproperties: {len(props)}, mandatory: {len(mandatory)}")
    for k, v in list(mandatory.items())[:50]:
        print("  MANDATORY", k, "->", v.get("type") or v.get("$ref"))
    for name in ("itemId", "class", "salesDescription", "purchaseDescription",
                 "department", "purchaseTaxCode", "salesTaxCode",
                 "custitem_brand", "itemVendorList", "externalId"):
        if name in props:
            print(f"  [{name}]:", json.dumps(props[name])[:250])
    print("sublists:", list((data.get('sublists') or {}).keys()))

probe("customrecord_brands")
