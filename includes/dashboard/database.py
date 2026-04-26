"""
Shared database session factories for FastAPI dashboard routes.

Provides both async (for FastAPI route handlers) and sync (for legacy
tool compatibility) session factories using the same DATABASE_URL.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import config


def _sync_url() -> str:
    """Convert the async DATABASE_URL to a sync psycopg URL."""
    url = config.DATABASE_URL
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    return url


_engine = None
_SessionLocal = None


def get_session():
    """Return a new sync SQLAlchemy session (caller must close)."""
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(_sync_url(), pool_pre_ping=True)
        _SessionLocal = sessionmaker(bind=_engine)
    return _SessionLocal()
