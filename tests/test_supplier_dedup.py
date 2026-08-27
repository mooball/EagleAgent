"""Tests for includes/dashboard/supplier_dedup.py — merge_suppliers matrix
and reference reassignment (shared foundation S2)."""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from includes.dashboard.models import (
    Brand,
    Contact,
    EmailTracking,
    Product,
    RFQ,
    RFQItem,
    Supplier,
    SupplierBrand,
    SupplierMatchKey,
    Transaction,
)


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


def _supplier(session, name="Dup Parts Pty Ltd", netsuite_id=None, **overrides):
    defaults = {
        "name": name,
        "netsuite_id": netsuite_id or f"NS-{uuid.uuid4().hex[:8]}",
        "source": "netsuite" if netsuite_id is not False else "web",
    }
    if netsuite_id is False:
        defaults["netsuite_id"] = None
    defaults.update(overrides)
    sup = Supplier(**defaults)
    session.add(sup)
    session.flush()
    return sup


def _brand(session, name=None):
    brand = Brand(netsuite_id=f"B-{uuid.uuid4().hex[:8]}", name=name or f"Brand {uuid.uuid4().hex[:6]}")
    session.add(brand)
    session.flush()
    return brand


class TestLookupHygiene:
    """S3: resolve_supplier_id and active_suppliers helpers."""

    def test_resolve_follows_chain(self, db_session):
        from includes.dashboard.supplier_dedup import resolve_supplier_id
        c = _supplier(db_session, name="C Final")
        b = _supplier(db_session, name="B Middle", use_instead=c.id)
        a = _supplier(db_session, name="A Flagged", use_instead=b.id)
        assert resolve_supplier_id(db_session, a.id) == c.id

    def test_resolve_no_chain_returns_self(self, db_session):
        from includes.dashboard.supplier_dedup import resolve_supplier_id
        a = _supplier(db_session)
        assert resolve_supplier_id(db_session, a.id) == a.id

    def test_resolve_cycle_safe(self, db_session):
        from includes.dashboard.supplier_dedup import resolve_supplier_id
        a = _supplier(db_session, name="A")
        b = _supplier(db_session, name="B", use_instead=a.id)
        a.use_instead = b.id
        db_session.flush()
        resolved = resolve_supplier_id(db_session, a.id)
        assert resolved in (a.id, b.id)  # stops, does not loop forever

    def test_active_suppliers_excludes_flagged(self, db_session):
        from includes.dashboard.supplier_dedup import active_suppliers
        primary = _supplier(db_session, name="Primary")
        dup = _supplier(db_session, name="Dup", use_instead=primary.id)
        ids = {s.id for s in active_suppliers(db_session).all()}
        assert primary.id in ids
        assert dup.id not in ids

    def test_supplier_lookup_hides_dups_for_linking(self, db_session):
        from includes.dashboard.supplier_dedup import supplier_lookup
        primary = _supplier(db_session, name="Commander Pty Ltd")
        dup = _supplier(db_session, name="Commander (Old)", use_instead=primary.id)
        ids = {s.id for s in supplier_lookup(db_session, "commander", hide_dups=True)}
        assert primary.id in ids
        assert dup.id not in ids

    def test_supplier_lookup_shows_dups_for_management(self, db_session):
        from includes.dashboard.supplier_dedup import supplier_lookup
        primary = _supplier(db_session, name="Commander Pty Ltd")
        dup = _supplier(db_session, name="Commander (Old)", use_instead=primary.id)
        ids = {s.id for s in supplier_lookup(db_session, "commander", hide_dups=False)}
        assert primary.id in ids
        assert dup.id in ids


def _product(session):
    product = Product(part_number=f"PN-{uuid.uuid4().hex[:8]}", netsuite_id=f"P-{uuid.uuid4().hex[:8]}")
    session.add(product)
    session.flush()
    return product


def _rfq_with_item(session, supplier_a, supplier_b):
    rfq = RFQ(
        rfq_number=f"RFQ-2026-{uuid.uuid4().hex[:4].upper()}",
        customer="Test Customer",
        created_by="tester",
        created_date=datetime.now(timezone.utc),
        supplier_meta={supplier_b.name: {"shipping_cost": 10}},
    )
    session.add(rfq)
    session.flush()
    item = RFQItem(
        rfq_id=rfq.id, line=1, input_description="Widget",
        suppliers=[{"supplier_id": str(supplier_a.id), "name": supplier_a.name}],
        brand_suppliers=[{"supplier_id": str(supplier_b.id), "name": supplier_b.name}],
    )
    session.add(item)
    session.flush()
    return rfq


class TestMergeMatrix:
    def test_web_into_netsuite_deletes_and_reassigns(self, db_session):
        from includes.dashboard.supplier_dedup import merge_suppliers

        primary = _supplier(db_session, name="Primary Pty Ltd", netsuite_id="NS-1")
        duplicate = _supplier(db_session, name="Primary Pty Ltd (Web)", netsuite_id=False,
                              url="https://primary.com.au", source="web")
        rfq = _rfq_with_item(db_session, duplicate, duplicate)

        result = merge_suppliers(db_session, primary.id, duplicate.id)
        db_session.commit()

        assert result.deleted is True
        assert result.use_instead_set is False
        assert db_session.get(Supplier, duplicate.id) is None
        # supplier row on RFQ item now points at primary
        item = db_session.query(RFQItem).filter(RFQItem.rfq_id == rfq.id).one()
        assert all(e["supplier_id"] == str(primary.id) for e in item.suppliers)
        assert all(e["name"] == primary.name for e in item.suppliers)
        # supplier_meta key remapped
        meta = db_session.query(RFQ).filter(RFQ.id == rfq.id).one().supplier_meta
        assert "Primary Pty Ltd" in meta

    def test_netsuite_into_netsuite_keeps_row_and_flags(self, db_session):
        from includes.dashboard.supplier_dedup import merge_suppliers

        primary = _supplier(db_session, name="Primary Pty Ltd", netsuite_id="NS-1")
        duplicate = _supplier(db_session, name="Primary Pty Ltd (Old)", netsuite_id="NS-2")

        result = merge_suppliers(db_session, primary.id, duplicate.id)
        db_session.commit()

        assert result.deleted is False
        assert result.use_instead_set is True
        kept = db_session.get(Supplier, duplicate.id)
        assert kept is not None
        assert kept.use_instead == primary.id

    def test_web_into_web_deletes(self, db_session):
        from includes.dashboard.supplier_dedup import merge_suppliers

        primary = _supplier(db_session, netsuite_id=False)
        duplicate = _supplier(db_session, netsuite_id=False, name="Dup Parts (2)")

        result = merge_suppliers(db_session, primary.id, duplicate.id)
        db_session.commit()

        assert result.deleted is True
        assert db_session.get(Supplier, duplicate.id) is None

    def test_netsuite_into_web_rejected(self, db_session):
        from includes.dashboard.supplier_dedup import merge_suppliers

        primary = _supplier(db_session, netsuite_id=False)
        duplicate = _supplier(db_session, netsuite_id="NS-2")

        with pytest.raises(ValueError, match="swap"):
            merge_suppliers(db_session, primary.id, duplicate.id)

    def test_same_supplier_rejected(self, db_session):
        from includes.dashboard.supplier_dedup import merge_suppliers

        sup = _supplier(db_session)
        with pytest.raises(ValueError, match="different"):
            merge_suppliers(db_session, sup.id, sup.id)

    def test_already_merged_rejected(self, db_session):
        from includes.dashboard.supplier_dedup import merge_suppliers

        primary = _supplier(db_session, name="Primary Pty Ltd", netsuite_id="NS-1")
        duplicate = _supplier(db_session, name="Primary Pty Ltd (Old)", netsuite_id="NS-2")
        merge_suppliers(db_session, primary.id, duplicate.id)
        db_session.commit()

        with pytest.raises(ValueError, match="already merged"):
            merge_suppliers(db_session, primary.id, duplicate.id)


class TestMergeDetails:
    def test_merge_names_adds_alt_names(self, db_session):
        from includes.dashboard.supplier_dedup import merge_suppliers

        primary = _supplier(db_session, name="Acme Pty Ltd", netsuite_id="NS-1")
        duplicate = _supplier(db_session, name="ACME Fluid", netsuite_id=False,
                              alt_names=["Acme International"])

        merge_suppliers(db_session, primary.id, duplicate.id)
        db_session.commit()

        kept = db_session.get(Supplier, primary.id)
        assert "ACME Fluid" in kept.alt_names
        assert "Acme International" in kept.alt_names

    def test_merge_names_disabled_leaves_alt_names(self, db_session):
        from includes.dashboard.supplier_dedup import MergeConfig, merge_suppliers

        primary = _supplier(db_session, name="Acme Pty Ltd", netsuite_id="NS-1")
        duplicate = _supplier(db_session, name="ACME Fluid", netsuite_id=False)

        merge_suppliers(db_session, primary.id, duplicate.id,
                        MergeConfig(merge_names=False))
        db_session.commit()

        kept = db_session.get(Supplier, primary.id)
        assert kept.alt_names in (None, [])

    def test_merge_domains_moves_domains(self, db_session):
        from includes.dashboard.supplier_dedup import merge_suppliers

        primary = _supplier(db_session, name="Acme Pty Ltd", netsuite_id="NS-1")
        duplicate = _supplier(db_session, name="Acme Fluid", netsuite_id=False,
                              url="https://acmefluid.co.nz", alt_domains=["acmefluid.com"])

        merge_suppliers(db_session, primary.id, duplicate.id)
        db_session.commit()

        kept = db_session.get(Supplier, primary.id)
        assert kept.url == "https://acmefluid.co.nz"  # primary had none — take it
        assert "acmefluid.com" in kept.alt_domains

    def test_fk_reassignment(self, db_session):
        from includes.dashboard.supplier_dedup import merge_suppliers

        primary = _supplier(db_session, name="Primary Pty Ltd", netsuite_id="NS-1")
        duplicate = _supplier(db_session, name="Dup Pty Ltd", netsuite_id=False)
        brand = _brand(db_session)
        product = _product(db_session)

        db_session.add(SupplierBrand(supplier_id=duplicate.id, brand_id=brand.id))
        db_session.add(Contact(supplier_id=duplicate.id, fullname="C", email="c@d.com",
                               label="Main", isinactive=False))
        db_session.add(Transaction(doc_number="DOC1", product_id=product.id,
                                   supplier_id=duplicate.id))
        db_session.add(EmailTracking(gmail_thread_id=f"t-{uuid.uuid4().hex[:8]}",
                                     user_email="harry@eagle-exports.com",
                                     direction="received",
                                     supplier_id=duplicate.id))
        db_session.commit()

        result = merge_suppliers(db_session, primary.id, duplicate.id)
        db_session.commit()

        assert result.counts["supplier_brands_moved"] == 1
        assert result.counts["contacts"] == 1
        assert result.counts["transactions"] == 1
        assert result.counts["email_tracking"] == 1

    def test_conflicting_brand_link_dropped(self, db_session):
        from includes.dashboard.supplier_dedup import merge_suppliers

        primary = _supplier(db_session, name="Primary Pty Ltd", netsuite_id="NS-1")
        duplicate = _supplier(db_session, name="Dup Pty Ltd", netsuite_id=False)
        brand = _brand(db_session)
        db_session.add(SupplierBrand(supplier_id=primary.id, brand_id=brand.id))
        db_session.add(SupplierBrand(supplier_id=duplicate.id, brand_id=brand.id))
        db_session.commit()

        result = merge_suppliers(db_session, primary.id, duplicate.id)
        db_session.commit()

        assert result.counts["supplier_brands_dropped"] == 1
        links = db_session.query(SupplierBrand).filter(
            SupplierBrand.brand_id == brand.id).all()
        assert [l.supplier_id for l in links] == [primary.id]

    def test_match_keys_rebuilt_for_primary(self, db_session):
        from includes.dashboard.supplier_dedup import merge_suppliers
        from includes.dashboard.supplier_matching import rebuild_match_keys

        primary = _supplier(db_session, name="Acme Pty Ltd", netsuite_id="NS-1")
        duplicate = _supplier(db_session, name="ACME Fluid", netsuite_id=False,
                              url="https://acmefluid.com")
        rebuild_match_keys(db_session, primary)
        db_session.commit()

        merge_suppliers(db_session, primary.id, duplicate.id)
        db_session.commit()

        keys = db_session.query(SupplierMatchKey).filter_by(supplier_id=primary.id).all()
        pairs = {(k.key_type, k.key_value) for k in keys}
        assert ("name", "acme fluid") in pairs        # duplicate's name now searchable
        assert ("domain", "acmefluid") in pairs       # duplicate's domain now searchable


class TestPickKeepRemove:
    """Transaction count and recency now participate in primary selection."""

    def _sup(self, netsuite_id=True, url=None, contacts=(), alt_names=(), alt_domains=()):
        return Supplier(
            id=uuid.uuid4(),
            netsuite_id=netsuite_id,
            url=url,
            contacts=list(contacts),
            alt_names=list(alt_names),
            alt_domains=list(alt_domains),
        )

    def test_netsuite_beats_web_regardless_of_activity(self):
        from includes.dashboard.supplier_dedup import pick_keep_remove

        ns = self._sup(netsuite_id="NS-1")
        web = self._sup(netsuite_id=None)
        stats = {ns.id: (0, None), web.id: (50, date.today())}
        keep, _remove = pick_keep_remove(ns, web, stats)
        assert keep is ns

    def test_transaction_count_breaks_netsuite_tie(self):
        from includes.dashboard.supplier_dedup import pick_keep_remove

        busy = self._sup(netsuite_id="NS-1")
        quiet = self._sup(netsuite_id="NS-2")
        stats = {busy.id: (10, None), quiet.id: (2, None)}
        keep, _remove = pick_keep_remove(busy, quiet, stats)
        assert keep is busy

    def test_recency_breaks_tie(self):
        from includes.dashboard.supplier_dedup import pick_keep_remove

        stale = self._sup(netsuite_id="NS-1")
        fresh = self._sup(netsuite_id="NS-2")
        stats = {
            stale.id: (3, date.today() - timedelta(days=800)),
            fresh.id: (3, date.today() - timedelta(days=10)),
        }
        keep, _remove = pick_keep_remove(stale, fresh, stats)
        assert keep is fresh

    def test_transaction_cap_does_not_beat_netsuite(self):
        """Even 500 transactions must not outweigh the netsuite_id bonus."""
        from includes.dashboard.supplier_dedup import pick_keep_remove

        ns = self._sup(netsuite_id="NS-1")
        web = self._sup(netsuite_id=None)
        stats = {ns.id: (0, None), web.id: (500, date.today())}
        keep, _remove = pick_keep_remove(ns, web, stats)
        assert keep is ns

    def test_missing_stats_safe_and_tie_keeps_first(self):
        from includes.dashboard.supplier_dedup import pick_keep_remove

        a = self._sup(netsuite_id="NS-1")
        b = self._sup(netsuite_id="NS-2")
        keep, _remove = pick_keep_remove(a, b, None)
        assert keep is a
