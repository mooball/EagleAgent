"""Tests for new supplier sourcing functionality in quote_tools.py.

Covers:
- _verify_supplier_url: HTTP HEAD checks, Gemini fallback
- _match_suppliers_to_db: URL verification, web supplier creation, auto-categorization
"""

import uuid
import pytest
from unittest.mock import patch, MagicMock

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from includes.dashboard.models import Base, Supplier
from includes.tools.quote_tools import _verify_supplier_url, _match_suppliers_to_db


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
# _verify_supplier_url
# ============================================================================

class TestVerifySupplierUrl:
    @patch("urllib.request.urlopen")
    def test_valid_url_returns_original(self, mock_urlopen):
        """HTTP 200 → return original URL unchanged."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value = mock_resp

        result = _verify_supplier_url("Acme Corp", "https://acme.com.au")
        assert result == "https://acme.com.au"

    @patch("includes.tools.quote_tools._search_supplier_url", return_value=None)
    @patch("urllib.request.urlopen")
    def test_connection_error_triggers_search(self, mock_urlopen, mock_search):
        """HTTP failure → calls _search_supplier_url."""
        mock_urlopen.side_effect = ConnectionError("refused")

        result = _verify_supplier_url("Western Filters", "https://westernfilters.com.au", "AU")
        mock_search.assert_called_once_with("Western Filters", "AU", product_hint="")
        # Falls back to original URL when search returns None
        assert result == "https://westernfilters.com.au"

    @patch("includes.tools.quote_tools._search_supplier_url", return_value="https://btpgroup.com.au")
    @patch("urllib.request.urlopen")
    def test_search_returns_corrected_url(self, mock_urlopen, mock_search):
        """Failed HEAD + successful search → return corrected URL."""
        mock_urlopen.side_effect = ConnectionError("refused")

        result = _verify_supplier_url("BTP Group", "https://btp.com.au", "AU")
        assert result == "https://btpgroup.com.au"

    @patch("includes.tools.quote_tools._search_supplier_url")
    def test_none_url_triggers_search(self, mock_search):
        """No URL at all → calls search directly."""
        mock_search.return_value = "https://acme.com.au"
        result = _verify_supplier_url("Acme Corp", None, "AU")
        assert result == "https://acme.com.au"
        mock_search.assert_called_once_with("Acme Corp", "AU", product_hint="")

    @patch("includes.tools.quote_tools._search_supplier_url", return_value=None)
    def test_none_url_and_no_search_result(self, mock_search):
        """No URL, search also fails → None."""
        result = _verify_supplier_url("Unknown Corp", None)
        assert result is None

    @patch("includes.tools.quote_tools._search_supplier_url", return_value=None)
    @patch("urllib.request.urlopen")
    def test_non_200_triggers_search(self, mock_urlopen, mock_search):
        """Non-200 status → triggers search, falls back to original."""
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_urlopen.return_value = mock_resp

        result = _verify_supplier_url("Parked Domain", "https://parked.com.au")
        mock_search.assert_called_once()
        assert result == "https://parked.com.au"

    @patch("urllib.request.urlopen")
    def test_product_hint_passed_to_search(self, mock_urlopen):
        """Product hint should be forwarded to _search_supplier_url."""
        mock_urlopen.side_effect = ConnectionError("refused")

        with patch("includes.tools.quote_tools._search_supplier_url", return_value=None) as mock_search:
            _verify_supplier_url(
                "Acme Corp", "https://bad.com.au", "AU",
                product_hint="DHP486Z Makita cordless drill",
            )
        mock_search.assert_called_once_with(
            "Acme Corp", "AU", product_hint="DHP486Z Makita cordless drill",
        )

    @patch("urllib.request.urlopen")
    def test_url_without_scheme_gets_https(self, mock_urlopen):
        """URL without scheme should still be checked with https:// prefix."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value = mock_resp

        result = _verify_supplier_url("Acme", "acme.com.au")
        assert result == "acme.com.au"
        # The Request should have been made with https://acme.com.au
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.full_url == "https://acme.com.au"


# ============================================================================
# _match_suppliers_to_db
# ============================================================================

class TestMatchSuppliersToDb:
    @patch("includes.tools.quote_tools._verify_supplier_url", return_value="https://zyxmatchtest.com.au")
    def test_matches_existing_supplier(self, mock_verify, db_session):
        """Existing DB supplier should be matched and supplier_id set."""
        sup = Supplier(
            id=uuid.uuid4(),
            name="ZyxMatchTest Tools",
            url="https://zyxmatchtest.com.au",
            country="AU",
            contacts=[{"email": "sales@zyxmatchtest.com.au"}],
            source="netsuite",
        )
        db_session.add(sup)
        db_session.flush()

        suppliers = [{
            "name": "ZyxMatchTest Tools",
            "country": "AU",
            "contacts": [{"url": "https://zyxmatchtest.com.au"}],
        }]

        with patch("includes.dashboard.database.get_session", return_value=db_session):
            _match_suppliers_to_db(suppliers)

        assert suppliers[0].get("supplier_id") == str(sup.id)

    @patch("includes.tools.quote_tools._verify_supplier_url", return_value="https://zyxnewco.com.au")
    def test_creates_new_web_supplier(self, mock_verify, db_session):
        """Unknown supplier should create a new Supplier record with source='web'."""
        suppliers = [{
            "name": "ZyxNewCo Supplies",
            "country": "AU",
            "currency": "AUD",
            "contacts": [{"url": "https://zyxnewco.com.au", "email": "info@zyxnewco.com.au"}],
        }]

        with patch("includes.dashboard.database.get_session", return_value=db_session), \
             patch("includes.supplier_categorization.categorize_supplier", side_effect=Exception("skip")):
            _match_suppliers_to_db(suppliers)

        assert suppliers[0].get("supplier_id") is not None

        # Verify the DB record
        new_sup = db_session.query(Supplier).filter(
            Supplier.name == "ZyxNewCo Supplies"
        ).first()
        assert new_sup is not None
        assert new_sup.source == "web"
        assert new_sup.country == "AU"
        assert new_sup.currency == "AUD"

    @patch("includes.tools.quote_tools._verify_supplier_url", return_value="https://zyxnewcat.com.au")
    def test_creates_web_supplier_with_auto_categorization(self, mock_verify, db_session):
        """New web supplier should be auto-categorized when possible."""
        suppliers = [{
            "name": "ZyxNewCat Supplies",
            "country": "AU",
            "currency": "AUD",
            "contacts": [{"url": "https://zyxnewcat.com.au"}],
        }]

        mock_cat_result = {
            "category": "Industrial Supplies",
            "tier": "Distributor",
            "confidence": 0.85,
            "reasoning": "General industrial distributor",
        }

        with patch("includes.dashboard.database.get_session", return_value=db_session), \
             patch("includes.supplier_categorization.categorize_supplier", return_value=mock_cat_result), \
             patch("includes.supplier_categorization.load_taxonomy", return_value="taxonomy text"), \
             patch("google.genai.Client") as mock_client:
            mock_client.return_value = MagicMock()
            _match_suppliers_to_db(suppliers)

        new_sup = db_session.query(Supplier).filter(
            Supplier.name == "ZyxNewCat Supplies"
        ).first()
        assert new_sup is not None
        assert new_sup.supply_chain_position["category"] == "Industrial Supplies"
        assert new_sup.supply_chain_position["tier"] == "Distributor"
        # The supplier dict should also be updated
        assert suppliers[0].get("tier") == "Distributor"
        assert suppliers[0].get("category") == "Industrial Supplies"

    @patch("includes.tools.quote_tools._verify_supplier_url", return_value="https://acme.com.au")
    def test_skips_suppliers_with_existing_id(self, mock_verify, db_session):
        """Suppliers that already have a supplier_id should be skipped."""
        existing_id = str(uuid.uuid4())
        suppliers = [{
            "name": "Already Matched",
            "supplier_id": existing_id,
            "contacts": [],
        }]

        with patch("includes.dashboard.database.get_session", return_value=db_session):
            _match_suppliers_to_db(suppliers)

        # Should not have tried to verify or change anything
        mock_verify.assert_not_called()
        assert suppliers[0]["supplier_id"] == existing_id

    @patch("includes.tools.quote_tools._verify_supplier_url")
    def test_db_matched_supplier_skips_url_verification(self, mock_verify, db_session):
        """When a supplier is already in the DB, URL verification should be skipped."""
        sup = Supplier(
            id=uuid.uuid4(),
            name="BTPZZUnique Test Co",
            url="https://btpzzunique.com.au",
            country="AU",
            contacts=[{"email": "info@btpzzunique.com.au"}],
            source="netsuite",
        )
        db_session.add(sup)
        db_session.flush()

        suppliers = [{
            "name": "BTPZZUnique Test Co",
            "country": "AU",
            "contacts": [{"url": "https://btp.com.au", "email": "info@btp.com.au"}],
        }]

        with patch("includes.dashboard.database.get_session", return_value=db_session):
            _match_suppliers_to_db(suppliers)

        # DB match should have succeeded
        assert suppliers[0].get("supplier_id") is not None
        # URL verification should NOT have been called (DB is authoritative)
        mock_verify.assert_not_called()

    @patch("includes.tools.quote_tools._verify_supplier_url", return_value=None)
    def test_empty_suppliers_list(self, mock_verify):
        """Empty list should be a no-op."""
        _match_suppliers_to_db([])
        mock_verify.assert_not_called()

    @patch("includes.tools.quote_tools._verify_supplier_url", return_value="https://zyxhinttest-unique.com.au")
    def test_product_hint_forwarded(self, mock_verify, db_session):
        """product_hint should be passed to _verify_supplier_url for a truly new supplier."""
        suppliers = [{
            "name": "ZyxHintTest-UNIQUE-XYZ Supplier",
            "contacts": [{"url": "https://zyxhinttest-unique.com.au"}],
        }]

        with patch("includes.dashboard.database.get_session", return_value=db_session):
            _match_suppliers_to_db(suppliers, product_hint="DHP486Z Makita drill")

        mock_verify.assert_called_once()
        _, kwargs = mock_verify.call_args
        assert kwargs["product_hint"] == "DHP486Z Makita drill"
