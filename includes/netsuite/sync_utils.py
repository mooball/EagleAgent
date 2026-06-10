"""
Shared utility functions for NetSuite sync scripts.

Provides common helpers used by all sync_netsuite_*.py scripts:
- parse_netsuite_date: convert NetSuite d/m/yyyy dates to Python datetime
- parse_since: parse --since CLI argument (ISO date or relative period)
- get_engine: build a synchronous SQLAlchemy engine from Config.DATABASE_URL
"""

import re
from datetime import datetime, timedelta

from sqlalchemy import create_engine

from config.settings import Config


def parse_netsuite_date(date_str: str | None) -> datetime | None:
    """Parse NetSuite date format (d/m/yyyy) to a datetime."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%d/%m/%Y")
    except ValueError:
        return None


def parse_since(value: str) -> str:
    """Parse a --since value into an ISO date string (YYYY-MM-DD).

    Accepts either:
      - An ISO date: '2026-04-01'
      - A relative period: '7 days', '7days', '7d'
    """
    match = re.match(r"^(\d+)\s*d(?:ays?)?$", value.strip(), re.IGNORECASE)
    if match:
        days = int(match.group(1))
        return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return value


def get_engine():
    """Build a synchronous SQLAlchemy engine from Config.DATABASE_URL.

    Handles the asyncpg → psycopg driver swap needed for sync scripts.
    """
    db_url = Config.DATABASE_URL
    if not db_url:
        raise ValueError("DATABASE_URL is empty. Check your `.env` settings.")

    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return create_engine(db_url)


# ---------------------------------------------------------------------------
# Currency normalisation
# ---------------------------------------------------------------------------

# Maps NetSuite display names (from BUILTIN.DF) to ISO 4217 codes.
_CURRENCY_NAME_TO_ISO = {
    "Australian Dollar": "AUD",
    "US Dollar": "USD",
    "Canadian Dollar": "CAD",
    "New Zealand Dollar": "NZD",
    "Euro": "EUR",
    "British Pound": "GBP",
    "Japanese Yen": "JPY",
    "Indian Rupee": "INR",
    "Philippine Peso": "PHP",
    "Singapore Dollar": "SGD",
    "South African Rand": "ZAR",
    "Chinese Yuan": "CNY",
    "Hong Kong Dollar": "HKD",
    "Thai Baht": "THB",
    "Malaysian Ringgit": "MYR",
    "Indonesian Rupiah": "IDR",
    "Korean Won": "KRW",
    "Swiss Franc": "CHF",
    "Swedish Krona": "SEK",
    "Norwegian Krone": "NOK",
    "Danish Krone": "DKK",
}

# Valid ISO codes (3 uppercase letters) — used for passthrough detection
_ISO_CODE_RE = re.compile(r"^[A-Z]{3}$")


def normalize_currency(value: str | None) -> str | None:
    """Normalise a NetSuite currency value to an ISO 4217 code.

    Handles three cases:
      - None/empty → None
      - Already an ISO code (e.g. 'AUD') → returned as-is (uppercased)
      - Full name (e.g. 'Canadian Dollar') → mapped to ISO code

    Returns None if the value cannot be recognised.
    """
    if not value:
        return None
    stripped = value.strip()
    upper = stripped.upper()
    # Already a 3-letter code
    if _ISO_CODE_RE.match(upper):
        return upper
    # Try name lookup (case-insensitive)
    return _CURRENCY_NAME_TO_ISO.get(stripped) or _CURRENCY_NAME_TO_ISO.get(stripped.title())
