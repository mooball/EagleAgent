"""Tests for the in-chat widget framework (``includes/chat/widgets.py``).

The framework is transport-generic, so most of it is testable without HTTP: state
helpers are pure, and the two DB/transcript touchpoints are monkeypatched.
"""

import pytest

from includes.chat import widgets
from includes.chat.widgets import (
    ACTION_FIELD,
    WIDGET_META_KEY,
    WidgetOutcome,
    apply_outcome,
    dispatch_widget,
    form_to_data,
    get_widget,
    list_widgets,
    new_widget_state,
    register_widget,
    rfq_id_for_thread,
    widget_state_from_metadata,
)


class FakeForm:
    """Stands in for Starlette's ``FormData`` — only ``multi_items`` is read."""

    def __init__(self, items):
        self._items = items

    def multi_items(self):
        return list(self._items)


class TestFormToData:
    def test_single_values_stay_strings(self):
        data = form_to_data(FakeForm([("name", "Acme"), ("email", "a@b.com")]))
        assert data == {"name": "Acme", "email": "a@b.com"}

    def test_repeated_keys_become_a_list(self):
        data = form_to_data(FakeForm([("lines", "1"), ("lines", "3")]))
        assert data["lines"] == ["1", "3"]

    def test_a_single_repeated_key_still_reads_as_a_string(self):
        """Handlers would otherwise have to care whether one box was ticked."""
        data = form_to_data(FakeForm([("lines", "2")]))
        assert data["lines"] == "2"

    def test_skips_non_string_values(self):
        """An UploadFile reaching a text column is worse than dropping it."""
        data = form_to_data(FakeForm([("file", object()), ("name", "Acme")]))
        assert data == {"name": "Acme"}


class TestState:
    def test_new_state_is_pending_and_captures_the_rfq(self):
        state = new_widget_state("add_supplier", data={"name": "Acme"},
                                 rfq_id="RFQ-2026-1")
        assert state["status"] == "pending"
        assert state["rfq_id"] == "RFQ-2026-1"
        assert state["data"] == {"name": "Acme"}
        assert state["id"]

    def test_result_and_notice_survive_an_outcome(self):
        state = new_widget_state("add_supplier")
        updated = apply_outcome(state, WidgetOutcome(
            status="submitted", result={"supplier_id": "abc"}, notice="done",
        ))
        assert updated["status"] == "submitted"
        assert updated["result"] == {"supplier_id": "abc"}
        assert updated["notice"] == "done"

    def test_input_is_preserved_when_a_submission_is_rejected(self):
        """The confirm-duplicate round trip depends on this: the user must not
        retype the form to tick one box."""
        state = new_widget_state("add_supplier")
        data = {"name": "Acme", "email": "a@b.com"}
        updated = apply_outcome(state, WidgetOutcome(
            status="pending", data=data, duplicates=[{"name": "Acme AU"}],
        ))
        assert updated["data"] == data
        assert updated["status"] == "pending"
        assert updated["duplicates"] == [{"name": "Acme AU"}]

    def test_a_rejected_submission_does_not_echo_a_stale_result(self):
        state = new_widget_state("add_supplier")
        state["result"] = {"supplier_id": "old"}
        updated = apply_outcome(state, WidgetOutcome(status="pending"))
        assert updated["result"] == {"supplier_id": "old"}

    def test_metadata_round_trip(self):
        state = new_widget_state("add_supplier")
        assert widget_state_from_metadata({WIDGET_META_KEY: state}) == state

    @pytest.mark.parametrize("metadata", [
        None, {}, "nonsense", {"actions": []},
        {WIDGET_META_KEY: "not-a-dict"},
        {WIDGET_META_KEY: {"name": "add_supplier"}},          # no id
        {WIDGET_META_KEY: {"id": "x"}},                        # no name
    ])
    def test_metadata_without_a_widget_is_none(self, metadata):
        """Metadata is shared with actions and predates widgets — a step that is
        not a widget must never be mistaken for one."""
        assert widget_state_from_metadata(metadata) is None


class TestRegistry:
    def test_the_supplier_widget_is_registered(self):
        assert get_widget("add_supplier") is not None

    def test_listing_is_sorted_for_a_stable_menu(self):
        labels = [spec.label for spec in list_widgets()]
        assert labels == sorted(labels)

    def test_specs_expose_what_the_menu_needs(self):
        spec = get_widget("add_supplier")
        assert spec.label and spec.description and spec.template
        assert callable(spec.submit) and callable(spec.context)

    def test_duplicate_registration_is_refused(self):
        """Silently replacing would make two widgets fight over one menu entry."""
        with pytest.raises(ValueError):
            register_widget("add_supplier", label="x", description="y",
                            template="z.html", submit=lambda *a: None)

    def test_unknown_widget_raises_keyerror(self):
        """KeyError (not a silent None) so the caller can answer 404 — an unknown
        name is a stale client, not a handler failure."""
        with pytest.raises(KeyError):
            dispatch_widget("no_such_widget", {}, {}, "a@b.com")


class TestRfqIdForThread:
    def test_returns_none_when_the_lookup_fails(self, monkeypatch):
        """A widget without its RFQ is still worth showing — the supplier can
        still be created — so this must degrade rather than raise."""
        import includes.dashboard.database as db

        def _boom():
            raise RuntimeError("db unavailable")

        monkeypatch.setattr(db, "get_session", _boom)
        assert rfq_id_for_thread("thread-1") is None

    def test_reads_the_binding_from_the_thread(self, monkeypatch):
        import includes.dashboard.models as models

        row = type("R", (), {"rfq_number": "RFQ-2026-42"})()

        class _Query:
            def filter(self, *_a, **_k):
                return self

            def first(self):
                return row

        class _Session:
            def query(self, *_a, **_k):
                return _Query()

            def close(self):
                pass

        class _RFQThread:
            """Stands in for the ORM class: the real query filters on these
            column attributes, so the fake needs them to reach `.filter()`."""
            thread_id = "thread_id"
            rfq_number = "rfq_number"
            user_email = "user_email"

        import includes.dashboard.database as db

        monkeypatch.setattr(db, "get_session", lambda: _Session())
        monkeypatch.setattr(models, "RFQThread", _RFQThread)
        assert rfq_id_for_thread("thread-1") == "RFQ-2026-42"


class TestOpenWidgetStep:
    @pytest.mark.asyncio
    async def test_persists_a_widget_step_with_its_state(self, monkeypatch):
        from includes.chat import transcript

        captured = {}

        async def _create_step(thread_id, *, type_, name, output, metadata=None,
                              parent_id=None, step_id=None):
            captured.update(thread_id=thread_id, type_=type_, metadata=metadata,
                            requested_step_id=step_id)
            return step_id or "step-1"

        monkeypatch.setattr(transcript, "create_step", _create_step)
        monkeypatch.setattr(widgets, "rfq_id_for_thread", lambda tid: "RFQ-2026-7")

        step_id, state = await widgets.open_widget_step(
            "thread-1", "add_supplier", data={"name": "Acme"}
        )

        assert step_id == captured["requested_step_id"]
        assert captured["type_"] == "widget"
        stored = captured["metadata"][WIDGET_META_KEY]
        assert stored["name"] == "add_supplier"
        assert stored["data"] == {"name": "Acme"}
        assert stored["rfq_id"] == "RFQ-2026-7"
        assert stored["status"] == "pending"
        assert state == stored

    @pytest.mark.asyncio
    async def test_state_id_is_the_step_id(self, monkeypatch):
        """The card renders its own action URL from ``state["id"]``, and the
        render/submit routes resolve that id. Two ids = every request 404s
        against a row that does not exist (seen live 2026-09-24)."""
        from includes.chat import transcript

        async def _create_step(thread_id, *, step_id=None, **_kwargs):
            return step_id

        monkeypatch.setattr(transcript, "create_step", _create_step)

        step_id, state = await widgets.open_widget_step("thread-1", "add_supplier")

        assert state["id"] == step_id
