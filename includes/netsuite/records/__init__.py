"""NetSuite record creation — write-back operations to NetSuite REST API.

Each record type has its own module with a focused creation function.
All creation functions return a CreateResult for uniform error handling
by callers (UI, agents, pipelines).
"""

from .base import CreateResult, NetSuiteCreateError
from .opportunity import create_opportunity, create_and_link_opportunity

__all__ = [
    "CreateResult",
    "NetSuiteCreateError",
    "create_opportunity",
    "create_and_link_opportunity",
]
