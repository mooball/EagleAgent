"""List metadata-catalog root to see inventoryitem + operations (READ-ONLY)."""
import json
from includes.netsuite.client import NetSuiteClient
c = NetSuiteClient()
headers = c._headers()
headers["Accept"] = "application/schema+json"
r = c._session.get(f"{c._base_url}/record/v1/metadata-catalog", headers=headers, timeout=120)
print("root catalog HTTP", r.status_code, "bytes", len(r.content))
data = r.json()
# structure may vary; find inventoryitem entry
s = json.dumps(data)
print("contains inventoryitem:", "inventoryitem" in s)
# try to find operations mentions
import re
m = re.findall(r'"operations"\s*:\s*(\[[^\]]*\]|\{[^}]*\})', s)
print("sample operations fields:", m[:5])
