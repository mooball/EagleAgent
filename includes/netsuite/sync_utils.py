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
