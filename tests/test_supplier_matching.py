"""Tests for includes/dashboard/supplier_matching.py — normalisation and
match-key maintenance (supplier dedup shared foundation, S1)."""

import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from includes.dashboard.models import Supplier, SupplierMatchKey


@pytest.fixture
def db_session():
    """DB session with SAVEPOINT so commits inside helpers don't end the
    outer transaction — everything rolls back at the end."""
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


def _make_supplier(session, **overrides):
    defaults = {
        "name": "Acme Parts Pty Ltd",
        "url": "https://www.acmeparts.com.au/contact",
        "source": "netsuite",
    }
    defaults.update(overrides)
    sup = Supplier(**defaults)
    session.add(sup)
    session.flush()
    return sup


class TestNormalizeSupplierName:
    def test_strips_punctuation_and_noise(self):
        from includes.dashboard.supplier_matching import normalize_supplier_name
        assert normalize_supplier_name("A.C.M. Laboratory Pty. Ltd.") == "acm laboratory"
        assert normalize_supplier_name("B J Inns Pty Ltd") == "bj inns"

    def test_ampersand_becomes_space(self):
        from includes.dashboard.supplier_matching import normalize_supplier_name
        assert normalize_supplier_name("Smith & Sons") == "smith sons"

    def test_case_and_whitespace_folded(self):
        from includes.dashboard.supplier_matching import normalize_supplier_name
        assert normalize_supplier_name("  THE   Billiard Shop ") == "billiard shop"

    def test_unicode_folded(self):
        from includes.dashboard.supplier_matching import normalize_supplier_name
        assert normalize_supplier_name("Kühlung GmbH") == "kuhlung"

    def test_alphanumerics_survive(self):
        from includes.dashboard.supplier_matching import normalize_supplier_name
        assert normalize_supplier_name("3M") == "3m"

    def test_currency_annotations_dropped(self):
        from includes.dashboard.supplier_matching import normalize_supplier_name
        assert normalize_supplier_name("Acme (AUD) Pty Ltd") == "acme"
        assert normalize_supplier_name("Kalgin Freight Services (usd)") == "kalgin freight services"

    def test_empty(self):
        from includes.dashboard.supplier_matching import normalize_supplier_name
        assert normalize_supplier_name("") == ""
        assert normalize_supplier_name(None) == ""

    def test_all_noise_name_keeps_tokens(self):
        from includes.dashboard.supplier_matching import normalize_supplier_name
        # must not normalise to '' — that would false-match empty keys
        assert normalize_supplier_name("The Company") == "the company"


class TestDomainKey:
    def test_cc_tld_stripped(self):
        from includes.dashboard.supplier_matching import domain_key
        assert domain_key("https://www.abcparts.com.au/x") == "abcparts"

    def test_plain_tld(self):
        from includes.dashboard.supplier_matching import domain_key
        assert domain_key("http://sleatorplant.com") == "sleatorplant"

    def test_bare_domain(self):
        from includes.dashboard.supplier_matching import domain_key
        assert domain_key("abcparts.com") == "abcparts"

    def test_free_mail_excluded(self):
        from includes.dashboard.supplier_matching import domain_key
        assert domain_key("https://gmail.com/x") is None
        assert domain_key("jo@bigpond.com") is None

    def test_email_address(self):
        from includes.dashboard.supplier_matching import domain_key
        assert domain_key("sales@abcparts.com.au") == "abcparts"

    def test_empty(self):
        from includes.dashboard.supplier_matching import domain_key
        assert domain_key(None) is None
        assert domain_key("") is None


class TestSupplierMatchKeys:
    def test_name_url_and_contact_keys(self):
        from includes.dashboard.supplier_matching import supplier_match_keys

        sup = Supplier(
            name="Acme Parts Pty Ltd",
            url="https://www.acmeparts.com.au/contact",
            alt_names=["Acme Pump Division"],
            alt_domains=["acmeparts.co.nz"],
            contacts=[
                {"name": "Sally", "email": "sally@acmeparts.com", "url": "https://acmeparts.com"},
                {"name": "Bob", "email": "bob@gmail.com"},
            ],
        )
        pairs = set(supplier_match_keys(sup))
        assert ("name", "acme parts") in pairs
        assert ("name", "acme pump division") in pairs
        assert ("domain", "acmeparts") in pairs          # url, contact url, email all collapse
        # .co.nz + .com.au + .com all share the same stripped key
        assert sum(1 for t, _v in pairs if t == "domain" and _v == "acmeparts") == 1

    def test_legacy_dedup_marker_skipped(self):
        from includes.dashboard.supplier_matching import supplier_match_keys

        sup = Supplier(name="Acme", alt_names=["__dedup_reviewed__", "Acme Fluid"])
        pairs = set(supplier_match_keys(sup))
        assert ("name", "acme") in pairs
        assert ("name", "acme fluid") in pairs
        assert not any("dedup" in kv for _t, kv in pairs)

    def test_no_keys_when_empty(self):
        from includes.dashboard.supplier_matching import supplier_match_keys
        sup = Supplier(name="The Co", url=None)
        assert supplier_match_keys(sup) == [("name", "the co")]


class TestRebuildMatchKeys:
    def test_idempotent(self, db_session):
        from includes.dashboard.supplier_matching import rebuild_match_keys

        sup = _make_supplier(
            db_session,
            alt_names=["Acme Pump Division"],
            contacts=[{"email": "sally@acmeparts.co.nz"}],
        )

        rebuild_match_keys(db_session, sup)
        db_session.commit()
        first = set(
            (k.key_type, k.key_value)
            for k in db_session.query(SupplierMatchKey).filter_by(supplier_id=sup.id)
        )

        rebuild_match_keys(db_session, sup)
        db_session.commit()
        second = set(
            (k.key_type, k.key_value)
            for k in db_session.query(SupplierMatchKey).filter_by(supplier_id=sup.id)
        )

        assert first == second
        assert ("name", "acme parts") in first
        assert ("name", "acme pump division") in first
        assert ("domain", "acmeparts") in first

    def test_rebuild_reflects_changes(self, db_session):
        from includes.dashboard.supplier_matching import rebuild_match_keys

        sup = _make_supplier(db_session)
        rebuild_match_keys(db_session, sup)
        db_session.commit()

        sup.alt_domains = ["newbrand.com"]
        rebuild_match_keys(db_session, sup)
        db_session.commit()

        keys = db_session.query(SupplierMatchKey).filter_by(supplier_id=sup.id).all()
        pairs = {(k.key_type, k.key_value) for k in keys}
        assert ("domain", "newbrand") in pairs
        assert ("domain", "acmeparts") in pairs  # original url key retained
