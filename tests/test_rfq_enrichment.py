"""Tests for RFQ supplier normalization and enrichment in routes.py.

Covers:
- _normalize_rfq_suppliers: new default keys (country, currency, tier, category, source, is_new)
- _enrich_rfq_supplier_contacts: is_new flag logic, transaction history checks
"""

import uuid
import pytest
from unittest.mock import patch, MagicMock

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from includes.dashboard.models import Base, Supplier, Transaction, Product
from includes.dashboard.routes import _normalize_rfq_suppliers, _enrich_rfq_supplier_contacts


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
# _normalize_rfq_suppliers
# ============================================================================

class TestNormalizeRfqSuppliers:
    def test_adds_all_default_keys(self):
        """All expected keys should be present after normalization."""
        rfq = {
            "items": [{
                "suppliers": [{"name": "Acme Corp"}],
            }],
        }
        _normalize_rfq_suppliers(rfq)
        sup = rfq["items"][0]["suppliers"][0]
        assert sup["name"] == "Acme Corp"
        assert sup["country"] is None
        assert sup["currency"] is None
        assert sup["tier"] is None
        assert sup["category"] is None
        assert sup["source"] is None
        assert sup["is_new"] is False
        assert sup["contacts"] == []
        assert sup["status"] == "candidate"
        assert sup["supplier_id"] is None

    def test_preserves_existing_values(self):
        """Existing values should not be overwritten by defaults."""
        rfq = {
            "items": [{
                "suppliers": [{
                    "name": "Acme Corp",
                    "country": "AU",
                    "currency": "AUD",
                    "tier": "Manufacturer",
                    "is_new": True,
                    "source": "web",
                }],
            }],
        }
        _normalize_rfq_suppliers(rfq)
        sup = rfq["items"][0]["suppliers"][0]
        assert sup["country"] == "AU"
        assert sup["currency"] == "AUD"
        assert sup["tier"] == "Manufacturer"
        assert sup["is_new"] is True
        assert sup["source"] == "web"

    def test_string_supplier_converted(self):
        """Bare string suppliers should be converted to dicts with defaults."""
        rfq = {
            "items": [{
                "suppliers": ["Old String Supplier"],
            }],
        }
        _normalize_rfq_suppliers(rfq)
        sup = rfq["items"][0]["suppliers"][0]
        assert sup["name"] == "Old String Supplier"
        assert sup["is_new"] is False
        assert sup["source"] is None

    def test_non_list_contacts_fixed(self):
        """contacts that aren't a list should be replaced with []."""
        rfq = {
            "items": [{
                "suppliers": [{"name": "Broken", "contacts": "not-a-list"}],
            }],
        }
        _normalize_rfq_suppliers(rfq)
        assert rfq["items"][0]["suppliers"][0]["contacts"] == []

    def test_empty_items(self):
        """RFQ with no items should be a no-op."""
        rfq = {"items": []}
        _normalize_rfq_suppliers(rfq)
        assert rfq["items"] == []


# ============================================================================
# _enrich_rfq_supplier_contacts — is_new flag
# ============================================================================

class TestEnrichIsNew:
    def test_supplier_with_transactions_not_new(self, db_session):
        """Supplier with transaction history → is_new should be False."""
        product = Product(
            id=uuid.uuid4(),
            part_number="TEST-001",
            description="Test part",
        )
        sup = Supplier(
            id=uuid.uuid4(),
            name="Established Supplier",
            country="AU",
            contacts=[{"email": "info@established.com.au"}],
            source="netsuite",
        )
        db_session.add_all([product, sup])
        db_session.flush()

        # Create a transaction for this supplier
        txn = Transaction(
            id=uuid.uuid4(),
            doc_number="SO-001",
            doc_type="SalesOrder",
            product_id=product.id,
            supplier_id=sup.id,
            quantity=10,
        )
        db_session.add(txn)
        db_session.flush()

        rfq = {
            "items": [{
                "suppliers": [{
                    "name": "Established Supplier",
                    "supplier_id": str(sup.id),
                }],
            }],
        }

        with patch("includes.dashboard.routes.get_session", return_value=db_session):
            _enrich_rfq_supplier_contacts(rfq)

        assert rfq["items"][0]["suppliers"][0]["is_new"] is False

    def test_supplier_without_transactions_is_new(self, db_session):
        """Supplier with no transactions → is_new should be True."""
        sup = Supplier(
            id=uuid.uuid4(),
            name="New Web Supplier",
            country="AU",
            contacts=[{"email": "info@newsupplier.com.au"}],
            source="web",
        )
        db_session.add(sup)
        db_session.flush()

        rfq = {
            "items": [{
                "suppliers": [{
                    "name": "New Web Supplier",
                    "supplier_id": str(sup.id),
                }],
            }],
        }

        with patch("includes.dashboard.routes.get_session", return_value=db_session):
            _enrich_rfq_supplier_contacts(rfq)

        assert rfq["items"][0]["suppliers"][0]["is_new"] is True

    def test_supplier_without_id_always_new(self, db_session):
        """Supplier with no supplier_id at all → always is_new."""
        rfq = {
            "items": [{
                "suppliers": [{
                    "name": "Unknown Supplier",
                }],
            }],
        }

        with patch("includes.dashboard.routes.get_session", return_value=db_session):
            _enrich_rfq_supplier_contacts(rfq)

        assert rfq["items"][0]["suppliers"][0]["is_new"] is True

    def test_mixed_suppliers_correct_flags(self, db_session):
        """Mix of established, new-DB, and unknown suppliers."""
        product = Product(
            id=uuid.uuid4(),
            part_number="MIX-001",
            description="Test",
        )
        established = Supplier(
            id=uuid.uuid4(),
            name="Established Co",
            country="AU",
            contacts=[{"email": "e@est.com"}],
            source="netsuite",
        )
        new_db = Supplier(
            id=uuid.uuid4(),
            name="New DB Supplier",
            country="AU",
            contacts=[{"email": "n@newdb.com"}],
            source="web",
        )
        db_session.add_all([product, established, new_db])
        db_session.flush()

        # Only established has transactions
        txn = Transaction(
            id=uuid.uuid4(),
            doc_number="SO-MIX",
            doc_type="SalesOrder",
            product_id=product.id,
            supplier_id=established.id,
            quantity=5,
        )
        db_session.add(txn)
        db_session.flush()

        rfq = {
            "items": [{
                "suppliers": [
                    {"name": "Established Co", "supplier_id": str(established.id)},
                    {"name": "New DB Supplier", "supplier_id": str(new_db.id)},
                    {"name": "Totally Unknown"},
                ],
            }],
        }

        with patch("includes.dashboard.routes.get_session", return_value=db_session):
            _enrich_rfq_supplier_contacts(rfq)

        sups = rfq["items"][0]["suppliers"]
        assert sups[0]["is_new"] is False, "Established supplier should not be new"
        assert sups[1]["is_new"] is True, "DB supplier without transactions should be new"
        assert sups[2]["is_new"] is True, "Unknown supplier should be new"

    def test_enrichment_fills_source(self, db_session):
        """source should be populated from DB during enrichment."""
        sup = Supplier(
            id=uuid.uuid4(),
            name="Web Source Test",
            contacts=[{"email": "test@example.com"}],
            source="web",
        )
        db_session.add(sup)
        db_session.flush()

        rfq = {
            "items": [{
                "suppliers": [{
                    "name": "Web Source Test",
                    "supplier_id": str(sup.id),
                }],
            }],
        }

        with patch("includes.dashboard.routes.get_session", return_value=db_session):
            _enrich_rfq_supplier_contacts(rfq)

        assert rfq["items"][0]["suppliers"][0]["source"] == "web"

    def test_enrichment_fills_tier_and_category(self, db_session):
        """tier and category should be pulled from supply_chain_position."""
        sup = Supplier(
            id=uuid.uuid4(),
            name="Categorized Supplier",
            contacts=[],
            source="web",
            supply_chain_position={
                "tier": "Manufacturer",
                "category": "Heavy Equipment",
                "confidence": 0.9,
            },
        )
        db_session.add(sup)
        db_session.flush()

        rfq = {
            "items": [{
                "suppliers": [{
                    "name": "Categorized Supplier",
                    "supplier_id": str(sup.id),
                }],
            }],
        }

        with patch("includes.dashboard.routes.get_session", return_value=db_session):
            _enrich_rfq_supplier_contacts(rfq)

        s = rfq["items"][0]["suppliers"][0]
        assert s["tier"] == "Manufacturer"
        assert s["category"] == "Heavy Equipment"

    def test_enrichment_fills_country_currency(self, db_session):
        """country and currency should be populated from DB when missing."""
        sup = Supplier(
            id=uuid.uuid4(),
            name="Geo Supplier",
            country="NZ",
            currency="NZD",
            contacts=[],
            source="netsuite",
        )
        db_session.add(sup)
        db_session.flush()

        rfq = {
            "items": [{
                "suppliers": [{
                    "name": "Geo Supplier",
                    "supplier_id": str(sup.id),
                }],
            }],
        }

        with patch("includes.dashboard.routes.get_session", return_value=db_session):
            _enrich_rfq_supplier_contacts(rfq)

        s = rfq["items"][0]["suppliers"][0]
        assert s["country"] == "NZ"
        assert s["currency"] == "NZD"
