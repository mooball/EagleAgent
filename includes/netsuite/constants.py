"""
NetSuite status code mappings and constants.

Status codes are single-letter values stored in transaction.status.
These maps provide human-readable labels for display in dashboards and agent tools.
"""

SALES_ORDER_STATUS = {
    "A": "Pending Approval",
    "B": "Pending Fulfillment",
    "C": "Cancelled",
    "D": "Partially Fulfilled",
    "E": "Pending Billing/Partially Fulfilled",
    "F": "Pending Billing",
    "G": "Billed",
    "H": "Closed",
}

QUOTE_STATUS = {
    "A": "Open",
    "B": "Processed",
    "C": "Closed",
    "V": "Voided",
    "X": "Expired",
}

OPPORTUNITY_STATUS = {
    "A": "In Progress",
    "B": "Issued Quote",
    "C": "Closed - Won",
    "D": "Closed - Lost",
}

_STATUS_MAPS = {
    "SalesOrder": SALES_ORDER_STATUS,
    "Quote": QUOTE_STATUS,
    "Opportunity": OPPORTUNITY_STATUS,
}


def get_status_label(doc_type: str, code: str) -> str:
    """Return human-readable status label for a given doc_type and status code.

    Falls back to the raw code if doc_type or code is unrecognized.
    """
    status_map = _STATUS_MAPS.get(doc_type)
    if status_map:
        return status_map.get(code, code)
    return code
