"""Tests for RFQ bulk-write safety: UUID validation and deadlock retry.

Regression coverage for the 2026-09-07 production incident:
- legacy non-UUID supplier ids ("sup_1597", "926") crashed RFQ page renders
  with InvalidTextRepresentation;
- concurrent bulk supplier writes deadlocked and 500'd.
"""

import asyncio
import uuid
from unittest.mock import MagicMock, patch

import pytest

from includes.dashboard.routes.rfqs import (
    _is_valid_uuid,
    _commit_bulk_with_retry,
)
from includes.dashboard.routes import _enrich_rfq_supplier_contacts


# ============================================================================
# _is_valid_uuid
# ============================================================================

class TestIsValidUuid:
    def test_accepts_uuid_strings_and_objects(self):
        uid = uuid.uuid4()
        assert _is_valid_uuid(str(uid))
        assert _is_valid_uuid(uid)

    @pytest.mark.parametrize("value", ["sup_1597", "sup_1508", "926", "2150", "", None, 926, True])
    def test_rejects_legacy_and_non_uuid_values(self, value):
        assert _is_valid_uuid(value) is False


# ============================================================================
# _commit_bulk_with_retry
# ============================================================================

class DeadlockDetected(Exception):
    pass


def _deadlock_error():
    exc = Exception("deadlock detected")
    exc.__cause__ = DeadlockDetected("psycopg deadlock")
    return exc


class FakeSession:
    def __init__(self, fail_commits=0, commit_error=None):
        self.commits = 0
        self.rollbacks = 0
        self._fail_commits = fail_commits
        self._commit_error = commit_error

    def commit(self):
        self.commits += 1
        if self._fail_commits > 0:
            self._fail_commits -= 1
            raise _deadlock_error()
        if self._commit_error is not None:
            raise self._commit_error

    def rollback(self):
        self.rollbacks += 1


class TestCommitBulkWithRetry:
    async def test_success_commits_once(self):
        session = FakeSession()
        calls = []

        await _commit_bulk_with_retry(session, lambda: calls.append(1))

        assert calls == [1]
        assert session.commits == 1
        assert session.rollbacks == 0

    async def test_retries_once_on_deadlock(self):
        session = FakeSession(fail_commits=1)
        calls = []

        await _commit_bulk_with_retry(session, lambda: calls.append(len(calls) + 1))

        assert calls == [1, 2]          # apply_fn re-ran from scratch
        assert session.commits == 2     # second commit succeeded
        assert session.rollbacks == 1

    async def test_exhausts_retries_then_raises(self):
        session = FakeSession(fail_commits=3)
        calls = []

        with pytest.raises(Exception):
            await _commit_bulk_with_retry(session, lambda: calls.append(1))

        assert len(calls) == 3
        assert session.rollbacks == 3

    async def test_non_deadlock_error_not_retried(self):
        session = FakeSession(commit_error=ValueError("boom"))
        calls = []

        with pytest.raises(ValueError):
            await _commit_bulk_with_retry(session, lambda: calls.append(1))

        assert calls == [1]
        assert session.rollbacks == 1

    async def test_short_circuit_propagates(self):
        from includes.dashboard.routes.rfqs import _BulkShortCircuit

        session = FakeSession()

        def _apply():
            raise _BulkShortCircuit("response")

        with pytest.raises(_BulkShortCircuit):
            await _commit_bulk_with_retry(session, _apply)
        assert session.commits == 0


# ============================================================================
# _enrich_rfq_supplier_contacts with legacy ids
# ============================================================================

class TestEnrichBogusSupplierIds:
    def test_bogus_id_falls_back_to_name_match_and_repairs(self):
        """'sup_1597' must never reach Supplier.id.in_(); name matching
        enriches the entry and overwrites the bogus id in memory."""
        matched_id = uuid.uuid4()
        matched = MagicMock()
        matched.id = matched_id
        matched.supply_chain_position = None
        matched.terms = None
        matched.country = None
        matched.currency = None
        matched.source = None

        fake_session = MagicMock()
        fake_session.query.return_value.filter.return_value.all.return_value = []
        fake_session.query.return_value.filter.return_value.distinct.return_value.all.return_value = []

        rfq = {
            "items": [
                {
                    "suppliers": [
                        {
                            "name": "Porter Equipment Australia",
                            "supplier_id": "sup_1597",
                            "db_match": "exact",
                        }
                    ]
                }
            ]
        }

        with patch(
            "includes.dashboard.routes._helpers.get_session",
            return_value=fake_session,
        ), patch(
            "includes.dashboard.database.match_suppliers_by_names",
            return_value={"porter equipment australia": matched},
        ), patch(
            "includes.dashboard.database.merge_supplier_contacts",
        ):
            _enrich_rfq_supplier_contacts(rfq)

        sup = rfq["items"][0]["suppliers"][0]
        assert sup["supplier_id"] == str(matched_id)
        assert sup["db_match"] == "exact"
        # The legacy id must not have been passed to a UUID IN(...) query.
        for call in fake_session.query.call_args_list:
            assert "sup_1597" not in str(call)
