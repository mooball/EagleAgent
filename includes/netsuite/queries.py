"""
Reusable SuiteQL query builders for NetSuite data retrieval.

Note: SuiteQL field names are case-insensitive but NetSuite returns them lowercase.
Date comparisons use 'd/m/yyyy' format (NetSuite's internal format).
"""

from datetime import datetime


def suppliers_updated_since(since_date: str) -> str:
    """
    SuiteQL query for vendor records modified on or after a given date.

    Args:
        since_date: ISO date string, e.g. '2026-04-01'

    Returns:
        SuiteQL SELECT statement.
    """
    # Convert ISO date (YYYY-MM-DD) to NetSuite format (d/m/yyyy)
    dt = datetime.strptime(since_date, "%Y-%m-%d")
    ns_date = f"{dt.day}/{dt.month}/{dt.year}"

    return (
        "SELECT v.id, v.entityid, v.companyname, v.email, v.phone, "
        "v.custentity_supplier_notes, v.custentity_supplier_brand, "
        "v.custentity_ss_hubspot_id, BUILTIN.DF(v.terms) AS terms, "
        "a.addr1, a.addr2, a.city, a.state, a.zip, a.country, "
        "v.datecreated, v.lastmodifieddate "
        "FROM vendor v "
        "LEFT JOIN vendorAddressbook vab ON vab.entity = v.id AND vab.defaultbilling = 'T' "
        "LEFT JOIN entityAddress a ON a.nkey = vab.addressbookaddress "
        f"WHERE v.lastmodifieddate >= '{ns_date}' "
        "ORDER BY v.lastmodifieddate DESC"
    )
