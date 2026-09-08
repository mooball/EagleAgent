"""Tests for includes/gmail/matching.py — domain extraction and email matching."""

import pytest
from unittest.mock import MagicMock, patch

from includes.gmail.matching import (
    extract_domain,
    extract_domain_from_url,
    match_by_subject,
    build_domain_index,
    find_all_matches,
    _GENERIC_DOMAINS,
)


class TestExtractDomain:
    def test_simple_email(self):
        assert extract_domain("john@example.com") == "example.com"

    def test_subdomain_email(self):
        assert extract_domain("john@mail.example.com") == "mail.example.com"

    def test_case_insensitive(self):
        assert extract_domain("John@EXAMPLE.com") == "example.com"

    def test_whitespace_email(self):
        assert extract_domain(" john@example.com ") == "example.com"

    def test_empty_email(self):
        assert extract_domain("") is None

    def test_no_at_sign(self):
        assert extract_domain("notanemail") is None

    def test_none_email(self):
        assert extract_domain(None) is None


class TestExtractDomainFromUrl:
    def test_https_url(self):
        assert extract_domain_from_url("https://www.example.com/page") == "example.com"

    def test_http_url(self):
        assert extract_domain_from_url("http://example.com") == "example.com"

    def test_url_without_scheme(self):
        assert extract_domain_from_url("example.com") == "example.com"

    def test_url_with_www(self):
        assert extract_domain_from_url("https://www.example.co.uk") == "example.co.uk"

    def test_url_with_path_and_query(self):
        assert extract_domain_from_url("https://example.com/path?q=1&x=2") == "example.com"

    def test_trailing_slash(self):
        assert extract_domain_from_url("https://example.com/") == "example.com"

    def test_empty_url(self):
        assert extract_domain_from_url("") is None

    def test_none_url(self):
        assert extract_domain_from_url(None) is None

    def test_invalid_url(self):
        # Should not raise — returns None on parse failure
        result = extract_domain_from_url("not a url at all !!!")
        assert result is None or isinstance(result, str)


class TestMatchBySubject:
    def test_rfq_pattern(self):
        result = match_by_subject("Re: Quote request [RFQ-2026-0042]")
        assert result["rfq_number"] == "2026-0042"

    def test_rfq_without_brackets(self):
        result = match_by_subject("RFQ-2026-0042 supplier quote")
        assert result["rfq_number"] == "2026-0042"

    def test_opportunity_pattern(self):
        result = match_by_subject("Opportunity OP12345 update")
        assert result["opportunity_number"] == "OP12345"

    def test_both_rfq_and_op(self):
        result = match_by_subject("Re: RFQ-2026-0042 and OP67890")
        assert result["rfq_number"] == "2026-0042"
        assert result["opportunity_number"] == "OP67890"

    def test_no_matches(self):
        result = match_by_subject("Hello, how are you?")
        assert result == {}

    def test_empty_subject(self):
        result = match_by_subject("")
        assert result == {}

    def test_none_subject(self):
        result = match_by_subject(None)
        assert result == {}

    def test_rfq_with_en_dash(self):
        # The regex captures digits and regular hyphens, en-dashes are separators only.
        # RFQ\u20132026\u20130042 captures '2026' (digits before next en-dash).
        result = match_by_subject("Quote: RFQ\u20132026\u20130042")
        assert result["rfq_number"] == "2026"

    def test_case_insensitive(self):
        result = match_by_subject("rfq-2026-0001")
        assert result["rfq_number"] == "2026-0001"


class TestGenericDomains:
    """Verify common email domains are excluded from matching."""

    def test_gmail_excluded(self):
        assert "gmail.com" in _GENERIC_DOMAINS

    def test_outlook_excluded(self):
        assert "outlook.com" in _GENERIC_DOMAINS

    def test_yahoo_excluded(self):
        assert "yahoo.com" in _GENERIC_DOMAINS

    def test_icloud_excluded(self):
        assert "icloud.com" in _GENERIC_DOMAINS


class TestInactiveParentFiltering:
    """Active contacts of INACTIVE customers/suppliers must never match.

    Regression: RFQ-2026-1854 was auto-linked to a deactivated customer
    because an active contact still pointed at it.
    """

    @staticmethod
    def _session_for_matching(contacts=None, customers=None):
        """Session mock: empty query results except for given contact/customer lists."""
        from includes.dashboard.models import Contact, Customer

        s = MagicMock()

        def query_side_effect(*cols):
            m = MagicMock()
            if cols and cols[0] is Contact:
                m.filter.return_value.all.return_value = contacts or []
            elif cols and cols[0] is Customer:
                m.filter.return_value.all.return_value = customers or []
            else:
                m.filter.return_value.all.return_value = []
            return m

        s.query.side_effect = query_side_effect
        s.get.side_effect = lambda model, eid: None
        return s

    @staticmethod
    def _contact(email, customer_id=None, supplier_id=None):
        c = MagicMock()
        c.email = email
        c.customer_id = customer_id
        c.supplier_id = supplier_id
        return c

    def test_exact_contact_match_skips_inactive_customer(self):
        from includes.dashboard.models import Customer

        contact = self._contact("nicole.suang@newmont.com", customer_id="inactive-cid")
        inactive = MagicMock()
        inactive.isinactive = True
        inactive.companyname = "Boddington Gold-Copper Mine & Processing Plant"

        session = self._session_for_matching(contacts=[contact])
        session.get.side_effect = lambda model, eid: inactive if model is Customer else None

        result = find_all_matches(session, "nicole.suang@newmont.com", {})
        assert result["match_type"] is None
        assert result["candidates"] == []

    def test_exact_contact_match_keeps_active_customer(self):
        from includes.dashboard.models import Customer

        contact = self._contact("nicole.suang@newmont.com", customer_id="active-cid")
        active = MagicMock()
        active.isinactive = False
        active.companyname = "Newmont Australia"

        session = self._session_for_matching(contacts=[contact])
        session.get.side_effect = lambda model, eid: active if model is Customer else None

        result = find_all_matches(session, "nicole.suang@newmont.com", {})
        assert result["match_type"] == "exact"
        assert result["is_unique"] is True
        assert result["unique_entity"]["id"] == "active-cid"

    def test_exact_contact_match_skips_inactive_supplier(self):
        from includes.dashboard.models import Supplier

        contact = self._contact("x@newmont.com", supplier_id="sup1")
        inactive = MagicMock()
        inactive.isinactive = True

        session = self._session_for_matching(contacts=[contact])
        session.get.side_effect = lambda model, eid: inactive if model is Supplier else None

        with patch("includes.dashboard.supplier_dedup.resolve_supplier_id", return_value="sup1"):
            result = find_all_matches(session, "x@newmont.com", {})
        assert result["match_type"] is None
        assert result["candidates"] == []

    def test_domain_fallback_skips_inactive_customer_from_stale_index(self):
        from includes.dashboard.models import Customer

        stale_index = {"newmont.com": [
            {"type": "customer", "id": "inactive-cid", "name": "Old Name"},
            {"type": "customer", "id": "active-cid", "name": "Newmont Australia"},
        ]}
        inactive = MagicMock()
        inactive.isinactive = True
        active = MagicMock()
        active.isinactive = False
        active.companyname = "Newmont Australia"

        session = self._session_for_matching()
        session.get.side_effect = (
            lambda model, eid: active if model is Customer and eid == "active-cid"
            else (inactive if model is Customer else None)
        )

        result = find_all_matches(session, "x@newmont.com", stale_index)
        assert result["match_type"] == "domain"
        assert result["is_unique"] is True
        assert result["unique_entity"]["id"] == "active-cid"

    def test_build_domain_index_skips_contacts_of_inactive_parents(self):
        from includes.dashboard.models import Contact, Customer, Supplier

        contacts = [
            self._contact("a@newmont.com", customer_id="inactive-cid"),
            self._contact("b@newmont.com", customer_id="active-cid"),
            self._contact("c@newmont.com", supplier_id="inactive-sid"),
            self._contact("d@newmont.com", supplier_id="active-sid"),
        ]
        session = MagicMock()

        def query_side_effect(*cols):
            m = MagicMock()
            if cols and cols[0] is Contact.email:
                m.filter.return_value.all.return_value = contacts
            elif cols and cols[0] is Customer.id and len(cols) == 1:
                m.filter.return_value.all.return_value = [("active-cid",)]
            elif cols and cols[0] is Supplier.id and len(cols) == 1:
                m.filter.return_value.all.return_value = [("active-sid",)]
            else:
                m.filter.return_value.all.return_value = []
            return m

        session.query.side_effect = query_side_effect

        index = build_domain_index(session)
        entries = index["newmont.com"]
        by_id = {(e["type"], e["id"]) for e in entries}

        assert ("customer", "active-cid") in by_id
        assert ("supplier", "active-sid") in by_id
        assert ("customer", "inactive-cid") not in by_id
        assert ("supplier", "inactive-sid") not in by_id
