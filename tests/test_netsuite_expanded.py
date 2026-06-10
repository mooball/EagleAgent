"""Tests for NetSuite expanded sync scripts: opportunities, customers, contacts, employee mappings.

Covers:
- Opportunity sync: upsert logic, customer/salesrep FK resolution
- Customer sync: upsert logic, contact list parsing
- Transaction sync: opportunity FK resolution via map_line_to_transaction
- Employee mapping validation
- Foreign key constraints between tables
"""

import uuid
import pytest
from unittest.mock import patch, MagicMock

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from includes.dashboard.models import (
    Base,
    Customer,
    Contact,
    Opportunity,
    NetSuiteEmployeeMapping,
    Product,
    Supplier,
    Transaction,
)
from includes.netsuite.queries import (
    opportunities_updated_since,
    customers_updated_since,
    contacts_updated_since,
)
from includes.netsuite.sync_utils import parse_netsuite_date, normalize_currency
from scripts.sync_netsuite_customers import parse_contact_list
from scripts.sync_netsuite_quotes import (
    build_lookup_maps as quotes_build_lookup_maps,
    map_line_to_transaction as quotes_map_line,
)
from scripts.sync_netsuite_sales_orders import (
    build_lookup_maps as so_build_lookup_maps,
    map_line_to_transaction as so_map_line,
)


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
# Query Builder Tests
# ============================================================================

class TestExpandedQueries:
    def test_opportunities_updated_since(self):
        sql = opportunities_updated_since("2026-03-15")
        assert "15/3/2026" in sql
        assert "opportunity" in sql.lower()
        assert "ORDER BY" in sql

    def test_customers_updated_since(self):
        sql = customers_updated_since("2026-06-01")
        assert "1/6/2026" in sql
        assert "customer" in sql.lower()
        assert "ORDER BY" in sql

    def test_contacts_updated_since(self):
        sql = contacts_updated_since("2026-01-10")
        assert "10/1/2026" in sql
        assert "contact" in sql.lower()
        assert "ORDER BY" in sql


# ============================================================================
# Contact List Parsing
# ============================================================================

class TestParseContactList:
    def test_normal_list(self):
        assert parse_contact_list("5, 76893, 124327") == ["5", "76893", "124327"]

    def test_empty_string(self):
        assert parse_contact_list("") == []

    def test_none(self):
        assert parse_contact_list(None) == []

    def test_single_value(self):
        assert parse_contact_list("12345") == ["12345"]

    def test_whitespace_handling(self):
        assert parse_contact_list("  1 , 2 , 3  ") == ["1", "2", "3"]

    def test_trailing_comma(self):
        assert parse_contact_list("1,2,") == ["1", "2"]


# ============================================================================
# Employee Mapping Validation
# ============================================================================

class TestEmployeeMappings:
    def test_mapping_exists_in_db(self, db_session):
        """Verify employee mappings are populated."""
        count = db_session.query(NetSuiteEmployeeMapping).count()
        assert count >= 10, f"Expected at least 10 employee mappings, got {count}"

    def test_known_employee_bill_watt(self, db_session):
        """Verify Bill Watt mapping (netsuite_id -5)."""
        emp = db_session.query(NetSuiteEmployeeMapping).filter(
            NetSuiteEmployeeMapping.netsuite_employee_id == "-5"
        ).first()
        assert emp is not None
        assert emp.name == "Bill Watt"
        assert emp.email == "bill@eagle-exports.com"

    def test_inactive_employees_marked(self, db_session):
        """Inactive employees should have is_active=False."""
        inactive = db_session.query(NetSuiteEmployeeMapping).filter(
            NetSuiteEmployeeMapping.is_active == False
        ).all()
        assert len(inactive) >= 7, "Expected at least 7 inactive employees"

    def test_unique_netsuite_ids(self, db_session):
        """All netsuite_employee_id values must be unique."""
        from sqlalchemy import func
        total = db_session.query(NetSuiteEmployeeMapping).count()
        distinct = db_session.query(
            func.count(NetSuiteEmployeeMapping.netsuite_employee_id.distinct())
        ).scalar()
        assert total == distinct


# ============================================================================
# Opportunity Sync Tests
# ============================================================================

class TestOpportunitySync:
    def test_opportunity_insert(self, db_session):
        """Test inserting a new opportunity."""
        opp = Opportunity(
            netsuite_id="99999",
            opportunity_number="OP9999",
            title="Test Opportunity",
            status="B",
            total=5000.0,
            currency="AUD",
        )
        db_session.add(opp)
        db_session.flush()

        result = db_session.query(Opportunity).filter(
            Opportunity.netsuite_id == "99999"
        ).first()
        assert result is not None
        assert result.title == "Test Opportunity"
        assert result.status == "B"

    def test_opportunity_customer_fk(self, db_session):
        """Test opportunity links to customer via FK."""
        customer = db_session.query(Customer).first()
        if not customer:
            pytest.skip("No customers in test DB")

        opp = Opportunity(
            netsuite_id="99998",
            opportunity_number="OP9998",
            title="Linked Opp",
            customer_id=customer.id,
            netsuite_customer_id=customer.netsuite_id,
        )
        db_session.add(opp)
        db_session.flush()

        result = db_session.query(Opportunity).filter(
            Opportunity.netsuite_id == "99998"
        ).first()
        assert result.customer_id == customer.id

    def test_opportunity_salesrep_fk(self, db_session):
        """Test opportunity links to salesrep via FK."""
        emp = db_session.query(NetSuiteEmployeeMapping).first()
        if not emp:
            pytest.skip("No employee mappings in test DB")

        opp = Opportunity(
            netsuite_id="99997",
            opportunity_number="OP9997",
            title="Rep Opp",
            salesrep_id=emp.id,
            netsuite_salesrep_id=emp.netsuite_employee_id,
        )
        db_session.add(opp)
        db_session.flush()

        result = db_session.query(Opportunity).filter(
            Opportunity.netsuite_id == "99997"
        ).first()
        assert result.salesrep_id == emp.id

    def test_opportunity_upsert_updates_existing(self, db_session):
        """Test that syncing an existing opportunity updates fields."""
        opp = Opportunity(
            netsuite_id="99996",
            opportunity_number="OP9996",
            title="Original Title",
            status="B",
            total=1000.0,
        )
        db_session.add(opp)
        db_session.flush()

        # Simulate re-sync with updated data
        existing = db_session.query(Opportunity).filter(
            Opportunity.netsuite_id == "99996"
        ).first()
        existing.title = "Updated Title"
        existing.status = "A"
        existing.total = 2500.0
        db_session.flush()

        result = db_session.query(Opportunity).filter(
            Opportunity.netsuite_id == "99996"
        ).first()
        assert result.title == "Updated Title"
        assert result.status == "A"
        assert result.total == 2500.0


# ============================================================================
# Customer Sync Tests
# ============================================================================

class TestCustomerSync:
    def test_customer_insert(self, db_session):
        """Test inserting a new customer."""
        cust = Customer(
            netsuite_id="88888",
            entity_code="TEST-CUST",
            companyname="Test Company Pty Ltd",
            email="test@example.com",
            isinactive=False,
            currency="AUD",
        )
        db_session.add(cust)
        db_session.flush()

        result = db_session.query(Customer).filter(
            Customer.netsuite_id == "88888"
        ).first()
        assert result is not None
        assert result.companyname == "Test Company Pty Ltd"
        assert result.isinactive == False

    def test_customer_upsert(self, db_session):
        """Test updating existing customer."""
        cust = Customer(
            netsuite_id="88887",
            entity_code="UPSERT-TEST",
            companyname="Old Name",
            isinactive=False,
        )
        db_session.add(cust)
        db_session.flush()

        existing = db_session.query(Customer).filter(
            Customer.netsuite_id == "88887"
        ).first()
        existing.companyname = "New Name"
        existing.email = "new@example.com"
        db_session.flush()

        result = db_session.query(Customer).filter(
            Customer.netsuite_id == "88887"
        ).first()
        assert result.companyname == "New Name"
        assert result.email == "new@example.com"

    def test_customer_contact_fk(self, db_session):
        """Test contact links to customer via FK."""
        cust = Customer(
            netsuite_id="88886",
            entity_code="CONTACT-TEST",
            companyname="Contact Parent",
            isinactive=False,
        )
        db_session.add(cust)
        db_session.flush()

        contact = Contact(
            netsuite_id="77777",
            customer_id=cust.id,
            fullname="John Smith",
            email="john@example.com",
            isinactive=False,
        )
        db_session.add(contact)
        db_session.flush()

        result = db_session.query(Contact).filter(
            Contact.netsuite_id == "77777"
        ).first()
        assert result.customer_id == cust.id
        assert result.fullname == "John Smith"


# ============================================================================
# Transaction → Opportunity Link Tests
# ============================================================================

class TestTransactionOpportunityLink:
    def test_map_line_resolves_opportunity(self):
        """Test map_line_to_transaction resolves opportunity ID."""
        product_id = uuid.uuid4()
        supplier_id = uuid.uuid4()
        opp_id = uuid.uuid4()

        product_map = {"100": product_id}
        supplier_map = {"200": supplier_id}
        opportunity_map = {"300": opp_id}

        row = {
            "uniquekey": "12345",
            "tranid": "Q001",
            "trandate": "1/6/2026",
            "item": "100",
            "custcol_po_vendor": "200",
            "quantity": "10",
            "rate": "50.00",
            "custcol_po_rate": "30.00",
            "currency_name": "Australian Dollar",
            "status": "A",
            "lastmodifieddate": "1/6/2026",
            "opportunity": "300",
        }

        result = quotes_map_line(row, product_map, supplier_map, opportunity_map)
        assert result is not None
        assert result["opportunity_id"] == opp_id
        assert result["netsuite_opportunity_id"] == "300"

    def test_map_line_null_opportunity(self):
        """Test map_line_to_transaction handles null opportunity."""
        product_id = uuid.uuid4()
        supplier_id = uuid.uuid4()

        product_map = {"100": product_id}
        supplier_map = {"200": supplier_id}
        opportunity_map = {}

        row = {
            "uniquekey": "12346",
            "tranid": "Q002",
            "trandate": "1/6/2026",
            "item": "100",
            "custcol_po_vendor": "200",
            "quantity": "5",
            "rate": "25.00",
            "custcol_po_rate": None,
            "currency_name": "AUD",
            "status": "B",
            "lastmodifieddate": "2/6/2026",
            "opportunity": None,
        }

        result = quotes_map_line(row, product_map, supplier_map, opportunity_map)
        assert result is not None
        assert result["opportunity_id"] is None
        assert result["netsuite_opportunity_id"] is None

    def test_map_line_unresolved_opportunity(self):
        """Test map_line_to_transaction handles unresolvable opportunity ID."""
        product_id = uuid.uuid4()
        supplier_id = uuid.uuid4()

        product_map = {"100": product_id}
        supplier_map = {"200": supplier_id}
        opportunity_map = {}  # empty — no local opp with ID 999

        row = {
            "uniquekey": "12347",
            "tranid": "SO003",
            "trandate": "5/6/2026",
            "item": "100",
            "custcol_po_vendor": "200",
            "quantity": "1",
            "rate": "100.00",
            "custcol_po_rate": "80.00",
            "currency_name": "USD",
            "status": "B",
            "lastmodifieddate": "5/6/2026",
            "opportunity": "999",
        }

        result = so_map_line(row, product_map, supplier_map, opportunity_map)
        assert result is not None
        assert result["netsuite_opportunity_id"] == "999"
        assert result["opportunity_id"] is None  # can't resolve

    def test_map_line_returns_none_missing_product(self):
        """Test that missing product returns None."""
        supplier_id = uuid.uuid4()

        product_map = {}
        supplier_map = {"200": supplier_id}
        opportunity_map = {}

        row = {
            "uniquekey": "12348",
            "tranid": "Q003",
            "trandate": "1/6/2026",
            "item": "UNKNOWN",
            "custcol_po_vendor": "200",
            "quantity": "1",
            "rate": "10",
            "custcol_po_rate": None,
            "currency_name": "AUD",
            "status": "A",
            "lastmodifieddate": "1/6/2026",
            "opportunity": None,
        }

        result = quotes_map_line(row, product_map, supplier_map, opportunity_map)
        assert result is None

    def test_transaction_opportunity_fk_in_db(self, db_session):
        """Test that transaction.opportunity_id FK works in the database."""
        opp = Opportunity(
            netsuite_id="55555",
            opportunity_number="OP5555",
            title="FK Test Opp",
        )
        db_session.add(opp)
        db_session.flush()

        # Need a product and supplier for the transaction
        product = db_session.query(Product).first()
        supplier = db_session.query(Supplier).first()
        if not product or not supplier:
            pytest.skip("No products/suppliers in test DB")

        txn = Transaction(
            doc_number="TEST-001",
            doc_type="Quote",
            netsuite_id="TXNTEST99999",
            product_id=product.id,
            supplier_id=supplier.id,
            opportunity_id=opp.id,
            netsuite_opportunity_id="55555",
        )
        db_session.add(txn)
        db_session.flush()

        result = db_session.query(Transaction).filter(
            Transaction.netsuite_id == "TXNTEST99999"
        ).first()
        assert result.opportunity_id == opp.id
        assert result.netsuite_opportunity_id == "55555"


# ============================================================================
# Build Lookup Maps Tests
# ============================================================================

class TestBuildLookupMaps:
    def test_quotes_build_lookup_maps(self, db_session):
        """Test that build_lookup_maps returns three maps."""
        product_map, supplier_map, opportunity_map = quotes_build_lookup_maps(db_session)
        assert isinstance(product_map, dict)
        assert isinstance(supplier_map, dict)
        assert isinstance(opportunity_map, dict)

    def test_so_build_lookup_maps(self, db_session):
        """Test that SO build_lookup_maps returns three maps."""
        product_map, supplier_map, opportunity_map = so_build_lookup_maps(db_session)
        assert isinstance(product_map, dict)
        assert isinstance(supplier_map, dict)
        assert isinstance(opportunity_map, dict)

    def test_opportunity_map_populated(self, db_session):
        """Verify opportunity_map picks up existing opportunities."""
        count = db_session.query(Opportunity).filter(
            Opportunity.netsuite_id.isnot(None)
        ).count()
        _, _, opportunity_map = quotes_build_lookup_maps(db_session)
        assert len(opportunity_map) == count


# ============================================================================
# Currency Normalization (used in expanded sync)
# ============================================================================

class TestCurrencyNormalization:
    def test_builtin_df_values(self):
        assert normalize_currency("Australian Dollar") == "AUD"
        assert normalize_currency("US Dollar") == "USD"
        assert normalize_currency("Euro") == "EUR"
        assert normalize_currency("British Pound") == "GBP"

    def test_already_iso(self):
        assert normalize_currency("AUD") == "AUD"
        assert normalize_currency("USD") == "USD"

    def test_none_returns_none(self):
        assert normalize_currency(None) is None

    def test_empty_returns_none(self):
        assert normalize_currency("") is None


# ============================================================================
# Foreign Key Constraints
# ============================================================================

class TestForeignKeyConstraints:
    def test_opportunity_invalid_customer_fk_raises(self, db_session):
        """Inserting an opportunity with a non-existent customer_id should fail."""
        from sqlalchemy.exc import IntegrityError
        fake_uuid = uuid.uuid4()
        opp = Opportunity(
            netsuite_id="FK-FAIL-1",
            opportunity_number="OPFAIL",
            customer_id=fake_uuid,
        )
        db_session.add(opp)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_transaction_invalid_opportunity_fk_raises(self, db_session):
        """Inserting a transaction with a non-existent opportunity_id should fail."""
        from sqlalchemy.exc import IntegrityError

        product = db_session.query(Product).first()
        supplier = db_session.query(Supplier).first()
        if not product or not supplier:
            pytest.skip("No products/suppliers in test DB")

        fake_uuid = uuid.uuid4()
        txn = Transaction(
            doc_number="FK-FAIL-2",
            doc_type="Quote",
            netsuite_id="FKFAIL2",
            product_id=product.id,
            supplier_id=supplier.id,
            opportunity_id=fake_uuid,
        )
        db_session.add(txn)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_contact_invalid_customer_fk_raises(self, db_session):
        """Inserting a contact with a non-existent customer_id should fail."""
        from sqlalchemy.exc import IntegrityError
        fake_uuid = uuid.uuid4()
        contact = Contact(
            netsuite_id="FK-FAIL-3",
            customer_id=fake_uuid,
            fullname="Ghost",
            isinactive=False,
        )
        db_session.add(contact)
        with pytest.raises(IntegrityError):
            db_session.flush()
