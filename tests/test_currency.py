"""Tests for includes.currency module."""

import time
from unittest.mock import patch, MagicMock

import pytest

from includes.currency import (
    convert,
    convert_to_aud,
    get_rate,
    _fetch_ecb_rates,
    _cache,
    _cache_lock,
    _CACHE_TTL,
)
import includes.currency as currency_mod


# Sample ECB CSV response
SAMPLE_CSV = (
    "FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n"
    "D,USD,EUR,SP00,A,2026-05-09,1.08\n"
    "D,AUD,EUR,SP00,A,2026-05-09,1.63\n"
    "D,GBP,EUR,SP00,A,2026-05-09,0.85\n"
    "D,JPY,EUR,SP00,A,2026-05-09,160.50\n"
    "D,NZD,EUR,SP00,A,2026-05-09,1.80\n"
)


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the module-level cache before each test."""
    currency_mod._cache = {}
    yield
    currency_mod._cache = {}


def _mock_response(text, status_code=200):
    resp = MagicMock()
    resp.text = text
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return resp


class TestFetchRates:
    @patch("includes.currency.httpx.get")
    def test_parses_ecb_csv(self, mock_get):
        mock_get.return_value = _mock_response(SAMPLE_CSV)
        rates = _fetch_ecb_rates()

        assert rates["EUR"] == 1.0
        assert rates["USD"] == 1.08
        assert rates["AUD"] == 1.63
        assert rates["GBP"] == 0.85
        assert rates["JPY"] == 160.50
        assert rates["NZD"] == 1.80

    @patch("includes.currency.httpx.get")
    def test_raises_on_empty_csv(self, mock_get):
        mock_get.return_value = _mock_response("FREQ,CURRENCY\n")
        with pytest.raises(ValueError, match="ECB returned"):
            _fetch_ecb_rates()


class TestGetRate:
    @patch("includes.currency.httpx.get")
    def test_same_currency_returns_one(self, mock_get):
        assert get_rate("AUD", "AUD") == 1.0
        assert get_rate("USD", "USD") == 1.0
        mock_get.assert_not_called()  # No fetch needed

    @patch("includes.currency.httpx.get")
    def test_cross_rate(self, mock_get):
        mock_get.return_value = _mock_response(SAMPLE_CSV)

        rate = get_rate("USD", "AUD")
        # AUD/EUR = 1.63, USD/EUR = 1.08
        # USD→AUD = 1.63 / 1.08 ≈ 1.5093
        expected = 1.63 / 1.08
        assert abs(rate - expected) < 0.0001

    @patch("includes.currency.httpx.get")
    def test_unknown_currency_raises(self, mock_get):
        mock_get.return_value = _mock_response(SAMPLE_CSV)
        with pytest.raises(ValueError, match="Unknown currency: XYZ"):
            get_rate("XYZ", "AUD")

    @patch("includes.currency.httpx.get")
    def test_case_insensitive(self, mock_get):
        mock_get.return_value = _mock_response(SAMPLE_CSV)
        rate1 = get_rate("usd", "aud")
        rate2 = get_rate("USD", "AUD")
        assert rate1 == rate2


class TestCaching:
    @patch("includes.currency.httpx.get")
    def test_caches_rates(self, mock_get):
        mock_get.return_value = _mock_response(SAMPLE_CSV)

        get_rate("USD", "AUD")
        get_rate("GBP", "AUD")

        # Should only fetch once
        assert mock_get.call_count == 1

    @patch("includes.currency.httpx.get")
    def test_stale_cache_refetches(self, mock_get):
        mock_get.return_value = _mock_response(SAMPLE_CSV)

        get_rate("USD", "AUD")
        assert mock_get.call_count == 1

        # Expire the cache
        currency_mod._cache["timestamp"] = time.time() - _CACHE_TTL - 1

        get_rate("USD", "AUD")
        assert mock_get.call_count == 2

    @patch("includes.currency.httpx.get")
    def test_uses_stale_cache_on_fetch_failure(self, mock_get):
        # First call succeeds
        mock_get.return_value = _mock_response(SAMPLE_CSV)
        rate1 = get_rate("USD", "AUD")

        # Expire the cache, then make fetch fail
        currency_mod._cache["timestamp"] = time.time() - _CACHE_TTL - 1
        mock_get.side_effect = Exception("Network error")

        # Should return stale cached value
        rate2 = get_rate("USD", "AUD")
        assert rate1 == rate2


class TestConvert:
    @patch("includes.currency.httpx.get")
    def test_convert(self, mock_get):
        mock_get.return_value = _mock_response(SAMPLE_CSV)

        result = convert(100.0, "USD", "AUD")
        expected = 100.0 * (1.63 / 1.08)
        assert abs(result - expected) < 0.01

    @patch("includes.currency.httpx.get")
    def test_convert_to_aud(self, mock_get):
        mock_get.return_value = _mock_response(SAMPLE_CSV)

        result = convert_to_aud(80.0, "USD")
        expected = 80.0 * (1.63 / 1.08)
        assert abs(result - expected) < 0.01

    @patch("includes.currency.httpx.get")
    def test_convert_same_currency(self, mock_get):
        assert convert(50.0, "AUD", "AUD") == 50.0
        mock_get.assert_not_called()
