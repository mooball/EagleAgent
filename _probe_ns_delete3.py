"""Compare DELETE error messages across record types (READ-ONLY, nothing deleted)."""
from includes.netsuite.client import NetSuiteClient
c = NetSuiteClient()
headers = c._headers()
for rt, rid in [("opportunity", "999999999"), ("vendor", "999999999"),
                ("inventoryitem", "999999999"), ("foobar", "1")]:
    r = c._session.delete(f"{c._base_url}/record/v1/{rt}/{rid}",
                          headers=headers, timeout=60)
    detail = ""
    try:
        detail = r.json().get("o:errorDetails", [{}])[0].get("detail", "")
    except Exception:
        pass
    print(f"DELETE {rt}/{rid} -> {r.status_code} | {detail[:90]}")
