"""Display-timezone helpers for UTC-stored timestamps.

Every ``DateTime(timezone=True)`` column round-trips through PostgreSQL as UTC:
``timestamptz`` is normalised on write, so the offset a value was written with is
discarded and it reads back as UTC regardless. Reading ``.hour``, ``.strftime()``
or ``.date()`` straight off a stored value therefore silently renders UTC — ten
hours behind AEST, and often on the wrong calendar day.

User-facing rendering must go through :func:`to_local` first. Duration maths
(``now - created``) needs no conversion: both sides are aware, so the difference
is the same instant whether it is expressed in UTC or AEST.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from config import config


def display_tz() -> ZoneInfo:
    """Return the configured display timezone (``config.TIMEZONE``)."""
    return ZoneInfo(config.TIMEZONE)


def to_local(dt: datetime) -> datetime:
    """Convert a stored UTC datetime to the configured display timezone.

    Naive datetimes are treated as UTC, which is how PostgreSQL hands back
    ``timestamptz`` values when no session timezone is set. Aware datetimes are
    converted by instant, so passing an already-localised value is harmless.

    Raises:
        TypeError: if ``dt`` is not a datetime (e.g. a bare ``date``, which has
            no time component and therefore nothing to convert).
    """
    if not isinstance(dt, datetime):
        raise TypeError(
            f"to_local() expects a datetime, got {type(dt).__name__}"
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(display_tz())
