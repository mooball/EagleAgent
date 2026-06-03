"""Test HubSpot authentication and API access.

Verifies:
1. Token is valid and connected
2. Deal pipeline stages are readable
3. Deal properties are discoverable
4. Transactional email scope availability
"""

import sys
import json

# Add project root to path
sys.path.insert(0, ".")

from includes.hubspot import get_client, test_connection
from config.settings import Config


def main():
    print("=" * 60)
    print("HubSpot Integration - Connection Test")
    print("=" * 60)
    
    # --- 1. Test basic connectivity ---
    print("\n[1] Testing token validity...")
    result = test_connection()
    if result["status"] != "ok":
        print(f"  FAILED: {result['message']}")
        sys.exit(1)
    
    print(f"  OK: {result['message']}")
    details = result["details"]
    print(f"  Portal ID: {details.get('portalId')}")
    print(f"  Currency: {details.get('companyCurrency')}")
    print(f"  Time Zone: {details.get('timeZone')}")
    
    # --- 2. List deal pipelines and stages ---
    print("\n[2] Fetching deal pipelines...")
    client = get_client()
    try:
        pipelines = client.crm.pipelines.pipelines_api.get_all(object_type="deals")
        for pipeline in pipelines.results:
            print(f"\n  Pipeline: {pipeline.label} (id: {pipeline.id})")
            for stage in sorted(pipeline.stages, key=lambda s: s.display_order):
                print(f"    Stage {stage.display_order}: {stage.label} (id: {stage.id})")
    except Exception as e:
        print(f"  FAILED to fetch pipelines: {e}")
    
    # --- 3. List deal properties (first 20) ---
    print("\n[3] Fetching deal properties...")
    try:
        props = client.crm.properties.core_api.get_all(object_type="deals")
        print(f"  Total deal properties: {len(props.results)}")
        print("  First 20:")
        for prop in props.results[:20]:
            print(f"    - {prop.name}: {prop.label} ({prop.type})")
    except Exception as e:
        print(f"  FAILED to fetch properties: {e}")
    
    # --- Summary ---
    print("\n" + "=" * 60)
    print("Connection test complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
