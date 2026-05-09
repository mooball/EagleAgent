"""
Currency conversion with 24-hour caching.

Uses the European Central Bank (ECB) daily reference rates as the primary
source. Rates are published against EUR, so we derive cross-rates (e.g.
USD→AUD) via EUR as the common base.

Usage:
    from includes.currency import convert, get_rate

    aud_amount = convert(80.0, "USD", "AUD")   # -> ~124.0
    rate = get_rate("USD", "AUD")               # -> ~1.55
"""

import logging
import time
from threading import Lock

import httpx

logger = logging.getLogger(__name__)

# ECB publishes daily reference rates as JSON — no API key needed
_ECB_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D..EUR.SP00.A?lastNObservations=1&format=csvdata"

# Cache: {"rates": {"USD": 1.08, "AUD": 1.67, ...}, "timestamp": epoch}
_cache: dict = {}
_cache_lock = Lock()
_CACHE_TTL = 86400  # 24 hours


def _fetch_ecb_rates() -> dict[str, float]:
    """Fetch latest ECB daily reference rates (all currencies vs EUR).

    Returns dict like {"USD": 1.08, "AUD": 1.67, "GBP": 0.85, ...}
    where each value is how many units of that currency per 1 EUR.
    """
    resp = httpx.get(_ECB_URL, timeout=15, follow_redirects=True)
    resp.raise_for_status()

    rates: dict[str, float] = {"EUR": 1.0}

    # Parse CSV — columns include CURRENCY and OBS_VALUE
    lines = resp.text.strip().split("\n")
    if len(lines) < 2:
        raise ValueError("ECB returned empty CSV")

    header = lines[0].split(",")
    try:
        currency_idx = header.index("CURRENCY")
        value_idx = header.index("OBS_VALUE")
    except ValueError:
        raise ValueError(f"Unexpected ECB CSV header: {header}")

    for line in lines[1:]:
        cols = line.split(",")
        if len(cols) > max(currency_idx, value_idx):
            currency = cols[currency_idx].strip()
            try:
                rate = float(cols[value_idx].strip())
                rates[currency] = rate
            except (ValueError, IndexError):
                continue

    if len(rates) < 5:
        raise ValueError(f"ECB returned too few rates ({len(rates)})")

    logger.info(f"Fetched {len(rates)} ECB exchange rates")
    return rates


def _get_rates() -> dict[str, float]:
    """Get cached rates, refreshing if stale (>24h) or missing."""
    global _cache

    with _cache_lock:
        if _cache and (time.time() - _cache["timestamp"]) < _CACHE_TTL:
            return _cache["rates"]

    # Fetch outside the lock to avoid blocking
    try:
        rates = _fetch_ecb_rates()
    except Exception as e:
        logger.error(f"Failed to fetch ECB rates: {e}")
        with _cache_lock:
            if _cache:
                logger.warning("Using stale cached rates")
                return _cache["rates"]
        raise RuntimeError(f"No exchange rates available: {e}")

    with _cache_lock:
        _cache = {"rates": rates, "timestamp": time.time()}

    return rates


def get_rate(from_currency: str, to_currency: str) -> float:
    """Get the exchange rate to convert from_currency to to_currency.

    Returns a multiplier: amount_in_from * rate = amount_in_to.
    """
    fr = from_currency.upper().strip()
    to = to_currency.upper().strip()

    if fr == to:
        return 1.0

    rates = _get_rates()

    if fr not in rates:
        raise ValueError(f"Unknown currency: {fr}")
    if to not in rates:
        raise ValueError(f"Unknown currency: {to}")

    # Cross-rate via EUR: from_currency -> EUR -> to_currency
    # rates[X] = how many X per 1 EUR
    # So: 1 from_currency = (1 / rates[fr]) EUR = (rates[to] / rates[fr]) to_currency
    return rates[to] / rates[fr]


def convert(amount: float, from_currency: str, to_currency: str) -> float:
    """Convert an amount from one currency to another.

    Returns the converted amount as a float.
    """
    return amount * get_rate(from_currency, to_currency)


def convert_to_aud(amount: float, from_currency: str) -> float:
    """Convenience: convert any amount to AUD."""
    return convert(amount, from_currency, "AUD")
