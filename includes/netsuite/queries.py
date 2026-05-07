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
        "SELECT v.id, v.entityid, v.companyname, v.email, v.phone, v.url, "
        "v.custentity_supplier_notes, v.custentity_supplier_brand, "
        "v.custentity_ss_hubspot_id, BUILTIN.DF(v.terms) AS terms, "
        "v.custentity_go_souce_email_address, v.custentity_go_souce_email_name, "
        "v.custentity_go_souce_cc_email_addresses, "
        "a.addr1, a.addr2, a.city, a.state, a.zip, a.country, "
        "v.datecreated, v.lastmodifieddate "
        "FROM vendor v "
        "LEFT JOIN vendorAddressbook vab ON vab.entity = v.id AND vab.defaultbilling = 'T' "
        "LEFT JOIN entityAddress a ON a.nkey = vab.addressbookaddress "
        f"WHERE v.lastmodifieddate >= '{ns_date}' "
        "ORDER BY v.lastmodifieddate DESC"
    )


def all_brands(since_date: str | None = None) -> str:
    """
    SuiteQL query for brand records (custom record type: customrecord_brands).

    Args:
        since_date: Optional ISO date string (e.g. '2026-04-01'). If provided,
                    only returns brands modified on or after that date.

    Returns:
        SuiteQL SELECT statement returning id and name for active brands.
    """
    query = (
        "SELECT id, name "
        "FROM customrecord_brands "
        "WHERE isinactive = 'F'"
    )

    if since_date:
        dt = datetime.strptime(since_date, "%Y-%m-%d")
        ns_date = f"{dt.day}/{dt.month}/{dt.year}"
        query += f" AND lastmodified >= '{ns_date}'"

    query += " ORDER BY name"
    return query


def products_updated_since(since_date: str) -> str:
    """
    SuiteQL query for inventory item records modified on or after a given date.

    Only returns active InvtPart (inventory part) items.

    Args:
        since_date: ISO date string, e.g. '2026-04-01'

    Returns:
        SuiteQL SELECT statement.
    """
    dt = datetime.strptime(since_date, "%Y-%m-%d")
    ns_date = f"{dt.day}/{dt.month}/{dt.year}"

    return (
        "SELECT i.id, i.itemid, i.description, "
        "i.custitem_brand, BUILTIN.DF(i.custitem_brand) AS brand_name, "
        "i.weight, "
        "i.lastmodifieddate "
        "FROM item i "
        "WHERE i.itemtype = 'InvtPart' "
        "AND i.isinactive = 'F' "
        f"AND i.lastmodifieddate >= '{ns_date}' "
        "ORDER BY i.lastmodifieddate ASC"
    )
