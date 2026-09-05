"""NetSuite record creation — write-back operations to NetSuite REST API.

Each record type has its own module with a focused creation function.
All creation functions return a CreateResult for uniform error handling
by callers (UI, agents, pipelines).
"""

from .base import CreateResult, NetSuiteCreateError
from .opportunity import create_opportunity, create_and_link_opportunity, update_opportunity_title
from .item import (
    create_brand,
    create_item,
    ensure_item_with_vendor,
    find_brand_by_name,
    find_item_by_part_number,
    get_or_create_brand,
    set_vendor_price,
)
from .vendor import (
    create_vendor,
    ensure_vendor,
    find_vendor_by_entity_id,
    resolve_currency,
    resolve_tax_item,
    vendor_lookup_options,
)

__all__ = [
    "CreateResult",
    "NetSuiteCreateError",
    "create_opportunity",
    "create_and_link_opportunity",
    "update_opportunity_title",
    "create_brand",
    "create_item",
    "ensure_item_with_vendor",
    "find_brand_by_name",
    "find_item_by_part_number",
    "get_or_create_brand",
    "set_vendor_price",
    "create_vendor",
    "ensure_vendor",
    "find_vendor_by_entity_id",
    "resolve_currency",
    "resolve_tax_item",
    "vendor_lookup_options",
]
