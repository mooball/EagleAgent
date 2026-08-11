#!/usr/bin/env python3
"""Find domains linked to multiple entities (suppliers / customers).

A domain conflict occurs when the same domain maps to:
  - Multiple different suppliers
  - Multiple different customers  
  - Both a supplier AND a customer (most problematic for matching)

This script mirrors the logic in includes/gmail/matching.py:build_domain_index()
to ensure we're analyzing the exact same data the matching pipeline uses.
"""

import sys
from collections import defaultdict
from urllib.parse import urlparse

# Add project root to path
sys.path.insert(0, "/Volumes/980PRO/tom_home_backup/src/EagleAgent")

from includes.dashboard.database import get_session
from includes.dashboard.models import Supplier, Customer, Contact
from sqlalchemy import func


# ── Helpers ──────────────────────────────────────────────────────────────────

def extract_domain(email: str) -> str | None:
    """Extract domain from an email address, lowercased."""
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].lower().strip()


def extract_domain_from_url(url: str) -> str | None:
    """Extract root domain from a URL."""
    if not url:
        return None
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            return None
        host = host.lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return None


# Same skip list as matching.py
_GENERIC_DOMAINS = frozenset({
    # Global freemail
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.com.au",
    "hotmail.com", "outlook.com", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com", "aol.com",
    "protonmail.com", "proton.me", "mail.com", "zoho.com",
    # Yahoo country variants
    "yahoo.co.id", "yahoo.co.in", "yahoo.co.uk",
    # Yahoo-owned freemail aliases
    "ymail.com", "y7mail.com", "rocketmail.com",
    # Microsoft country variants
    "hotmail.com.au", "live.com.au", "live.fr", "outlook.com.au",
    # Chinese freemail
    "163.com", "qq.com",
    # Korean portal
    "naver.com",
    # Australian consumer ISPs
    "bigpond.com", "bigpond.com.au", "bigpond.net.au",
    "optusnet.com.au", "tpg.com.au", "iinet.net.au",
    "internode.on.net", "westnet.com.au", "dodo.com.au",
    "iprimus.com.au", "ozemail.com.au", "exemail.com.au",
    "onthenet.com.au", "activ8.net.au", "skymesh.com.au",
    "pacific.net.au",
    # International ISPs
    "orange.fr", "wanadoo.fr", "btinternet.com", "connect.com.fj",
})

_INTERNAL_DOMAINS = frozenset({
    "eagle-exports.com", "eagle-exports.com.au",
    "eaglexp.com", "eaglexp.com.au",
})


def should_skip(domain: str) -> bool:
    return domain in _GENERIC_DOMAINS or domain in _INTERNAL_DOMAINS


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    session = get_session()
    try:
        # domain → list of (entity_type, entity_id, entity_name, source)
        domain_map: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)

        def add(domain: str, entity_type: str, entity_id, entity_name: str, source: str):
            if not domain or should_skip(domain):
                return
            domain = domain.lower()
            entry = (entity_type, str(entity_id), entity_name or "(unnamed)", source)
            # Avoid exact duplicates (same entity_id + same type + same source)
            if entry not in domain_map[domain]:
                domain_map[domain].append(entry)

        # 1. Contact emails → supplier/customer
        print("Loading contacts...")
        contacts = (
            session.query(Contact.email, Contact.supplier_id, Contact.customer_id)
            .filter(Contact.email.isnot(None), Contact.isinactive == False)
            .all()
        )
        for c in contacts:
            domain = extract_domain(c.email)
            if c.supplier_id:
                supplier = session.get(Supplier, c.supplier_id)
                name = supplier.name if supplier else f"supplier:{c.supplier_id}"
                add(domain, "supplier", c.supplier_id, name, "contact email")
            elif c.customer_id:
                customer = session.get(Customer, c.customer_id)
                name = customer.companyname if customer else f"customer:{c.customer_id}"
                add(domain, "customer", c.customer_id, name, "contact email")

        # 2. Customer emails
        print("Loading customer emails...")
        customers = (
            session.query(Customer.id, Customer.companyname, Customer.email)
            .filter(Customer.email.isnot(None), Customer.isinactive == False)
            .all()
        )
        for cust in customers:
            domain = extract_domain(cust.email)
            add(domain, "customer", cust.id, cust.companyname, "customer email")

        # 3. Supplier website URLs
        print("Loading supplier URLs...")
        suppliers_url = (
            session.query(Supplier.id, Supplier.name, Supplier.url)
            .filter(Supplier.url.isnot(None), func.length(Supplier.url) > 0)
            .all()
        )
        for s in suppliers_url:
            domain = extract_domain_from_url(s.url)
            add(domain, "supplier", s.id, s.name, "website URL")

        # 4. Supplier alt_domains
        print("Loading supplier alt_domains...")
        suppliers_alt = (
            session.query(Supplier.id, Supplier.name, Supplier.alt_domains)
            .filter(Supplier.alt_domains.isnot(None))
            .all()
        )
        for s in suppliers_alt:
            if isinstance(s.alt_domains, list):
                for d in s.alt_domains:
                    add(str(d).lower().strip(), "supplier", s.id, s.name, "alt_domain")

        # ── Analyse conflicts ────────────────────────────────────────────────
        total_domains = len(domain_map)
        conflicts = []
        for domain, entries in domain_map.items():
            # Deduplicate: unique entities (type + id)
            unique_entities = set()
            for etype, eid, ename, esrc in entries:
                unique_entities.add((etype, eid, ename))
            if len(unique_entities) <= 1:
                continue

            # Classify conflict type
            supplier_ids = set()
            customer_ids = set()
            for etype, eid, ename in unique_entities:
                if etype == "supplier":
                    supplier_ids.add((eid, ename))
                else:
                    customer_ids.add((eid, ename))

            has_supplier_conflict = len(supplier_ids) > 1
            has_customer_conflict = len(customer_ids) > 1
            has_cross_type = bool(supplier_ids) and bool(customer_ids)

            conflicts.append({
                "domain": domain,
                "entries": entries,
                "unique_entities": unique_entities,
                "supplier_ids": supplier_ids,
                "customer_ids": customer_ids,
                "has_supplier_conflict": has_supplier_conflict,
                "has_customer_conflict": has_customer_conflict,
                "has_cross_type": has_cross_type,
            })

        # Sort: cross-type first (most problematic), then by domain
        conflicts.sort(key=lambda c: (
            not c["has_cross_type"],
            not c["has_supplier_conflict"],
            not c["has_customer_conflict"],
            c["domain"],
        ))

        # ── Output ───────────────────────────────────────────────────────────
        from datetime import datetime, timezone

        cross_type = [c for c in conflicts if c["has_cross_type"]]
        supplier_only = [c for c in conflicts if not c["has_cross_type"] and c["has_supplier_conflict"]]
        customer_only = [c for c in conflicts if not c["has_cross_type"] and c["has_customer_conflict"]]

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        outfile = "data/domain_conflicts_report.md"

        lines = []
        def w(s=""):
            lines.append(s)

        w(f"# Domain Conflict Report")
        w()
        w(f"**Generated:** {now}")
        w()
        w("## Summary")
        w()
        w("| Metric | Count |")
        w("|--------|------:|")
        w(f"| Total unique domains indexed | {total_domains:,} |")
        w(f"| Domains with conflicts | **{len(conflicts):,}** |")
        w(f"| ⚠ Cross-type (supplier + customer) | **{len(cross_type):,}** |")
        w(f"| ⚠ Multiple suppliers | {len(supplier_only):,} |")
        w(f"| ⚠ Multiple customers | {len(customer_only):,} |")
        w()

        def _write_section(title: str, items: list, severity: str):
            if not items:
                return
            w(f"## {title}")
            w()
            w(f"**{len(items)} domains** with {severity}")
            w()
            for i, c in enumerate(items, 1):
                w(f"### {i}. `{c['domain']}`")
                w()
                w("| Type | Entity | Source |")
                w("|------|--------|--------|")
                # Deduplicate entries for display
                seen = set()
                for etype, eid, ename, esrc in c["entries"]:
                    key = (etype, eid, esrc)
                    if key in seen:
                        continue
                    seen.add(key)
                    tag = "🏭 Supplier" if etype == "supplier" else "🏢 Customer"
                    w(f"| {tag} | {ename} | {esrc} |")
                w()

        _write_section(
            "Cross-Type Conflicts (supplier + customer)",
            cross_type,
            "domains linked to both a supplier and a customer — matching is ambiguous",
        )
        _write_section(
            "Multi-Supplier Conflicts",
            supplier_only,
            "domains linked to more than one supplier",
        )
        _write_section(
            "Multi-Customer Conflicts",
            customer_only,
            "domains linked to more than one customer",
        )

        # Write file
        with open(outfile, "w") as f:
            f.write("\n".join(lines))

        print(f"\nReport written to: {outfile}")
        print(f"  Total domains:    {total_domains:,}")
        print(f"  Conflicts:        {len(conflicts):,}")
        print(f"  Cross-type:       {len(cross_type):,}")
        print(f"  Multi-supplier:   {len(supplier_only):,}")
        print(f"  Multi-customer:   {len(customer_only):,}")
        print("Done.")

    finally:
        session.close()


if __name__ == "__main__":
    main()
