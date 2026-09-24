"""Tests for the add-supplier widget's handler internals.

The RFQ line linking gets its own file because it is the part with real
decision-making: which lines, what happens when the RFQ moved, and how the two
halves of the submission (create, then link) report themselves separately.
"""

import pytest

from includes.dashboard import supplier_widget as sw

RFQ = "RFQ-2026-100"
LINES = [
    {"line": 1, "part_number": "ABC-1", "description": "Bolt"},
    {"line": 2, "part_number": "ABC-2", "description": "Nut"},
    {"line": 3, "part_number": "ABC-3", "description": "Washer"},
]


class FakeSupplier:
    id = "supplier-uuid-1"
    name = "Acme Fasteners Pty Ltd"


@pytest.fixture
def lines_and_writes(monkeypatch):
    """Stub the three outside dependencies: line reads, the bulk write, and the
    read-back that verifies it."""
    captured: dict = {"calls": [], "verified": True}

    monkeypatch.setattr(sw, "rfq_lines", lambda rfq_id: list(LINES))

    def _fake_bulk(rfq_number, data, user_id):
        captured["calls"].append({
            "rfq_number": rfq_number, "data": data, "user_id": user_id,
        })
        return {"id": rfq_number}          # the RFQ dict a success returns

    def _fake_rfq(rfq_number):
        """The RFQ as read back. ``verified=False`` simulates a write that the
        helper returned success for but that did not land."""
        carriers = [{"supplier_id": FakeSupplier.id, "name": FakeSupplier.name}]
        return {
            "items": [
                {"line": line["line"],
                 "suppliers": carriers if captured["verified"] else []}
                for line in LINES
            ]
        }

    import includes.tools.quote_tools as quote_tools
    import includes.tools.rfq_crud as rfq_crud

    monkeypatch.setattr(rfq_crud, "_add_suppliers_bulk_sync", _fake_bulk)
    monkeypatch.setattr(quote_tools, "_get_rfq_dict_sync", _fake_rfq)
    return captured


def _state(rfq_id=RFQ):
    return {"id": "w1", "name": "add_supplier", "status": "pending", "rfq_id": rfq_id}


class TestLinkToLines:
    def test_none_mode_touches_nothing(self, lines_and_writes):
        result = sw._link_to_lines(_state(), FakeSupplier(), {"line_mode": "none"}, "a@b.com")
        assert result == {"lines": [], "error": ""}
        assert lines_and_writes["calls"] == []

    def test_a_plain_thread_links_nothing(self, lines_and_writes):
        """No RFQ bound, no line section — and no attempt to guess one."""
        result = sw._link_to_lines(
            _state(rfq_id=None), FakeSupplier(), {"line_mode": "all"}, "a@b.com"
        )
        assert result == {"lines": [], "error": ""}
        assert lines_and_writes["calls"] == []

    def test_all_mode_covers_every_line(self, lines_and_writes):
        result = sw._link_to_lines(_state(), FakeSupplier(), {"line_mode": "all"}, "tom@x.com")

        assert result == {"lines": [1, 2, 3], "error": ""}
        call = lines_and_writes["calls"][0]
        assert call["rfq_number"] == RFQ
        assert call["user_id"] == "tom@x.com"
        assert [entry["line"] for entry in call["data"]["entries"]] == [1, 2, 3]

    def test_entries_are_flat_supplier_dicts(self, lines_and_writes):
        """The bulk helper takes one *supplier* dict per line, with a "line" key
        mixed in — not a nested {"line": n, "suppliers": [...]}.

        Regression (RFQ-2026-1231, 2026-09-24): nesting made every entry arrive
        with no name, so `_add_suppliers_to_line_core` rejected it as "Unknown",
        added nothing, and still returned the RFQ dict — which the card read as
        success.
        """
        sw._link_to_lines(_state(), FakeSupplier(), {"line_mode": "all"}, "tom@x.com")

        entries = lines_and_writes["calls"][0]["data"]["entries"]
        for entry in entries:
            assert "suppliers" not in entry, (
                "nesting the supplier under 'suppliers' makes it nameless to the "
                "helper, which rejects it as Unknown"
            )
            assert entry["name"] == FakeSupplier.name
            assert entry["supplier_id"] == FakeSupplier.id

    def test_entries_carry_the_supplier_id(self, lines_and_writes):
        """This is what routes the entry through the ``db_linked`` branch: no
        name matching, no web search, and no contact URL required."""
        sw._link_to_lines(_state(), FakeSupplier(), {"line_mode": "all"}, "tom@x.com")

        for entry in lines_and_writes["calls"][0]["data"]["entries"]:
            assert entry["supplier_id"] == FakeSupplier.id

    def test_a_write_that_added_nothing_is_reported_as_a_failure(self, lines_and_writes):
        """The helper returns the RFQ dict even when it accepted nothing, so the
        result must be read back before it is reported."""
        lines_and_writes["verified"] = False

        result = sw._link_to_lines(_state(), FakeSupplier(), {"line_mode": "specific", "lines": ["1"]}, "tom@x.com")

        assert result["lines"] == []
        assert "did not accept the supplier" in result["error"]

    def test_the_lines_reported_are_the_lines_that_actually_landed(self, monkeypatch):
        """Report from the read-back, not from what was requested."""
        monkeypatch.setattr(sw, "rfq_lines", lambda rfq_id: list(LINES))
        import includes.tools.quote_tools as quote_tools
        import includes.tools.rfq_crud as rfq_crud

        monkeypatch.setattr(
            rfq_crud, "_add_suppliers_bulk_sync", lambda *a, **k: {"id": RFQ}
        )
        # Only lines 1 and 3 accepted it.
        monkeypatch.setattr(quote_tools, "_get_rfq_dict_sync", lambda rfq_id: {
            "items": [
                {"line": 1, "suppliers": [{"supplier_id": FakeSupplier.id}]},
                {"line": 2, "suppliers": []},
                {"line": 3, "suppliers": [{"supplier_id": FakeSupplier.id}]},
            ]
        })

        result = sw._link_to_lines(_state(), FakeSupplier(), {"line_mode": "all"}, "tom@x.com")

        assert result == {"lines": [1, 3], "error": ""}

    def test_an_unreadable_verification_does_not_invent_a_failure(self, monkeypatch):
        monkeypatch.setattr(sw, "rfq_lines", lambda rfq_id: list(LINES))
        import includes.tools.quote_tools as quote_tools
        import includes.tools.rfq_crud as rfq_crud

        monkeypatch.setattr(
            rfq_crud, "_add_suppliers_bulk_sync", lambda *a, **k: {"id": RFQ}
        )

        def _boom(rfq_id):
            raise RuntimeError("connection lost")

        monkeypatch.setattr(quote_tools, "_get_rfq_dict_sync", _boom)

        result = sw._link_to_lines(_state(), FakeSupplier(), {"line_mode": "specific", "lines": ["2"]}, "tom@x.com")

        # The write itself did not error, so the request is reported as done.
        assert result == {"lines": [2], "error": ""}

    def test_specific_mode_uses_only_the_ticked_lines(self, lines_and_writes):
        result = sw._link_to_lines(
            _state(), FakeSupplier(), {"line_mode": "specific", "lines": ["1", "3"]}, "tom@x.com"
        )

        assert result == {"lines": [1, 3], "error": ""}
        assert [e["line"] for e in lines_and_writes["calls"][0]["data"]["entries"]] == [1, 3]

    def test_a_single_ticked_line_arrives_as_a_string(self, lines_and_writes):
        """A one-item checkbox group is submitted as a string, not a list."""
        result = sw._link_to_lines(
            _state(), FakeSupplier(), {"line_mode": "specific", "lines": "2"}, "tom@x.com"
        )
        assert result == {"lines": [2], "error": ""}

    def test_a_line_that_no_longer_exists_is_dropped(self, lines_and_writes):
        result = sw._link_to_lines(
            _state(), FakeSupplier(), {"line_mode": "specific", "lines": ["1", "99"]}, "tom@x.com"
        )
        assert result == {"lines": [1], "error": ""}

    def test_nothing_valid_selected_is_reported_and_writes_nothing(self, lines_and_writes):
        result = sw._link_to_lines(
            _state(), FakeSupplier(), {"line_mode": "specific", "lines": ["99"]}, "tom@x.com"
        )
        assert result["lines"] == []
        assert "No matching lines" in result["error"]
        assert lines_and_writes["calls"] == []

    def test_an_rfq_with_no_lines_is_reported(self, monkeypatch):
        monkeypatch.setattr(sw, "rfq_lines", lambda rfq_id: [])
        result = sw._link_to_lines(_state(), FakeSupplier(), {"line_mode": "all"}, "tom@x.com")
        assert result["lines"] == []
        assert "no line items" in result["error"]

    def test_a_rejected_write_surfaces_the_reason(self, monkeypatch):
        """A failed link must be reported as such — never as a silent success."""
        monkeypatch.setattr(sw, "rfq_lines", lambda rfq_id: list(LINES))
        import includes.tools.rfq_crud as rfq_crud

        monkeypatch.setattr(
            rfq_crud, "_add_suppliers_bulk_sync",
            lambda rfq_number, data, user_id: "Error: RFQ 'RFQ-2026-100' not found.",
        )
        result = sw._link_to_lines(_state(), FakeSupplier(), {"line_mode": "all"}, "tom@x.com")

        assert result["lines"] == []
        assert "not found" in result["error"]

    def test_an_exception_is_reported_rather_than_raised(self, monkeypatch):
        monkeypatch.setattr(sw, "rfq_lines", lambda rfq_id: list(LINES))
        import includes.tools.rfq_crud as rfq_crud

        def _boom(*_a, **_k):
            raise RuntimeError("deadlock")

        monkeypatch.setattr(rfq_crud, "_add_suppliers_bulk_sync", _boom)
        result = sw._link_to_lines(_state(), FakeSupplier(), {"line_mode": "all"}, "tom@x.com")

        assert result["lines"] == []
        assert "deadlock" in result["error"]


class TestNotice:
    def test_creation_only(self):
        assert sw._notice({"name": "Acme", "lines": [], "line_error": ""}) == (
            "✅ Created supplier **Acme**."
        )

    def test_creation_with_one_line(self):
        notice = sw._notice({"name": "Acme", "lines": [2], "line_error": ""})
        assert "line 2" in notice and "lines 2" not in notice

    def test_creation_with_several_lines(self):
        notice = sw._notice({"name": "Acme", "lines": [1, 3], "line_error": ""})
        assert "lines 1, 3" in notice

    def test_a_failed_link_says_so(self):
        """The supplier exists either way — the transcript must not imply the
        lines were updated too."""
        notice = sw._notice({"name": "Acme", "lines": [], "line_error": "No matching lines."})
        assert "Created supplier" in notice
        assert "Could not add it to the RFQ: No matching lines." in notice


class TestSubmitAsksTheShellToRefresh:
    """A widget submit runs outside an agent turn, so there is no run to emit a
    dashboard refresh. The outcome carries the command instead, and the client
    raises the same DOM event the run path raises.

    Without it the RFQ page behind the panel keeps showing the old line
    suppliers until the user reloads by hand (reported 2026-09-24).
    """

    @pytest.fixture
    def wired_submit(self, monkeypatch):
        from includes.dashboard.supplier_create import DuplicateReport

        monkeypatch.setattr(sw, "validate", lambda data: (
            {"name": "Acme Pty Ltd", "email": "a@acme.com", "currency": "AUD"}, {}
        ))
        monkeypatch.setattr(sw, "find_duplicates", lambda *a, **k: DuplicateReport())
        monkeypatch.setattr(sw, "create_local_supplier", lambda *a, **k: FakeSupplier())
        return monkeypatch

    def test_asks_for_a_refresh_when_lines_changed(self, wired_submit):
        wired_submit.setattr(sw, "_link_to_lines", lambda *a, **k: {"lines": [1, 3], "error": ""})

        outcome = sw._submit(
            {"name": "Acme Pty Ltd", "email": "a@acme.com"},
            _state(), "tom@x.com",
        )

        assert outcome.status == "submitted"
        assert outcome.dashboard == {
            "command": "dashboard_refresh",
            "payload": {"rfq_id": RFQ},
        }

    def test_no_refresh_when_nothing_on_the_rfq_changed(self, wired_submit):
        wired_submit.setattr(sw, "_link_to_lines", lambda *a, **k: {"lines": [], "error": ""})

        outcome = sw._submit(
            {"name": "Acme Pty Ltd", "email": "a@acme.com"},
            _state(), "tom@x.com",
        )

        assert outcome.status == "submitted"
        assert outcome.dashboard is None

    def test_a_plain_thread_asks_for_nothing(self, wired_submit):
        wired_submit.setattr(sw, "_link_to_lines", lambda *a, **k: {"lines": [], "error": ""})

        outcome = sw._submit(
            {"name": "Acme Pty Ltd", "email": "a@acme.com"},
            _state(rfq_id=None), "tom@x.com",
        )

        assert outcome.dashboard is None

    def test_a_failed_link_still_reports_the_creation(self, wired_submit):
        wired_submit.setattr(sw, "_link_to_lines", lambda *a, **k: {
            "lines": [], "error": f"{RFQ} did not accept the supplier on line 1.",
        })

        outcome = sw._submit(
            {"name": "Acme Pty Ltd", "email": "a@acme.com"},
            _state(), "tom@x.com",
        )

        assert outcome.status == "submitted"
        assert outcome.result["line_error"]
        assert outcome.dashboard is None


class TestContext:
    def test_offers_line_targets_only_when_bound(self, monkeypatch):
        monkeypatch.setattr(sw, "rfq_lines", lambda rfq_id: list(LINES))

        unbound = sw._context({"id": "w1", "rfq_id": None})
        assert unbound["rfq"] is None

        bound = sw._context({"id": "w1", "rfq_id": RFQ})
        assert bound["rfq"]["id"] == RFQ
        assert len(bound["rfq"]["lines"]) == 3

    def test_selected_lines_come_from_the_previous_submission(self):
        state = {"id": "w1", "rfq_id": RFQ, "data": {"lines": ["1", "3"]}}
        assert sw._context(state)["selected_lines"] == [1, 3]

    def test_selected_lines_is_empty_without_a_submission(self):
        assert sw._context({"id": "w1", "rfq_id": None})["selected_lines"] == []
