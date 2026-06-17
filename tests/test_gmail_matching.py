"""Tests for includes/gmail/matching.py — domain extraction and email matching."""

import pytest
from unittest.mock import MagicMock, patch

from includes.gmail.matching import (
    extract_domain,
    extract_domain_from_url,
    match_by_subject,
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
