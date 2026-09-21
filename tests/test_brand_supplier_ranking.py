"""Brand-linked supplier search: ranking, duplicate brands, duplicate suppliers.

Covers includes/tools/product_tools.py `_find_brand_suppliers_for_brands` /
`_find_brand_suppliers_with_tier` and the RFQ line sort key in
includes/tools/rfq_crud.py.

Brand names carry a random suffix so each test is isolated from real data —
matching is normalised (case/punctuation-insensitive) exact, so a unique
suffix guarantees the family contains only rows created by the test.
"""

import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from includes.dashboard.models import (
    Brand,
    Product,
    Supplier,
    SupplierBrand,
    Transaction,
)
from includes.tools.product_tools import (
    _find_brand_suppliers_for_brands,
    _find_brand_suppliers_with_tier,
)
from includes.tools.rfq_crud import _supplier_sort_key, sort_item_suppliers


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


@pytest.fixture
def patch_session(db_session, monkeypatch):
    monkeypatch.setattr(
        "includes.tools.product_tools.get_session", lambda: db_session
    )
    return db_session


# ── helpers ────────────────────────────────────────────────────────────────

def _brand(session, name=None, duplicate_of=None):
    name = name or f"Brand{uuid.uuid4().hex[:8]}"
    brand = Brand(
        netsuite_id=f"B-{uuid.uuid4().hex[:10]}",
        name=name,
        duplicate_of=duplicate_of,
    )
    session.add(brand)
    session.flush()
    return brand


def _supplier(session, name=None, tier=None, country=None, use_instead=None):
    supplier = Supplier(
        name=name or f"Supplier {uuid.uuid4().hex[:8]}",
        netsuite_id=f"NS-{uuid.uuid4().hex[:10]}",
        source="netsuite",
        country=country,
        use_instead=use_instead,
        supply_chain_position={"tier": tier} if tier else None,
    )
    session.add(supplier)
    session.flush()
    return supplier


def _link(session, supplier, brand):
    session.add(SupplierBrand(supplier_id=supplier.id, brand_id=brand.id))
    session.flush()


def _product(session, brand, part_number=None):
    product = Product(
        part_number=part_number or f"PN-{uuid.uuid4().hex[:8]}",
        brand=brand.name,
        brand_id=brand.id,
    )
    session.add(product)
    session.flush()
    return product


def _txn(session, supplier, product, doc_type="SalesOrder"):
    txn = Transaction(
        doc_number=f"DOC-{uuid.uuid4().hex[:8]}",
        doc_type=doc_type,
        product_id=product.id,
        supplier_id=supplier.id,
        quantity=1,
    )
    session.add(txn)
    session.flush()
    return txn


def _unique_brand_name(prefix="Zorbex"):
    return f"{prefix}{uuid.uuid4().hex[:8]}"


# ── ranking ────────────────────────────────────────────────────────────────

class TestRanking:
    def test_transaction_count_outranks_tier(self, patch_session):
        """Transaction count is the primary measure — a busy tier C supplier
        ranks above a barely-used tier A supplier."""
        session = patch_session
        brand = _brand(session, _unique_brand_name("Rank"))
        product = _product(session, brand)

        tier_a = _supplier(session, "Tier A Low Volume", tier="A")
        tier_c = _supplier(session, "Tier C High Volume", tier="C")
        _link(session, tier_a, brand)
        _link(session, tier_c, brand)
        _txn(session, tier_a, product)
        for _ in range(5):
            _txn(session, tier_c, product)

        rows = _find_brand_suppliers_with_tier(brand.name)
        names = [r["name"] for r in rows]
        assert names == ["Tier C High Volume", "Tier A Low Volume"]
        assert rows[0]["transaction_count"] == 5
        assert rows[1]["transaction_count"] == 1

    def test_tier_breaks_transaction_ties(self, patch_session):
        session = patch_session
        brand = _brand(session, _unique_brand_name("Tie"))
        product = _product(session, brand)

        low_tier = _supplier(session, "Tier D Supplier", tier="D")
        high_tier = _supplier(session, "Tier B Supplier", tier="B")
        untiered = _supplier(session, "No Tier Supplier")
        for s in (low_tier, high_tier, untiered):
            _link(session, s, brand)
            _txn(session, s, product)

        rows = _find_brand_suppliers_with_tier(brand.name)
        assert [r["name"] for r in rows] == [
            "Tier B Supplier", "Tier D Supplier", "No Tier Supplier",
        ]

    def test_zero_transaction_suppliers_rank_last(self, patch_session):
        session = patch_session
        brand = _brand(session, _unique_brand_name("Zero"))
        product = _product(session, brand)

        with_txn = _supplier(session, "Has Transactions", tier="D")
        no_txn = _supplier(session, "No Transactions", tier="A")
        _link(session, with_txn, brand)
        _link(session, no_txn, brand)
        _txn(session, with_txn, product)

        rows = _find_brand_suppliers_with_tier(brand.name)
        assert [r["name"] for r in rows] == ["Has Transactions", "No Transactions"]


# ── duplicate brands ───────────────────────────────────────────────────────

class TestDuplicateBrands:
    def test_links_on_duplicate_brand_are_included(self, patch_session):
        """A supplier linked only to a duplicate brand is found via the
        canonical brand."""
        session = patch_session
        name = _unique_brand_name("DupBrand")
        canonical = _brand(session, name)
        duplicate = _brand(session, name.upper(), duplicate_of=canonical.id)

        supplier = _supplier(session, "Linked Via Duplicate Brand")
        _link(session, supplier, duplicate)

        rows = _find_brand_suppliers_with_tier(name)
        assert [r["name"] for r in rows] == ["Linked Via Duplicate Brand"]

    def test_links_on_canonical_and_duplicate_are_not_double_counted(self, patch_session):
        session = patch_session
        name = _unique_brand_name("Both")
        canonical = _brand(session, name)
        duplicate = _brand(session, name.upper(), duplicate_of=canonical.id)

        supplier = _supplier(session, "Linked To Both")
        _link(session, supplier, canonical)
        _link(session, supplier, duplicate)

        rows = _find_brand_suppliers_with_tier(name)
        assert [r["name"] for r in rows] == ["Linked To Both"]

    def test_search_by_duplicate_brand_name_resolves_to_canonical(self, patch_session):
        """An item whose brand text only matches a duplicate record still
        returns the canonical brand's suppliers."""
        session = patch_session
        name = _unique_brand_name("Alias")
        canonical = _brand(session, name)
        duplicate = _brand(session, f"{name} AUSTRALIA", duplicate_of=canonical.id)

        supplier = _supplier(session, "Canonical Supplier")
        _link(session, supplier, canonical)

        rows = _find_brand_suppliers_with_tier(duplicate.name)
        assert [r["name"] for r in rows] == ["Canonical Supplier"]

    def test_transactions_on_duplicate_brand_products_are_counted(self, patch_session):
        session = patch_session
        name = _unique_brand_name("DupProd")
        canonical = _brand(session, name)
        duplicate = _brand(session, name.upper(), duplicate_of=canonical.id)

        supplier = _supplier(session, "Spans Both Brands")
        _link(session, supplier, canonical)

        _txn(session, supplier, _product(session, canonical))
        _txn(session, supplier, _product(session, duplicate))

        rows = _find_brand_suppliers_with_tier(name)
        assert len(rows) == 1
        assert rows[0]["transaction_count"] == 2

    def test_name_variants_match(self, patch_session):
        """Case, spacing and punctuation variants of the brand name resolve."""
        session = patch_session
        name = _unique_brand_name("Variant")
        brand = _brand(session, name)
        supplier = _supplier(session, "Variant Supplier")
        _link(session, supplier, brand)

        for variant in (name.upper(), name.lower(), f"  {name}  "):
            assert [r["name"] for r in _find_brand_suppliers_with_tier(variant)] == [
                "Variant Supplier"
            ]

    def test_unknown_brand_returns_empty(self, patch_session):
        assert _find_brand_suppliers_with_tier(f"Nope{uuid.uuid4().hex[:8]}") == []


# ── duplicate suppliers ────────────────────────────────────────────────────

class TestDuplicateSuppliers:
    def test_counts_roll_up_and_only_canonical_is_returned(self, patch_session):
        session = patch_session
        brand = _brand(session, _unique_brand_name("MergeSup"))
        product = _product(session, brand)

        primary = _supplier(session, "Canonical Supplier")
        duplicate = _supplier(session, "Canonical Supplier (Old)", use_instead=primary.id)
        _link(session, primary, brand)
        _link(session, duplicate, brand)
        for _ in range(3):
            _txn(session, primary, product)
        for _ in range(2):
            _txn(session, duplicate, product)

        rows = _find_brand_suppliers_with_tier(brand.name)
        assert len(rows) == 1
        entry = rows[0]
        assert entry["supplier_id"] == str(primary.id)
        assert entry["name"] == "Canonical Supplier"
        assert entry["transaction_count"] == 5
        assert entry["brand_transaction_count"] == 5
        assert entry["duplicate_count"] == 1
        assert entry["merged_names"] == ["Canonical Supplier (Old)"]

    def test_duplicate_linked_without_primary_link_attributes_to_primary(self, patch_session):
        """A duplicate record holding the brand link (not yet reassigned) still
        surfaces as its canonical supplier."""
        session = patch_session
        brand = _brand(session, _unique_brand_name("OrphanSup"))
        product = _product(session, brand)

        primary = _supplier(session, "Primary Only")
        duplicate = _supplier(session, "Duplicate Only", use_instead=primary.id)
        _link(session, duplicate, brand)
        _txn(session, duplicate, product)

        rows = _find_brand_suppliers_with_tier(brand.name)
        assert len(rows) == 1
        assert rows[0]["supplier_id"] == str(primary.id)
        assert rows[0]["name"] == "Primary Only"
        assert rows[0]["transaction_count"] == 1
        assert rows[0]["duplicate_count"] == 1

    def test_chained_duplicates_resolve_to_final_primary(self, patch_session):
        session = patch_session
        brand = _brand(session, _unique_brand_name("Chain"))
        product = _product(session, brand)

        a = _supplier(session, "Final Primary")
        b = _supplier(session, "Middle")
        c = _supplier(session, "Oldest")
        b.use_instead = a.id
        c.use_instead = b.id
        session.flush()

        for s in (a, b, c):
            _link(session, s, brand)
            _txn(session, s, product)

        rows = _find_brand_suppliers_with_tier(brand.name)
        assert len(rows) == 1
        assert rows[0]["supplier_id"] == str(a.id)
        assert rows[0]["transaction_count"] == 3
        assert rows[0]["duplicate_count"] == 2


# ── batching ───────────────────────────────────────────────────────────────

class TestBatch:
    def test_multiple_brands_in_one_pass(self, patch_session):
        session = patch_session
        brand_a = _brand(session, _unique_brand_name("BatchA"))
        brand_b = _brand(session, _unique_brand_name("BatchB"))
        sup_a = _supplier(session, "Supplier A")
        sup_b = _supplier(session, "Supplier B")
        _link(session, sup_a, brand_a)
        _link(session, sup_b, brand_b)
        _txn(session, sup_a, _product(session, brand_a))

        result = _find_brand_suppliers_for_brands([brand_a.name, brand_b.name])
        assert set(result) == {brand_a.name, brand_b.name}
        assert [r["name"] for r in result[brand_a.name]] == ["Supplier A"]
        assert [r["name"] for r in result[brand_b.name]] == ["Supplier B"]
        assert result[brand_a.name][0]["transaction_count"] == 1
        assert result[brand_b.name][0]["transaction_count"] == 0

    def test_empty_and_unknown_inputs(self, patch_session):
        unknown = f"None{uuid.uuid4().hex[:8]}"
        assert _find_brand_suppliers_for_brands([]) == {}
        # Blank names are dropped rather than returned as keys — callers use
        # .get(name), so a missing key is the same as an empty list.
        result = _find_brand_suppliers_for_brands(["", unknown])
        assert result[unknown] == []
        assert "" not in result


# ── RFQ line sort key ──────────────────────────────────────────────────────

class TestSupplierSortKey:
    def test_brand_count_is_primary_measure(self):
        busy = {"name": "Busy", "tier": "C", "brand_transaction_count": 9}
        quiet = {"name": "Quiet", "tier": "A", "brand_transaction_count": 1}
        assert sort_item_suppliers([quiet, busy])[0] is busy

    def test_part_transaction_count_used_when_no_brand_count(self):
        with_part_history = {"name": "Part History", "tier": "C", "transaction_count": 4}
        tier_only = {"name": "Tier Only", "tier": "A"}
        assert sort_item_suppliers([tier_only, with_part_history])[0] is with_part_history

    def test_tier_sorts_suppliers_with_no_history(self):
        tier_a = {"name": "A Supplier", "tier": "A"}
        tier_c = {"name": "C Supplier", "tier": "C"}
        assert sort_item_suppliers([tier_c, tier_a])[0] is tier_a

    def test_purchase_ref_counts_as_history(self):
        with_ref = {"name": "Ref Supplier", "tier": "D", "purchase_ref": "PO-1"}
        without = {"name": "No Ref", "tier": "A"}
        assert sort_item_suppliers([without, with_ref])[0] is with_ref

    def test_country_and_name_break_remaining_ties(self):
        au = {"name": "Zed", "tier": "A", "country": "AU"}
        overseas = {"name": "Alpha", "tier": "A", "country": "US"}
        assert sort_item_suppliers([overseas, au])[0] is au
        assert _supplier_sort_key({"name": "Beta"}) < _supplier_sort_key({"name": "Gamma"})
