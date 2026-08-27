"""Tests for supplier matching and domain extraction utilities in database.py.

Covers:
- _extract_domain: subdomain stripping, ccTLD handling, edge cases
- match_supplier: domain-first lookup, name + domain verification, country check
- match_supplier_by_name: containment and trigram fallback
"""

import uuid
import pytest
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from includes.dashboard.database import _extract_domain, match_supplier, match_supplier_by_name
from includes.dashboard.models import Base, Supplier


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def db_session():
    """Create a test DB session with SAVEPOINT rollback."""
    from includes.dashboard.database import _sync_url
    engine = create_engine(_sync_url(), pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection)
    session = Session(bind=connection)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess, trans):
        if trans.nested and not trans._parent.nested:
            sess.begin_nested()

    session.close = lambda: None
    yield session
    transaction.rollback()
    connection.close()


# ============================================================================
# _extract_domain
# ============================================================================

class TestExtractDomain:
    def test_simple_domain(self):
        assert _extract_domain("https://example.com") == "example.com"

    def test_strips_www(self):
        assert _extract_domain("https://www.example.com") == "example.com"

    def test_strips_all_subdomains(self):
        assert _extract_domain("https://shop.store.example.com") == "example.com"

    def test_com_au_tld(self):
        assert _extract_domain("https://www.abcparts.com.au/contact") == "abcparts.com.au"

    def test_com_au_subdomain_stripping(self):
        """my.komatsu.com.au → komatsu.com.au, not my.komatsu.com.au."""
        assert _extract_domain("https://my.komatsu.com.au") == "komatsu.com.au"

    def test_co_uk_tld(self):
        assert _extract_domain("https://shop.example.co.uk") == "example.co.uk"

    def test_co_nz_tld(self):
        assert _extract_domain("https://www.supplier.co.nz/products") == "supplier.co.nz"

    def test_net_au_tld(self):
        assert _extract_domain("https://portal.warehouse.net.au") == "warehouse.net.au"

    def test_org_uk_tld(self):
        assert _extract_domain("https://www.charity.org.uk") == "charity.org.uk"

    def test_no_scheme(self):
        """URL without scheme should still parse if hostname is deducible."""
        # urlparse treats "example.com" as a path, not hostname
        # So this returns None — caller should always provide scheme
        result = _extract_domain("example.com")
        # Depending on implementation: no hostname → None
        assert result is None

    def test_bare_hostname_with_scheme(self):
        assert _extract_domain("http://sleatorplant.com") == "sleatorplant.com"

    def test_with_path_and_query(self):
        assert _extract_domain("https://www.supplier.com/products?id=42") == "supplier.com"

    def test_with_port(self):
        assert _extract_domain("https://www.supplier.com:8080/api") == "supplier.com"

    def test_none_input(self):
        assert _extract_domain(None) is None

    def test_empty_string(self):
        assert _extract_domain("") is None

    def test_invalid_url(self):
        assert _extract_domain("not-a-url") is None

    def test_ip_address(self):
        # IP addresses have no "domain" in the traditional sense
        result = _extract_domain("http://192.168.1.1/admin")
        # Should return something parseable (last 2 parts)
        assert result is not None

    def test_com_sg_tld(self):
        assert _extract_domain("https://shop.company.com.sg") == "company.com.sg"

    def test_co_jp_tld(self):
        assert _extract_domain("https://www.toyota.co.jp") == "toyota.co.jp"


# ============================================================================
# match_supplier — domain-first lookup
# ============================================================================

class TestMatchSupplierDomainFirst:
    def test_domain_match_different_name(self, db_session):
        """Domain-first: same domain matches even when names differ significantly."""
        sup = Supplier(
            id=uuid.uuid4(),
            name="Xyzzy Test Export & Wholesale",
            url="https://www.xyzzytest-unique.com.au",
            country="AU",
            source="netsuite",
        )
        db_session.add(sup)
        db_session.flush()

        result = match_supplier(
            "Xyzzy Test Australia",
            url="https://xyzzytest-unique.com.au/shop",
            country="AU",
            session=db_session,
        )
        assert result is not None
        assert result.id == sup.id

    def test_domain_match_subdomain(self, db_session):
        """Subdomain difference shouldn't matter — root domains match."""
        sup = Supplier(
            id=uuid.uuid4(),
            name="Fakeomatsu Parts",
            url="https://www.fakeomatsu-test.com.au",
            country="AU",
            source="netsuite",
        )
        db_session.add(sup)
        db_session.flush()

        result = match_supplier(
            "Fakeomatsu Australia",
            url="https://my.fakeomatsu-test.com.au",
            country="AU",
            session=db_session,
        )
        assert result is not None
        assert result.id == sup.id

    def test_no_domain_match_falls_through_to_name(self, db_session):
        """When domains don't match, domain-first should not return the wrong supplier."""
        sup = Supplier(
            id=uuid.uuid4(),
            name="ABC Supplies",
            url="https://abcsupplies.com.au",
            country="AU",
            source="netsuite",
        )
        db_session.add(sup)
        db_session.flush()

        result = match_supplier(
            "ABC Supplies",
            url="https://differentsite.com.au",
            country="AU",
            session=db_session,
        )
        # Should NOT match — domain mismatch rejects the name-based candidate too
        assert result is None

    def test_domain_match_via_contact_url(self, db_session):
        """Match via a URL in the supplier's contacts array."""
        sup = Supplier(
            id=uuid.uuid4(),
            name="Fakewestern Equipment",
            url=None,  # no main URL
            contacts=[{"url": "https://www.fakewestern-test.com.au", "email": "info@fakewestern-test.com.au"}],
            country="AU",
            source="netsuite",
        )
        db_session.add(sup)
        db_session.flush()

        result = match_supplier(
            "Fakewestern Equipment Co",
            url="https://fakewestern-test.com.au",
            country="AU",
            session=db_session,
        )
        assert result is not None
        assert result.id == sup.id


# ============================================================================
# match_supplier — name + verification
# ============================================================================

class TestMatchSupplierNameVerification:
    def test_name_match_same_country(self, db_session):
        """Name containment + same country → match."""
        sup = Supplier(
            id=uuid.uuid4(),
            name="Sydney Tools",
            country="AU",
            source="netsuite",
        )
        db_session.add(sup)
        db_session.flush()

        result = match_supplier("Sydney Tools Pty Ltd", country="AU", session=db_session)
        assert result is not None
        assert result.id == sup.id

    def test_name_match_different_country_rejected(self, db_session):
        """Name match + different country → reject."""
        sup = Supplier(
            id=uuid.uuid4(),
            name="Global Supply Co",
            country="US",
            source="netsuite",
        )
        db_session.add(sup)
        db_session.flush()

        result = match_supplier("Global Supply Co", country="AU", session=db_session)
        assert result is None

    def test_name_match_domain_mismatch_rejected(self, db_session):
        """Name matches but domains are different → reject."""
        sup = Supplier(
            id=uuid.uuid4(),
            name="ZyxTestDomainMismatchCo",
            url="https://zyxtestdomainmismatch.com.au",
            country="AU",
            source="netsuite",
        )
        db_session.add(sup)
        db_session.flush()

        result = match_supplier(
            "ZyxTestDomainMismatchCo",
            url="https://differentzyxsite.com.au",
            country="AU",
            session=db_session,
        )
        # Domains differ → domain-first won't match; name match found but domain mismatch rejects it.
        assert result is None

    def test_no_corroborating_attrs_containment_accepted(self, db_session):
        """No URL or country on either side, but name containment → accept."""
        sup = Supplier(
            id=uuid.uuid4(),
            name="Parker Hannifin",
            source="netsuite",
        )
        db_session.add(sup)
        db_session.flush()

        result = match_supplier("Parker Hannifin Australia", session=db_session)
        assert result is not None
        assert result.id == sup.id

    def test_no_match_returns_none(self, db_session):
        """Completely unknown supplier → None."""
        result = match_supplier(
            "Totally Nonexistent Corp XYZ",
            url="https://nonexistent-xyz.com",
            country="NZ",
            session=db_session,
        )
        assert result is None

    def test_match_no_url_same_country(self, db_session):
        """No URLs on either side, same country → match by name containment."""
        sup = Supplier(
            id=uuid.uuid4(),
            name="Total Tools",
            country="AU",
            source="netsuite",
        )
        db_session.add(sup)
        db_session.flush()

        result = match_supplier("Total Tools", country="AU", session=db_session)
        assert result is not None
        assert result.id == sup.id


# ============================================================================
# match_supplier_by_name
# ============================================================================

class TestMatchSupplierByName:
    def test_exact_name_match(self, db_session):
        sup = Supplier(id=uuid.uuid4(), name="Acme Industrial", source="netsuite")
        db_session.add(sup)
        db_session.flush()

        result = match_supplier_by_name("Acme Industrial", session=db_session)
        assert result is not None
        assert result.id == sup.id

    def test_containment_input_in_db(self, db_session):
        """Input name is contained within DB name."""
        sup = Supplier(id=uuid.uuid4(), name="Acme Industrial Supplies Pty Ltd", source="netsuite")
        db_session.add(sup)
        db_session.flush()

        result = match_supplier_by_name("Acme Industrial", session=db_session)
        assert result is not None
        assert result.id == sup.id

    def test_containment_db_in_input(self, db_session):
        """DB name is contained within input name."""
        sup = Supplier(id=uuid.uuid4(), name="ZyxUniqueContainTest", source="netsuite")
        db_session.add(sup)
        db_session.flush()

        result = match_supplier_by_name("ZyxUniqueContainTest Australia Pty Ltd", session=db_session)
        assert result is not None
        assert result.id == sup.id

    def test_empty_name_returns_none(self, db_session):
        result = match_supplier_by_name("", session=db_session)
        assert result is None

    def test_whitespace_name_returns_none(self, db_session):
        result = match_supplier_by_name("   ", session=db_session)
        assert result is None


# ============================================================================
# S3 — flagged (use_instead) suppliers are invisible to matching
# ============================================================================

class TestMatchingSkipsFlagged:

    def _pair(self, db_session):
        primary = Supplier(
            name="Acme Pty Ltd", netsuite_id="NS-1", source="netsuite",
            url="https://www.acme.com.au",
        )
        db_session.add(primary)
        db_session.flush()
        dup = Supplier(
            name="Acme Pty Ltd", netsuite_id=None, source="web",
            url="https://www.acme.com.au", use_instead=primary.id,
        )
        db_session.add(dup)
        db_session.flush()
        return primary, dup

    def test_match_supplier_by_name_skips_flagged(self, db_session):
        primary, _dup = self._pair(db_session)
        result = match_supplier_by_name("Acme Pty Ltd", session=db_session)
        assert result is not None and result.id == primary.id

    def test_match_supplier_domain_first_skips_flagged(self, db_session):
        primary, _dup = self._pair(db_session)
        result = match_supplier(
            "Acme Pty Ltd", url="https://www.acme.com.au", session=db_session
        )
        assert result is not None and result.id == primary.id
