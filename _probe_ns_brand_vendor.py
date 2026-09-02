"""Read-only probes: customrecord_brands fields + test vendor 6730."""
from includes.netsuite.client import NetSuiteClient
c = NetSuiteClient()

print("== vendor 6730 ==")
r = c.get("record/v1/vendor/6730", timeout=60)
v = r.json()
for k in ("companyName", "currency", "taxItem", "entityId", "subsidiary"):
    print(f"  {k}:", v.get(k))

print("\n== customrecord_brands sample ==")
try:
    rows = c.suiteql("SELECT id, name FROM customrecord_brands ORDER BY id DESC", limit=5)
    for row in rows:
        print("  ", row)
except Exception as e:
    print("SuiteQL brands FAILED:", type(e).__name__, str(e)[:200])

print("\n== item lookup pattern ==")
try:
    rows = c.suiteql("SELECT id, itemid FROM item WHERE UPPER(itemid) = UPPER('egtest-item-001')", limit=5)
    print("   egtest lookup ->", rows)
    rows = c.suiteql("SELECT id, itemid, custitem_brand, department, class FROM item WHERE UPPER(itemid) = UPPER('2438R13-7')", limit=5)
    for row in rows:
        print("   ", row)
except Exception as e:
    print("SuiteQL item FAILED:", type(e).__name__, str(e)[:200])
