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
        assert result == {"lines": [], "already_on": [], "error": ""}
        assert lines_and_writes["calls"] == []

    def test_a_plain_thread_links_nothing(self, lines_and_writes):
        """No RFQ bound, no line section — and no attempt to guess one."""
        result = sw._link_to_lines(
            _state(rfq_id=None), FakeSupplier(), {"line_mode": "all"}, "a@b.com"
        )
        assert result == {"lines": [], "already_on": [], "error": ""}
        assert lines_and_writes["calls"] == []

    def test_all_mode_covers_every_line(self, lines_and_writes):
        result = sw._link_to_lines(_state(), FakeSupplier(), {"line_mode": "all"}, "tom@x.com")

        assert result == {"lines": [1, 2, 3], "already_on": [], "error": ""}
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

        assert result == {"lines": [1, 3], "already_on": [], "error": ""}

    def test_a_supplier_already_on_a_line_is_not_reported_as_added(self, monkeypatch):
        """An existing supplier is merged into the line's entry rather than
        appended, so the write is a no-op there. Reporting it as "added" would be
        the same false success the flat-entries bug produced."""
        monkeypatch.setattr(sw, "rfq_lines", lambda rfq_id: [
            {"line": 1, "part_number": "ABC-1", "description": "Bolt",
             "supplier_ids": [FakeSupplier.id]},
            {"line": 2, "part_number": "ABC-2", "description": "Nut",
             "supplier_ids": []},
        ])
        import includes.tools.quote_tools as quote_tools
        import includes.tools.rfq_crud as rfq_crud

        monkeypatch.setattr(
            rfq_crud, "_add_suppliers_bulk_sync", lambda *a, **k: {"id": RFQ}
        )
        monkeypatch.setattr(quote_tools, "_get_rfq_dict_sync", lambda rfq_id: {
            "items": [
                {"line": 1, "suppliers": [{"supplier_id": FakeSupplier.id}]},
                {"line": 2, "suppliers": [{"supplier_id": FakeSupplier.id}]},
            ]
        })

        result = sw._link_to_lines(_state(), FakeSupplier(), {"line_mode": "all"}, "tom@x.com")

        assert result == {"lines": [2], "already_on": [1], "error": ""}, (
            "line 1 already carried the supplier, so only line 2 is new — "
            "reporting both reads as though the RFQ gained something it did not"
        )

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
        assert result == {"lines": [2], "already_on": [], "error": ""}

    def test_specific_mode_uses_only_the_ticked_lines(self, lines_and_writes):
        result = sw._link_to_lines(
            _state(), FakeSupplier(), {"line_mode": "specific", "lines": ["1", "3"]}, "tom@x.com"
        )

        assert result == {"lines": [1, 3], "already_on": [], "error": ""}
        assert [e["line"] for e in lines_and_writes["calls"][0]["data"]["entries"]] == [1, 3]

    def test_a_single_ticked_line_arrives_as_a_string(self, lines_and_writes):
        """A one-item checkbox group is submitted as a string, not a list."""
        result = sw._link_to_lines(
            _state(), FakeSupplier(), {"line_mode": "specific", "lines": "2"}, "tom@x.com"
        )
        assert result == {"lines": [2], "already_on": [], "error": ""}

    def test_a_line_that_no_longer_exists_is_dropped(self, lines_and_writes):
        result = sw._link_to_lines(
            _state(), FakeSupplier(), {"line_mode": "specific", "lines": ["1", "99"]}, "tom@x.com"
        )
        assert result == {"lines": [1], "already_on": [], "error": ""}

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
        wired_submit.setattr(sw, "_link_to_lines", lambda *a, **k: {
            "lines": [1, 3], "already_on": [], "error": ""})

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
        wired_submit.setattr(sw, "_link_to_lines", lambda *a, **k: {
            "lines": [], "already_on": [], "error": ""})

        outcome = sw._submit(
            {"name": "Acme Pty Ltd", "email": "a@acme.com"},
            _state(), "tom@x.com",
        )

        assert outcome.status == "submitted"
        assert outcome.dashboard is None

    def test_a_plain_thread_asks_for_nothing(self, wired_submit):
        wired_submit.setattr(sw, "_link_to_lines", lambda *a, **k: {
            "lines": [], "already_on": [], "error": ""})

        outcome = sw._submit(
            {"name": "Acme Pty Ltd", "email": "a@acme.com"},
            _state(rfq_id=None), "tom@x.com",
        )

        assert outcome.dashboard is None

    def test_a_failed_link_still_reports_the_creation(self, wired_submit):
        wired_submit.setattr(sw, "_link_to_lines", lambda *a, **k: {
            "lines": [], "already_on": [],
            "error": f"{RFQ} did not accept the supplier on line 1.",
        })

        outcome = sw._submit(
            {"name": "Acme Pty Ltd", "email": "a@acme.com"},
            _state(), "tom@x.com",
        )

        assert outcome.status == "submitted"
        assert outcome.result["line_error"]
        assert outcome.dashboard is None


def _existing_result(**overrides):
    base = {"name": "Acme Fasteners Pty Ltd", "existing": True,
            "lines": [], "already_on": [], "line_error": ""}
    base.update(overrides)
    return base


class TestNoticeForAnExistingSupplier:
    """The existing-supplier path must not borrow the create wording: nothing was
    created, and "Added on line 1" for one that was already there would put a
    falsehood in the transcript that phase 2's agent reads."""

    def test_added_to_a_line(self):
        assert sw._notice(_existing_result(lines=[4])) == (
            "✅ Added **Acme Fasteners Pty Ltd** as a candidate on line 4."
        )

    def test_already_on_the_line(self):
        assert sw._notice(_existing_result(already_on=[1])) == (
            "✅ **Acme Fasteners Pty Ltd** was already a candidate on line 1."
        )

    def test_selected_with_no_lines(self):
        """A "Don't add" submission, or a thread with no RFQ: the supplier is
        still the outcome, so it must not read like a failure."""
        assert sw._notice(_existing_result()) == (
            "✅ Selected **Acme Fasteners Pty Ltd**."
        )

    def test_new_lines_alongside_ones_it_was_already_on(self):
        notice = sw._notice(_existing_result(lines=[3], already_on=[1, 2]))
        assert "as a candidate on line 3" in notice
        assert "(already on lines 1, 2)" in notice

    def test_never_says_created(self):
        assert "Created" not in sw._notice(_existing_result(lines=[4]))


class TestExistingOrNone:
    """The id is client-supplied, so the row it names is re-checked before any
    write. Attaching a retired (merged) or inactive supplier to an RFQ is exactly
    what the linking rule in ``supplier_lookup`` exists to prevent, and doing it
    through the widget would be the same mistake by a side door."""

    ID = "11111111-1111-1111-1111-111111111111"

    def _session_returning(self, monkeypatch, row):
        import includes.dashboard.database as database

        class FakeSession:
            def query(self, *a):
                return self

            def filter(self, *a):
                return self

            def first(self):
                return row

            def all(self):
                return []

            def close(self):
                pass

        monkeypatch.setattr(database, "get_session", lambda: FakeSession())

    def _row(self, **overrides):
        import uuid

        row = type("Row", (), {
            "id": uuid.UUID(self.ID),
            "name": "Acme Fasteners Pty Ltd",
            "isinactive": False,
            "use_instead": None,
            "contacts": [{"email": "jsonb@acme.example"}],
        })
        for key, value in overrides.items():
            setattr(row, key, value)
        return row

    def test_a_live_supplier_resolves(self, monkeypatch):
        self._session_returning(monkeypatch, self._row())

        found = sw._existing_or_none(self.ID)

        assert found["supplier"].id == self.ID
        assert found["supplier"].name == "Acme Fasteners Pty Ltd"
        assert found["email"] == "jsonb@acme.example", (
            "with no Contact row the JSONB list is the fallback — it is what the "
            "RFQ form itself prefills from"
        )

    def test_an_inactive_supplier_is_refused(self, monkeypatch):
        self._session_returning(monkeypatch, self._row(isinactive=True))

        assert sw._existing_or_none(self.ID) is None

    def test_a_merged_duplicate_is_refused(self, monkeypatch):
        import uuid

        self._session_returning(monkeypatch, self._row(use_instead=uuid.uuid4()))

        assert sw._existing_or_none(self.ID) is None, (
            "a merged row still exists, so a search result the user picked "
            "earlier can name it — it must not be linkable"
        )

    def test_a_missing_row_is_refused(self, monkeypatch):
        self._session_returning(monkeypatch, None)

        assert sw._existing_or_none(self.ID) is None

    def test_a_malformed_id_never_reaches_the_database(self, monkeypatch):
        import includes.dashboard.database as database

        def _boom():
            raise AssertionError("a junk id must be rejected before any query")

        monkeypatch.setattr(database, "get_session", _boom)

        assert sw._existing_or_none("not-a-uuid") is None
        assert sw._existing_or_none("") is None
        assert sw._existing_or_none(None) is None


class TestSubmitExisting:
    """The existing-supplier path: the only write is the line link."""

    @pytest.fixture
    def picked(self, monkeypatch):
        monkeypatch.setattr(sw, "_existing_or_none", lambda sid: {
            "supplier": FakeSupplier(), "email": "sales@acme.example",
        })
        return monkeypatch

    def test_nothing_is_created_and_nothing_is_re_validated(self, picked):
        created = []
        picked.setattr(sw, "create_local_supplier",
                       lambda *a, **k: created.append(1) or FakeSupplier())
        picked.setattr(sw, "validate", lambda data: pytest.fail(
            "the row already passed validation when it was created"))
        picked.setattr(sw, "find_duplicates", lambda *a, **k: pytest.fail(
            "an existing supplier is not a duplicate question"))
        picked.setattr(sw, "_link_to_lines", lambda *a, **k: {
            "lines": [2], "already_on": [], "error": ""})

        outcome = sw._submit(
            {"supplier_id": FakeSupplier.id, "name": FakeSupplier.name},
            _state(), "tom@x.com",
        )

        assert created == [], "the picked supplier already exists"
        assert outcome.status == "submitted"
        assert outcome.result["existing"] is True
        assert outcome.result["supplier_id"] == FakeSupplier.id
        assert outcome.result["name"] == FakeSupplier.name
        assert outcome.result["email"] == "sales@acme.example"
        assert outcome.dashboard == {
            "command": "dashboard_refresh", "payload": {"rfq_id": RFQ},
        }, "the RFQ page behind the panel is stale once a line changed"

    def test_a_stale_pick_is_refused_rather_than_linked(self, picked):
        """The name is compared against the row, so an id whose name no longer
        matches means the user edited since picking — and that id no longer
        describes what they meant."""
        picked.setattr(sw, "_link_to_lines", lambda *a, **k: pytest.fail(
            "a stale pick must not reach the RFQ"))

        outcome = sw._submit(
            {"supplier_id": FakeSupplier.id, "name": "Acme Bolt Supplies"},
            _state(), "tom@x.com",
        )

        assert outcome.status == "pending"
        assert outcome.data["mode"] == "search", "send them back to searching"
        assert outcome.data["supplier_id"] == "", (
            "clearing the id is what stops the next submit being stale too"
        )
        assert "went stale" in outcome.error
        assert outcome.dashboard is None

    def test_an_id_that_no_longer_resolves_is_refused(self, monkeypatch):
        import uuid

        monkeypatch.setattr(sw, "_existing_or_none", lambda sid: None)
        monkeypatch.setattr(sw, "_link_to_lines", lambda *a, **k: pytest.fail(
            "nothing resolved, so there is nothing to link"))

        outcome = sw._submit(
            {"supplier_id": str(uuid.uuid4()), "name": FakeSupplier.name},
            _state(), "tom@x.com",
        )

        assert outcome.status == "pending"
        assert outcome.data["mode"] == "search"
        assert outcome.data["supplier_id"] == ""
        assert "no longer available" in outcome.error

    def test_the_picked_id_decides_the_path_not_the_client_label(self, picked):
        """``mode`` is a client-supplied label. Deciding on it would let a crafted
        submit skip the create path's validation entirely, or create a second copy
        of the supplier the user had just picked."""
        picked.setattr(sw, "_link_to_lines", lambda *a, **k: {
            "lines": [], "already_on": [], "error": ""})
        picked.setattr(sw, "create_local_supplier", lambda *a, **k: pytest.fail(
            "an id was picked, so there is nothing to create"))

        outcome = sw._submit(
            {"supplier_id": FakeSupplier.id, "name": FakeSupplier.name,
             "mode": "create"},
            _state(), "tom@x.com",
        )

        assert outcome.status == "submitted"
        assert outcome.result["existing"] is True

    def test_no_refresh_when_nothing_was_linked(self, picked):
        picked.setattr(sw, "_link_to_lines", lambda *a, **k: {
            "lines": [], "already_on": [1], "error": ""})

        outcome = sw._submit(
            {"supplier_id": FakeSupplier.id, "name": FakeSupplier.name,
             "line_mode": "all"},
            _state(), "tom@x.com",
        )

        assert outcome.status == "submitted"
        assert outcome.dashboard is None, (
            "every targeted line already carried it, so the RFQ page is not stale"
        )
        assert "was already a candidate" in outcome.notice


class TestRfqLines:
    """What the line picker gets to show."""

    def _rfq(self, monkeypatch, items):
        import includes.tools.quote_tools as quote_tools

        monkeypatch.setattr(quote_tools, "_get_rfq_dict_sync",
                            lambda rfq_id: {"items": items})

    def test_carries_the_brand_so_multi_brand_lines_can_be_told_apart(
        self, monkeypatch
    ):
        """A part number and a truncated description do not distinguish line 1
        (Bahco) from lines 2-6 (Milwaukee) on a drill/battery RFQ, and picking the
        wrong line links a supplier to the wrong hardware."""
        self._rfq(monkeypatch, [
            {"line": 1, "brand": "Bahco", "part_number": "8073",
             "input_description": "ADJUSTABLE WRENCH 300MM"},
            {"line": 2, "brand": "Milwaukee", "part_number": "M18B5",
             "input_description": "BATTERY 18V 5.0AH"},
        ])

        lines = sw.rfq_lines(RFQ)

        assert [line["brand"] for line in lines] == ["Bahco", "Milwaukee"]
        assert lines[0]["part_number"] == "8073"

    def test_a_line_without_a_brand_reports_an_empty_string(self, monkeypatch):
        """About half of all lines have no brand, and the template tests it for
        truthiness — None would render as "None" in the picker."""
        self._rfq(monkeypatch, [
            {"line": 1, "part_number": "10ST-T", "input_description": "Strop"},
        ])

        assert sw.rfq_lines(RFQ)[0]["brand"] == ""


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

    def test_a_bound_thread_opens_on_the_search_view(self, monkeypatch):
        monkeypatch.setattr(sw, "rfq_lines", lambda rfq_id: list(LINES))

        assert sw._context({"id": "w1", "rfq_id": RFQ})["mode"] == "search"

    def test_a_plain_thread_opens_on_the_create_form(self):
        """With no RFQ there is nothing to attach a supplier to, so a search
        could only ever end in "Don't add" — the card opens on the form instead."""
        assert sw._context({"id": "w1", "rfq_id": None})["mode"] == "create"

    @pytest.mark.parametrize("mode", ["search", "chosen", "create"])
    def test_a_submitted_view_survives_a_rerender(self, monkeypatch, mode):
        """A validation error or a duplicate confirm re-renders the card, and it
        must come back on the view the user was looking at — snapping to search
        would lose the form they were filling in."""
        monkeypatch.setattr(sw, "rfq_lines", lambda rfq_id: list(LINES))
        state = {"id": "w1", "rfq_id": RFQ, "data": {"mode": mode}}

        assert sw._context(state)["mode"] == mode

    def test_a_chosen_view_survives_on_a_plain_thread(self, monkeypatch):
        monkeypatch.setattr(sw, "rfq_lines", lambda rfq_id: [])
        state = {"id": "w1", "rfq_id": None, "data": {"mode": "chosen"}}

        assert sw._context(state)["mode"] == "chosen", (
            "an existing supplier on a thread with no RFQ is a valid outcome"
        )

    def test_an_unknown_mode_falls_back_rather_than_showing_nothing(self, monkeypatch):
        monkeypatch.setattr(sw, "rfq_lines", lambda rfq_id: list(LINES))
        state = {"id": "w1", "rfq_id": RFQ, "data": {"mode": "wat"}}

        assert sw._context(state)["mode"] == "search"

        unbound = {"id": "w1", "rfq_id": None, "data": {"mode": "wat"}}
        assert sw._context(unbound)["mode"] == "create"


# ---------------------------------------------------------------------------
# Contacts — which person the card records for this supplier
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session():
    """Session with a SAVEPOINT, for the parts that read the contacts table."""
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from includes.dashboard.database import _sync_url

    engine = create_engine(_sync_url(), pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection)
    session = Session(bind=connection)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess, trans):
        if trans.nested and not trans._parent.nested:
            sess.begin_nested()

    session.close = lambda: None
    yield session
    transaction.rollback()
    connection.close()


def _supplier_with_contacts(db_session, contacts, jsonb=None):
    """A supplier whose contacts live in the table (and optionally the JSONB)."""
    import uuid

    from includes.dashboard.models import Contact, Supplier

    supplier = Supplier(id=uuid.uuid4(), name="Iveco Brisbane", source="netsuite",
                        contacts=jsonb)
    db_session.add(supplier)
    db_session.flush()
    for label, email, name in contacts:
        db_session.add(Contact(supplier_id=supplier.id, label=label, email=email,
                               fullname=name))
    db_session.flush()
    return supplier


class TestContactOptions:
    """What the card's contact picker offers, and in what order."""

    def test_the_go_source_contact_comes_first(self, db_session, monkeypatch):
        import includes.dashboard.database as database

        monkeypatch.setattr(database, "get_session", lambda: db_session)
        supplier = _supplier_with_contacts(db_session, [
            ("Main", "parts@iveco.example", None),
            ("Source", "corey@iveco.example", "Corey Halloran"),
        ])

        options = sw._contact_options(supplier.id)

        assert [c["email"] for c in options] == ["corey@iveco.example",
                                                "parts@iveco.example"]
        assert options[0]["name"] == "Corey Halloran"
        assert options[0]["label"] == "Source"

    def test_the_jsonb_is_the_fallback_when_the_table_is_empty(
        self, db_session, monkeypatch
    ):
        """A web-discovered supplier can have contacts only in the legacy JSONB."""
        import includes.dashboard.database as database

        monkeypatch.setattr(database, "get_session", lambda: db_session)
        supplier = _supplier_with_contacts(
            db_session, [],
            jsonb=[{"label": "Source", "email": "sales@webco.example"}],
        )

        assert [c["email"] for c in sw._contact_options(supplier.id)] == [
            "sales@webco.example"
        ]

    def test_junk_and_addressless_rows_are_not_offered(self, db_session, monkeypatch):
        import includes.dashboard.database as database

        monkeypatch.setattr(database, "get_session", lambda: db_session)
        supplier = _supplier_with_contacts(db_session, [
            ("Main", "None", None),
            ("Source", "Daniel", "daniel@sensatek.example"),
            ("Source CC", None, "Phone Only"),
        ])

        options = sw._contact_options(supplier.id)

        assert [c["email"] for c in options] == ["daniel@sensatek.example"], (
            "the swapped columns are recovered, and the literal 'None' is not an "
            "address to offer"
        )

    def test_a_malformed_id_asks_nothing(self, monkeypatch):
        import includes.dashboard.database as database

        def _boom():
            raise AssertionError("a junk id must not reach the database")

        monkeypatch.setattr(database, "get_session", _boom)

        assert sw._contact_options("sup_1597") == []
        assert sw._contact_options("") == []


class TestContactOnTheLine:
    """The chosen contact travels with the link.

    Otherwise the next RFQ email re-derives it from the labels and the user's
    choice is lost — which is exactly the "fix the same supplier every time"
    problem the picker exists to end.
    """

    OPTIONS = [
        {"id": "c-source", "label": "Source", "name": "Corey", "email": "corey@x.com",
         "phone": ""},
        {"id": "c-main", "label": "Main", "name": "", "email": "parts@x.com",
         "phone": ""},
    ]

    def _entries(self, lines_and_writes):
        return lines_and_writes["calls"][0]["data"]["entries"]

    def test_a_chosen_contact_is_written_onto_every_entry(
        self, lines_and_writes, monkeypatch
    ):
        monkeypatch.setattr(sw, "_contact_options", lambda sid: list(self.OPTIONS))

        sw._link_to_lines(
            _state(), FakeSupplier(), {"line_mode": "all", "contact_id": "c-main"},
            "tom@x.com",
        )

        for entry in self._entries(lines_and_writes):
            assert entry["contact_id"] == "c-main"
            assert entry["contact_email"] == "parts@x.com"
            assert "contact_name" not in entry, (
                "the chosen contact has no name; an empty one must not be recorded"
            )

    def test_an_address_can_be_the_choice(self, lines_and_writes, monkeypatch):
        """JSONB rows have no id, so the picker's value is the address itself."""
        monkeypatch.setattr(sw, "_contact_options", lambda sid: list(self.OPTIONS))

        sw._link_to_lines(
            _state(), FakeSupplier(),
            {"line_mode": "all", "contact_id": "corey@x.com"}, "tom@x.com",
        )

        assert self._entries(lines_and_writes)[0]["contact_email"] == "corey@x.com"
        assert self._entries(lines_and_writes)[0]["contact_name"] == "Corey"

    def test_no_choice_records_the_ranked_answer(self, lines_and_writes, monkeypatch):
        """Nobody was asked (one contact, or a card from before the picker), so
        the link records what an email would have used."""
        monkeypatch.setattr(sw, "_contact_options", lambda sid: list(self.OPTIONS))

        sw._link_to_lines(_state(), FakeSupplier(), {"line_mode": "all"}, "tom@x.com")

        assert self._entries(lines_and_writes)[0]["contact_id"] == "c-source"

    def test_a_choice_that_no_longer_exists_falls_back(
        self, lines_and_writes, monkeypatch
    ):
        """The contact may have been deleted, or the id may belong to another
        supplier — either way the link still has to happen."""
        monkeypatch.setattr(sw, "_contact_options", lambda sid: list(self.OPTIONS))

        sw._link_to_lines(
            _state(), FakeSupplier(),
            {"line_mode": "all", "contact_id": "someone-elses-contact"}, "tom@x.com",
        )

        assert self._entries(lines_and_writes)[0]["contact_id"] == "c-source"

    def test_a_supplier_with_no_contacts_records_none(
        self, lines_and_writes, monkeypatch
    ):
        monkeypatch.setattr(sw, "_contact_options", lambda sid: [])

        sw._link_to_lines(_state(), FakeSupplier(), {"line_mode": "all"}, "tom@x.com")

        entry = self._entries(lines_and_writes)[0]
        assert "contact_id" not in entry and "contact_email" not in entry


class TestShortlistOnLink:
    """Adding a supplier from the chat almost always means the RFQ goes to them.

    "Shortlisted" is the set the Suppliers tab emails, so the box is ticked to
    begin with — and unticking it leaves the status alone rather than writing
    "candidate" over whatever the RFQ already said.
    """

    def _entries(self, lines_and_writes):
        return lines_and_writes["calls"][0]["data"]["entries"]

    def test_the_default_shortlists_them(self, lines_and_writes, monkeypatch):
        monkeypatch.setattr(sw, "_contact_options", lambda sid: [])

        sw._link_to_lines(_state(), FakeSupplier(),
                          {"line_mode": "all", "shortlist": "1"}, "tom@x.com")

        for entry in self._entries(lines_and_writes):
            assert entry["status"] == "shortlisted"

    def test_unticking_sends_no_status_at_all(self, lines_and_writes, monkeypatch):
        """Not \"candidate\": sending that would overwrite a supplier the RFQ had
        already shortlisted or quoted from."""
        monkeypatch.setattr(sw, "_contact_options", lambda sid: [])

        sw._link_to_lines(_state(), FakeSupplier(), {"line_mode": "all"}, "tom@x.com")

        assert "status" not in self._entries(lines_and_writes)[0]

    def test_the_notice_says_what_the_rfq_now_says(self, lines_and_writes, monkeypatch):
        monkeypatch.setattr(sw, "_contact_options", lambda sid: [])

        sw._link_to_lines(_state(), FakeSupplier(),
                          {"line_mode": "all", "shortlist": "1"}, "tom@x.com")

        linked = lambda **extra: {  # noqa: E731 - a tiny fixture for four calls
            "name": "Acme", "lines": [1], "already_on": [], "line_error": "",
            "existing": True, **extra,
        }

        assert sw._notice(linked(shortlisted=True)) == (
            "✅ Added **Acme** as a shortlisted supplier on line 1."
        )
        assert sw._notice(linked()) == "✅ Added **Acme** as a candidate on line 1."

        # The create path says it too, in its own words.
        created = {"name": "Acme", "lines": [1], "already_on": [], "line_error": "",
                   "existing": False, "shortlisted": True}
        assert sw._notice(created) == (
            "✅ Created supplier **Acme**. Added it as a shortlisted supplier on line 1."
        )

    def test_a_write_that_did_not_happen_does_not_claim_shortlisting(
        self, lines_and_writes, monkeypatch
    ):
        """No lines chosen means no status was set, whatever the box said."""
        monkeypatch.setattr(sw, "_contact_options", lambda sid: [])

        assert sw._link_status_fields(
            {"lines": [], "already_on": [], "error": ""}, {"shortlist": "1"}
        ) == {"shortlisted": False}
        assert sw._link_status_fields(
            {"lines": [], "already_on": [], "error": "rejected"}, {"shortlist": "1"}
        ) == {"shortlisted": False}
        assert sw._link_status_fields(
            {"lines": [1], "already_on": [], "error": ""}, {"shortlist": "1"}
        ) == {"shortlisted": True}
        assert sw._link_status_fields(
            {"lines": [], "already_on": [1], "error": ""}, {"shortlist": "1"}
        ) == {"shortlisted": True}


class TestTheTickSurvivesARerender:
    def test_an_unticked_box_is_recorded(self, monkeypatch):
        """A checkbox submits nothing when it is not ticked, so a rejected
        submission has to record the tick — otherwise unticking \"Shortlist\" and
        then hitting a validation error would quietly re-tick it."""
        monkeypatch.setattr(sw, "validate", lambda data: ({}, {"name": "required"}))

        outcome = sw._submit({"mode": "create", "name": ""}, _state(), "tom@x.com")

        assert outcome.status == "pending"
        assert outcome.data["shortlist"] == "0"

    def test_a_ticked_box_is_recorded(self, monkeypatch):
        monkeypatch.setattr(sw, "validate", lambda data: ({}, {"name": "required"}))

        outcome = sw._submit({"mode": "create", "name": "", "shortlist": "1"},
                             _state(), "tom@x.com")

        assert outcome.data["shortlist"] == "1"
