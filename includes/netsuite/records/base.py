"""Shared types and helpers for NetSuite record creation."""

from dataclasses import dataclass


@dataclass
class CreateResult:
    """Outcome of a NetSuite record creation attempt.

    Every creation function returns this so callers (UI, agent, pipeline)
    can handle success and failure uniformly.
    """
    success: bool
    netsuite_id: str | None = None
    tran_id: str | None = None        # e.g. "OP72309" — fetched after creation
    error: str | None = None
    error_code: int | None = None      # HTTP status code on failure
    record_type: str = ""


class NetSuiteCreateError(Exception):
    """Raised when NetSuite rejects a record creation request."""

    def __init__(self, message: str, status_code: int, response_body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
