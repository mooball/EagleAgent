"""Tests for RFQ write serialization in includes/tools/rfq_crud.py.

Regression: concurrent tool calls in one agent turn (LangGraph gathers them)
ran RFQ CRUD helpers in parallel threads, deadlocking on Postgres row locks
and silently losing read-modify-write history updates. Writes are now
serialized per RFQ.
"""

import threading
import time

from includes.tools.rfq_crud import _rfq_write_lock, _serialized_rfq_write


class TestRfqWriteLock:
    def test_same_rfq_shares_lock(self):
        assert _rfq_write_lock("RFQ-1") is _rfq_write_lock("RFQ-1")

    def test_different_rfqs_have_different_locks(self):
        assert _rfq_write_lock("RFQ-A") is not _rfq_write_lock("RFQ-B")


class TestSerializedRfqWrite:
    def test_serializes_writes_for_same_rfq_but_not_different(self):
        active = {"RFQ-1": 0, "RFQ-2": 0}
        max_active = {"RFQ-1": 0, "RFQ-2": 0}
        guard = threading.Lock()

        @_serialized_rfq_write
        def fake_write(rfq_number, tag):
            with guard:
                active[rfq_number] += 1
                max_active[rfq_number] = max(max_active[rfq_number], active[rfq_number])
            time.sleep(0.03)
            with guard:
                active[rfq_number] -= 1
            return tag

        results = []
        errors = []

        def run(rfq_number, tag):
            try:
                results.append(fake_write(rfq_number, tag))
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=run, args=("RFQ-1", i)) for i in range(6)]
        threads += [threading.Thread(target=run, args=("RFQ-2", i)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # Same RFQ: never two concurrent writers
        assert max_active["RFQ-1"] == 1
        assert max_active["RFQ-2"] == 1
        assert sorted(results) == [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5]

    def test_decorator_returns_function_result(self):
        @_serialized_rfq_write
        def fake_write(rfq_number):
            return f"done-{rfq_number}"

        assert fake_write("RFQ-9") == "done-RFQ-9"
