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
        "BUILTIN.DF(v.currency) AS currency, "
        "v.custentity_go_souce_email_address, v.custentity_go_souce_email_name, "
        "v.custentity_go_souce_cc_email_addresses, "
        "a.addr1, a.addr2, a.city, a.state, a.zip, a.country, "
        "v.datecreated, v.lastmodifieddate "
        "FROM vendor v "
        "LEFT JOIN vendorAddressbook vab ON vab.entity = v.id AND vab.defaultbilling = 'T' "
        "LEFT JOIN entityAddress a ON a.nkey = vab.addressbookaddress "
        f"WHERE v.lastmodifieddate >= '{ns_date}' "
        "ORDER BY v.lastmodifieddate ASC"
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
        "SELECT id, name, lastmodified "
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


def sales_orders_updated_since(since_date: str) -> str:
    """
    SuiteQL query for Sales Order line items modified on or after a given date.

    Joins transactionLine with transaction to get header-level fields.
    Only returns lines with a PO vendor (custcol_po_vendor) assigned.

    Args:
        since_date: ISO date string, e.g. '2026-04-01'

    Returns:
        SuiteQL SELECT statement sorted ASC for resumability.
    """
    dt = datetime.strptime(since_date, "%Y-%m-%d")
    ns_date = f"{dt.day}/{dt.month}/{dt.year}"

    return (
        "SELECT t.tranid, t.trandate, t.status, "
        "BUILTIN.DF(t.currency) AS currency_name, "
        "t.lastmodifieddate, t.opportunity, "
        "tl.item, BUILTIN.DF(tl.item) AS item_name, "
        "tl.quantity, tl.rate, "
        "tl.custcol_po_rate, tl.custcol_po_vendor, "
        "BUILTIN.DF(tl.custcol_po_vendor) AS vendor_name, "
        "tl.uniquekey "
        "FROM transactionLine tl "
        "INNER JOIN transaction t ON t.id = tl.transaction "
        "WHERE t.type = 'SalesOrd' "
        "AND tl.item IS NOT NULL "
        "AND tl.mainline = 'F' "
        "AND tl.taxline = 'F' "
        "AND tl.custcol_po_vendor IS NOT NULL "
        f"AND t.lastmodifieddate >= '{ns_date}' "
        "ORDER BY t.lastmodifieddate ASC"
    )


def quotes_updated_since(since_date: str) -> str:
    """
    SuiteQL query for Quote (Estimate) line items modified on or after a given date.

    Same structure as sales_orders_updated_since but for type = 'Estimate'.
    Excludes status 'B' (Processed) and 'V' (Voided):
      - Processed Quotes become Sales Orders so importing them would duplicate data.
      - Voided Quotes do not represent useful data.

    Args:
        since_date: ISO date string, e.g. '2026-04-01'

    Returns:
        SuiteQL SELECT statement sorted ASC for resumability.
    """
    dt = datetime.strptime(since_date, "%Y-%m-%d")
    ns_date = f"{dt.day}/{dt.month}/{dt.year}"

    return (
        "SELECT t.tranid, t.trandate, t.status, "
        "BUILTIN.DF(t.currency) AS currency_name, "
        "t.lastmodifieddate, t.opportunity, "
        "tl.item, BUILTIN.DF(tl.item) AS item_name, "
        "tl.quantity, tl.rate, "
        "tl.custcol_po_rate, tl.custcol_po_vendor, "
        "BUILTIN.DF(tl.custcol_po_vendor) AS vendor_name, "
        "tl.uniquekey "
        "FROM transactionLine tl "
        "INNER JOIN transaction t ON t.id = tl.transaction "
        "WHERE t.type = 'Estimate' "
        "AND tl.item IS NOT NULL "
        "AND tl.mainline = 'F' "
        "AND tl.taxline = 'F' "
        "AND tl.custcol_po_vendor IS NOT NULL "
        "AND t.status NOT IN ('V', 'B') "
        f"AND t.lastmodifieddate >= '{ns_date}' "
        "ORDER BY t.lastmodifieddate ASC"
    )


def opportunities_updated_since(since_date: str) -> str:
    """
    SuiteQL query for Opportunity records modified on or after a given date.

    Args:
        since_date: ISO date string, e.g. '2026-04-01'

    Returns:
        SuiteQL SELECT statement sorted ASC for resumability.
    """
    dt = datetime.strptime(since_date, "%Y-%m-%d")
    ns_date = f"{dt.day}/{dt.month}/{dt.year}"

    return (
        "SELECT o.id, o.tranid, o.title, o.entity, o.status, "
        "o.salesrep, o.total, BUILTIN.DF(o.currency) AS currency, o.lastmodifieddate "
        "FROM opportunity o "
        f"WHERE o.lastmodifieddate >= '{ns_date}' "
        "ORDER BY o.lastmodifieddate ASC"
    )


def customers_updated_since(since_date: str) -> str:
    """
    SuiteQL query for Customer records modified on or after a given date.

    Only returns active customers (isinactive = 'F').

    Args:
        since_date: ISO date string, e.g. '2026-04-01'

    Returns:
        SuiteQL SELECT statement sorted ASC for resumability.
    """
    dt = datetime.strptime(since_date, "%Y-%m-%d")
    ns_date = f"{dt.day}/{dt.month}/{dt.year}"

    return (
        "SELECT c.id, c.entityid, c.companyname, c.fullname, c.email, c.phone, "
        "c.isinactive, BUILTIN.DF(c.currency) AS currency, c.salesrep, c.contactlist, c.lastmodifieddate "
        "FROM customer c "
        "WHERE c.isinactive = 'F' "
        f"AND c.lastmodifieddate >= '{ns_date}' "
        "ORDER BY c.lastmodifieddate ASC"
    )


def contacts_for_ids(contact_ids: list[str]) -> str:
    """
    SuiteQL query to fetch Contact records by ID list.

    Args:
        contact_ids: List of NetSuite contact IDs (e.g. ['5', '76893', '124327'])

    Returns:
        SuiteQL SELECT statement.
    """
    if not contact_ids:
        return ""
    
    id_list = ", ".join(f"'{cid}'" for cid in contact_ids)
    
    return (
        "SELECT c.id, c.firstname, c.lastname, c.email, c.phone, "
        "c.company, c.isinactive, c.lastmodifieddate "
        "FROM contact c "
        f"WHERE c.id IN ({id_list})"
    )


def contacts_updated_since(since_date: str) -> str:
    """
    SuiteQL query for Contact records modified on or after a given date.

    Args:
        since_date: ISO date string, e.g. '2026-04-01'

    Returns:
        SuiteQL SELECT statement.
    """
    dt = datetime.strptime(since_date, "%Y-%m-%d")
    ns_date = f"{dt.day}/{dt.month}/{dt.year}"

    return (
        "SELECT c.id, c.firstname, c.lastname, c.email, c.phone, "
        "c.company, c.isinactive, c.lastmodifieddate "
        "FROM contact c "
        f"WHERE c.lastmodifieddate >= '{ns_date}' "
        "ORDER BY c.lastmodifieddate ASC"
    )
