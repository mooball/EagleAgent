"""The recipient an RFQ email is addressed to.

The Suppliers tab and the bulk-compose modal read ``_build_rfq_supplier_email_data``,
and the only way to correct a wrong address was to notice it and retype it. Two
things were wrong with the default:

* it took the **first** row with an email and the **first** row with a name — two
  independent scans, so the address and the person could come from different
  contacts;
* the stored snapshot on a line is written when the supplier is linked and goes
  stale. Over 1,357 line entries with more than one contact it disagreed with the
  contacts table 37% of the time, because the table's rows were *appended* and the
  snapshot's generic ``info@``/``sales@`` came first.

``Source`` is the Go Source contact — the purchasing person, held in NetSuite —
and it is what the rest of the codebase already treats as a supplier's preferred
contact. These tests pin that, and the option list the picker needs.
"""

import json
import re

from includes.dashboard.routes.rfqs import _build_rfq_supplier_email_data


def _rfq(**supplier_overrides):
    supplier = {
        "name": "Sydney Tools Pty Ltd",
        "supplier_id": "11111111-1111-1111-1111-111111111111",
        "status": "shortlisted",
        "contacts": [],
        "country": "AU",
        "currency": "AUD",
    }
    supplier.update(supplier_overrides)
    return {
        "id": "RFQ-2026-1",
        "items": [
            {
                "line": 1,
                "input_description": "DRILL",
                "part_number": "D-1",
                "suppliers": [supplier],
            }
        ],
    }


def _only(rfq):
    suppliers = _build_rfq_supplier_email_data(rfq)
    assert len(suppliers) == 1
    return suppliers[0]


class TestRecipient:
    def test_the_go_source_contact_wins(self):
        """'Source' is the purchasing person; 'Main' is often a generic mailbox
        and 'Source CC' somebody who was copied in."""
        supplier = _only(_rfq(contacts=[
            {"label": "Main", "email": "info@sydneytools.com.au"},
            {"label": "Source CC", "email": "admin@sydneytools.com.au"},
            {"label": "Source", "email": "joshuac@sydneytools.com.au", "name": "Josh C"},
        ]))

        assert supplier["email"] == "joshuac@sydneytools.com.au"
        assert supplier["contact_name"] == "Josh C"
        assert supplier["contact_label"] == "Source"

    def test_the_address_and_the_name_come_from_the_same_row(self):
        """The old two-scan version could pair a name with another contact's
        address, which is how a greeting ends up naming the wrong person."""
        supplier = _only(_rfq(contacts=[
            {"label": "Source", "email": "buyer@example.com"},
            {"label": "Main", "email": "info@example.com", "name": "Reception"},
        ]))

        assert supplier["email"] == "buyer@example.com"
        assert supplier["contact_name"] is None, (
            "the chosen contact has no name; borrowing 'Reception' from a contact "
            "we are not emailing would address the wrong person"
        )

    def test_a_generic_row_does_not_beat_a_labelled_one(self):
        supplier = _only(_rfq(contacts=[
            {"email": "someone@example.com"},
            {"label": "Main", "email": "info@example.com"},
        ]))

        assert supplier["email"] == "info@example.com"

    def test_the_table_row_wins_a_label_tie(self):
        """Both lists can hold a 'Source' row — 1,060 stored snapshot rows are
        labelled that way — so the enrichment puts the table's rows first."""
        supplier = _only(_rfq(contacts=[
            {"label": "Source", "email": "current@example.com"},
            {"label": "Source", "email": "stale@example.com"},
        ]))

        assert supplier["email"] == "current@example.com"

    def test_junk_addresses_are_never_used(self):
        """The literal string 'None' sits in real email columns, and two
        addresses sometimes share one field."""
        supplier = _only(_rfq(contacts=[
            {"label": "Source", "email": "None"},
            {"label": "Main", "email": "real@example.com; second@example.com"},
        ]))

        assert supplier["email"] == "real@example.com"

    def test_a_supplier_with_nothing_reachable_has_no_recipient(self):
        supplier = _only(_rfq(contacts=[{"label": "Source", "email": "None"}]))

        assert supplier["email"] is None
        assert supplier["contact_name"] is None

    def test_no_contacts_at_all_is_not_an_error(self):
        assert _only(_rfq())["email"] is None


class TestPersistedChoice:
    def test_a_saved_choice_is_used(self):
        """Chosen once, respected on every later send — otherwise the user fixes
        the same supplier's contact every time they email them."""
        supplier = _only(_rfq(
            contacts=[
                {"id": "c1", "label": "Source", "email": "buyer@example.com"},
                {"id": "c2", "label": "Main", "email": "accounts@example.com"},
            ],
            contact_id="c2",
        ))

        assert supplier["email"] == "accounts@example.com"

    def test_a_choice_that_no_longer_exists_falls_back(self):
        supplier = _only(_rfq(
            contacts=[{"id": "c1", "label": "Source", "email": "buyer@example.com"}],
            contact_id="deleted-contact",
        ))

        assert supplier["email"] == "buyer@example.com"


class TestOptions:
    def test_the_alternatives_are_offered_in_order(self):
        supplier = _only(_rfq(contacts=[
            {"label": "Main", "email": "info@example.com"},
            {"label": "Source", "email": "buyer@example.com", "name": "Josh"},
        ]))

        assert [c["email"] for c in supplier["contact_options"]] == [
            "buyer@example.com", "info@example.com",
        ]

    def test_one_address_twice_is_offered_once(self):
        """A picker that lists the same address twice is a fake choice."""
        supplier = _only(_rfq(contacts=[
            {"label": "Source", "email": "pad@example.com"},
            {"label": "Main", "email": "PAD@example.com"},
        ]))

        assert len(supplier["contact_options"]) == 1

    def test_unusable_rows_are_not_offered(self):
        supplier = _only(_rfq(contacts=[
            {"email": "None"},
            {"name": "Phone Only", "phone": "07 1234"},
            {"email": "real@example.com"},
        ]))

        assert [c["email"] for c in supplier["contact_options"]] == ["real@example.com"]

    def test_a_single_contact_still_reports_its_option(self):
        supplier = _only(_rfq(contacts=[{"label": "Source", "email": "a@example.com"}]))

        assert len(supplier["contact_options"]) == 1


class TestSalutation:
    def test_the_greeting_names_the_recipient(self):
        assert _only(_rfq(contacts=[
            {"label": "Source", "email": "buyer@example.com", "name": "Josh Carter"},
        ]))["salutation_name"] == "Josh"

    def test_a_generic_recipient_gets_no_name(self):
        """Falling back to 'Hi Sales Team,' is better than naming a person at an
        address we are not writing to."""
        assert _only(_rfq(contacts=[
            {"label": "Source", "email": "buyer@example.com"},
        ]))["salutation_name"] is None

    def test_a_name_column_holding_an_address_is_not_a_name(self):
        assert _only(_rfq(contacts=[
            {"label": "Source", "email": "Daniel", "name": "daniel@sensatek.com.au"},
        ]))["salutation_name"] is None


class TestGrouping:
    def test_one_supplier_on_several_lines_is_one_recipient(self):
        rfq = _rfq(contacts=[{"label": "Source", "email": "buyer@example.com"}])
        rfq["items"].append({
            "line": 2, "input_description": "SAW", "part_number": "S-1",
            "suppliers": [dict(rfq["items"][0]["suppliers"][0])],
        })

        suppliers = _build_rfq_supplier_email_data(rfq)

        assert len(suppliers) == 1, "one email per supplier, not per line"
        assert [item["line"] for item in suppliers[0]["line_items"]] == [1, 2]

    def test_only_shortlisted_suppliers_are_emailed(self):
        rfq = _rfq(status="candidate",
                   contacts=[{"label": "Source", "email": "buyer@example.com"}])

        assert _build_rfq_supplier_email_data(rfq) == []


# ============================================================================
# The Suppliers tab row: which contact, and how it is changed
# ============================================================================

def _render_panel(**overrides):
    """The Suppliers-tab panel, rendered from the same data the builder emits."""
    from includes.dashboard.routes._helpers import templates

    supplier = {
        "name": "Iveco Brisbane",
        "email": "corey@iveco.example",
        "contact_name": "Corey Halloran",
        "contact_id": "c-source",
        "contact_label": "Source",
        "contact_options": [],
        "salutation_name": "Corey",
        "supplier_id": "11111111-1111-1111-1111-111111111111",
        "country": "AU",
        "currency": "AUD",
        "line_items": [{"line": 1, "description": "DRILL", "part_number": "D-1"}],
        "has_been_emailed": False,
    }
    supplier.update(overrides)
    return templates.env.get_template("partials/_rfq_email_suppliers.html").render(
        rfq={"id": "RFQ-2026-1", "netsuite_opportunity": None},
        user={"name": "Tom", "email": "tom@eagle-exports.com"},
        suppliers=[supplier],
    )


CONTACTS = [
    {"id": "c-source", "label": "Source", "name": "Corey Halloran",
     "email": "corey@iveco.example", "phone": ""},
    {"id": "c-parts", "label": "Main", "name": "",
     "email": "parts@iveco.example", "phone": ""},
]


class TestThePanelRow:
    """The Suppliers tab is where the recipient is read before sending.

    With one contact there is nothing to decide, so the row shows the address. With
    several it offers them — the address is the thing being chosen, and retyping it
    was the only way to change it before.
    """

    def test_several_contacts_are_offered(self):
        html = _render_panel(contact_options=CONTACTS)

        assert "<select" in html
        assert "corey@iveco.example" in html and "parts@iveco.example" in html

    def test_the_options_name_the_person(self):
        """The name is how you know who you are writing to — two addresses at one
        supplier look alike without it."""
        html = _render_panel(contact_options=CONTACTS)

        assert "Source — Corey Halloran — corey@iveco.example" in html
        assert "Main — parts@iveco.example" in html, (
            "a contact with no name skips it rather than showing a blank"
        )

    def test_the_chosen_contact_is_the_one_selected(self):
        html = _render_panel(contact_options=CONTACTS, contact_id="c-parts")

        selected = re.search(r'value="([^"]+)"\s+data-email="parts@iveco\.example"', html)
        assert selected, "the row for the recorded contact should be the one marked"
        assert 'value="c-parts"' in html

    def test_a_single_contact_is_not_a_question(self):
        html = _render_panel(contact_options=CONTACTS[:1])

        assert "<select" not in html, "a picker with one option is a fake choice"
        assert "corey@iveco.example" in html, "the address is still shown"

    def test_no_contacts_at_all(self):
        html = _render_panel(contact_options=[], email=None, contact_name=None)

        assert "<select" not in html
        assert "No contact info" in html

    def test_the_picker_carries_what_the_save_needs(self):
        """The address and the name travel on the option, because the value is
        the id when there is one and the address when there is not."""
        html = _render_panel(contact_options=CONTACTS)

        assert 'data-email="parts@iveco.example"' in html
        assert 'data-name=""' in html
        assert "saveContact(" in html

    def test_the_recipient_data_reaches_the_compose_modal(self):
        """The modal builds its recipient rows from this JSON, so a field missing
        here means no picker at the last screen before sending."""
        html = _render_panel(contact_options=CONTACTS)

        payload = re.search(r'id="bulk-supplier-data">(.*?)</script>', html, re.S).group(1)
        data = json.loads(payload)

        assert data[0]["contact_options"][0]["email"] == "corey@iveco.example"
        assert data[0]["contact_id"] == "c-source"


def test_the_compose_modal_picker_names_the_person_too():
    """The last screen before sending offers the same list, so it has to read the
    same way. Its options are Alpine bindings, so this reads the source: a missing
    `c.name` there is invisible to every other test."""
    from pathlib import Path

    template = (
        Path(__file__).parent.parent
        / "templates" / "partials" / "_email_bulk_compose_modal.html"
    ).read_text()

    assert "[c.label, c.name, c.email]" in template, (
        "the compose picker must show the contact's name, like the tab and the card"
    )
