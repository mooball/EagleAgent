"""Which contact a supplier's email should go to.

Every case here is taken from the production data, because the rules only make
sense against it: 498 suppliers have more than one contact, and the stored
snapshot on an RFQ line disagreed with the contacts table 28% of the time over
1,357 line entries. The junk values are real too — the literal string "None" in
an email column, two addresses in one field, and provenance rows with the name
and the address in each other's columns.

The rule the rest of the codebase already documented (``_resolve_salutation_name``
prefers "Source" for a supplier) is now the rule everywhere: **Source is the Go
Source contact NetSuite manages, and it is the person who handles purchasing.**
"""

import uuid

import pytest

from includes.dashboard.supplier_contacts import (
    CUSTOMER_LABELS,
    MAIN,
    SOURCE,
    SOURCE_CC,
    best_contact,
    best_from,
    merge_ordered,
    normalise_contact,
    normalise_email,
    rank_contacts,
    salutation_name,
)


class Row:
    """A ``Contact`` row, without needing a database."""

    def __init__(self, email=None, name=None, label=None, phone=None, isinactive=False):
        self.id = uuid.uuid4()
        self.email = email
        self.fullname = name
        self.label = label
        self.phone = phone
        self.isinactive = isinactive


class TestNormaliseEmail:
    def test_keeps_a_plain_address(self):
        assert normalise_email("sales@example.com") == "sales@example.com"

    def test_strips_surrounding_space_and_semicolons(self):
        assert normalise_email("  sales@example.com;  ") == "sales@example.com"

    @pytest.mark.parametrize("value", [
        None, "", "   ", "-", "n/a", "None", "none", "NULL", "unknown", "undefined",
    ])
    def test_rejects_the_junk_values_that_are_in_the_data(self, value):
        """'None' is not a placeholder here — it is a string sitting in the
        email column, and it was being used as the recipient."""
        assert normalise_email(value) == ""

    @pytest.mark.parametrize("value", [
        "Daniel",                       # a person's name, in the email column
        "daniel",                       # ... same, lowercased
        "example.com",                  # a bare domain
        "12345",                        # a phone number
        "Sales Team",                   # a team label
    ])
    def test_rejects_values_that_are_not_addresses(self, value):
        assert normalise_email(value) == ""

    def test_takes_the_first_address_out_of_a_field_holding_several(self):
        """Exedy Australia holds 'qld@exedy.com.au; GWilson@exedy.com.au' in one
        field — neither a valid recipient nor something to pass through."""
        assert normalise_email(
            "qld@exedy.com.au; GWilson@exedy.com.au"
        ) == "qld@exedy.com.au"
        assert normalise_email("a@x.com, b@y.com") == "a@x.com"

    def test_skips_a_leading_junk_part(self):
        assert normalise_email("None; real@example.com") == "real@example.com"


class TestNormaliseContact:
    def test_reads_a_snapshot_dict(self):
        contact = normalise_contact(
            {"id": "abc", "label": SOURCE, "name": "Peter", "email": "p@x.com",
             "phone": "07 1234"}
        )
        assert contact == {"id": "abc", "label": SOURCE, "name": "Peter",
                           "email": "p@x.com", "phone": "07 1234"}

    def test_reads_a_contact_row(self):
        row = Row(email="p@x.com", name="Peter", label=SOURCE, phone="07 1234")
        contact = normalise_contact(row)
        assert contact["id"] == str(row.id)
        assert contact["name"] == "Peter"
        assert contact["email"] == "p@x.com"

    def test_recovers_a_swapped_address(self):
        """SensaTek holds label='Source', email='Daniel',
        name='daniel@sensatek.com.au' — the row is usable, the columns are not."""
        contact = normalise_contact(
            {"label": SOURCE, "email": "Daniel", "name": "daniel@sensatek.com.au"}
        )
        assert contact["email"] == "daniel@sensatek.com.au"
        assert contact["name"] == "", (
            "the address must not be offered as a person's name — 'Hi "
            "daniel@sensatek.com.au,' is the failure this prevents"
        )

    def test_recovers_a_swapped_address_from_a_contact_row(self):
        """The same row read from the contacts table, where the name column is
        ``fullname`` rather than ``name``."""
        row = Row(email="Daniel", name="daniel@sensatek.com.au", label=SOURCE)
        contact = normalise_contact(row)
        assert contact["email"] == "daniel@sensatek.com.au"
        assert contact["name"] == ""

    def test_a_missing_label_is_not_a_label(self):
        assert normalise_contact({"email": "a@x.com"})["label"] == ""

    def test_empty_contact_is_blank_not_an_error(self):
        assert normalise_contact({})["email"] == ""
        assert normalise_contact(None)["email"] == ""


class TestRankContacts:
    def test_source_beats_main_and_a_cc(self):
        """The Go Source contact is the purchasing person; 'Main' is often a
        generic mailbox and 'Source CC' is somebody who was copied in."""
        ranked = rank_contacts([
            {"label": MAIN, "email": "info@example.com"},
            {"label": SOURCE_CC, "email": "assistant@example.com"},
            {"label": SOURCE, "email": "buyer@example.com"},
        ])
        assert [c["email"] for c in ranked] == [
            "buyer@example.com", "info@example.com", "assistant@example.com",
        ]

    def test_an_unlabelled_row_loses_to_a_labelled_one(self):
        """Provenance rows carry no label, and the old 'first row wins' rule let
        them beat the supplier's own contact."""
        ranked = rank_contacts([
            {"email": "someone@example.com"},
            {"label": MAIN, "email": "info@example.com"},
        ])
        assert [c["label"] for c in ranked] == [MAIN, ""]

    def test_a_named_person_beats_a_generic_mailbox_with_the_same_label(self):
        ranked = rank_contacts([
            {"label": MAIN, "email": "info@sydneytools.com.au"},
            {"label": MAIN, "email": "joshuac@sydneytools.com.au", "name": "Josh C"},
        ])
        assert ranked[0]["email"] == "joshuac@sydneytools.com.au"

    def test_the_same_address_twice_is_one_choice(self):
        """EMR Switchboards lists the same address as 'Source' and 'Main';
        offering it twice in a picker would be a fake choice."""
        ranked = rank_contacts([
            {"label": SOURCE, "email": "pad@emrswbds.com.au"},
            {"label": MAIN, "email": "PAD@emrswbds.com.au"},
        ])
        assert len(ranked) == 1
        assert ranked[0]["label"] == SOURCE, "the better label is the one kept"

    def test_contacts_without_an_address_are_not_choices(self):
        """A row with a name and no way to reach it cannot be emailed, so it must
        not count as an option either."""
        ranked = rank_contacts([
            {"name": "Phone Only", "phone": "07 1234"},
            {"email": "real@example.com"},
        ])
        assert [c["email"] for c in ranked] == ["real@example.com"]

    def test_junk_addresses_never_rank(self):
        assert rank_contacts([{"email": "None"}, {"email": "Daniel"}]) == []

    def test_empty_inputs(self):
        assert rank_contacts(None) == []
        assert rank_contacts([]) == []

    def test_a_customer_prefers_main(self):
        ranked = rank_contacts(
            [{"label": SOURCE, "email": "source@x.com"},
             {"label": MAIN, "email": "main@x.com"}],
            labels=CUSTOMER_LABELS,
        )
        assert ranked[0]["email"] == "main@x.com"


class TestBestContact:
    def test_nothing_reachable_is_none(self):
        assert best_contact([]) is None
        assert best_contact([{"email": "None"}]) is None

    def test_an_explicit_choice_beats_the_labels(self):
        """A person's decision outranks a default — until the contact is gone."""
        contacts = [
            {"id": "1", "label": SOURCE, "email": "buyer@example.com"},
            {"id": "2", "label": MAIN, "email": "accounts@example.com"},
        ]
        chosen = best_contact(contacts, preferred_id="2")
        assert chosen["email"] == "accounts@example.com"

    def test_a_preference_that_no_longer_exists_falls_back(self):
        """The contact may have been deleted or merged into another since the
        choice was made; the RFQ still has to be emailed."""
        contacts = [{"id": "1", "label": SOURCE, "email": "buyer@example.com"}]
        assert best_contact(contacts, preferred_id="gone")["email"] == "buyer@example.com"

    def test_a_preference_with_no_id_never_matches_by_accident(self):
        contacts = [{"label": SOURCE, "email": "buyer@example.com"}]
        assert best_contact(contacts, preferred_id="")["email"] == "buyer@example.com"


class TestBestFrom:
    def test_the_table_wins_over_a_stale_snapshot(self):
        """Sydney Tools: the snapshot says info@, the table says joshuac@."""
        chosen = best_from(
            db_contacts=[{"label": SOURCE, "email": "joshuac@sydneytools.com.au",
                          "name": "Josh C"}],
            snapshot=[{"email": "info@sydneytools.com.au"}],
        )
        assert chosen["email"] == "joshuac@sydneytools.com.au"

    def test_the_snapshot_is_used_when_the_table_has_nothing(self):
        """A web-discovered supplier can have contacts only in the JSONB."""
        chosen = best_from(
            db_contacts=[],
            snapshot=[{"label": SOURCE, "email": "sales@webco.com.au"}],
        )
        assert chosen["email"] == "sales@webco.com.au"

    def test_a_table_row_with_no_address_does_not_block_the_snapshot(self):
        chosen = best_from(
            db_contacts=[{"name": "Phone Only", "phone": "07 1234"}],
            snapshot=[{"email": "sales@webco.com.au"}],
        )
        assert chosen["email"] == "sales@webco.com.au"

    def test_a_preference_is_honoured_across_both_sources(self):
        chosen = best_from(
            db_contacts=[{"id": "1", "label": SOURCE, "email": "a@x.com"}],
            snapshot=[{"id": "9", "label": MAIN, "email": "b@x.com"}],
            preferred_id="9",
        )
        assert chosen["email"] == "a@x.com", (
            "the table wins: a preference pointing at a snapshot row that the "
            "table has replaced is not a preference we can honour"
        )


class TestSalutation:
    def test_the_first_name_of_the_chosen_contact(self):
        assert salutation_name({"name": "Josh Carter"}) == "Josh"

    def test_a_full_name_is_not_used_whole(self):
        assert salutation_name({"name": "Josh"}) == "Josh"

    def test_nothing_usable_is_none(self):
        assert salutation_name(None) is None
        assert salutation_name({"name": ""}) is None

    def test_a_single_letter_is_not_a_name(self):
        assert salutation_name({"name": "J"}) is None


class TestMergeOrdered:
    """Combining the contacts table with a stored snapshot.

    The two lists describe the same supplier, so duplicates are dropped — and the
    row that survives an address clash has to be the *better* one. Deduping in
    plain list order kept whichever came first, which is how a `Main` copy of a
    mailbox shadowed the `Source` row holding the same address: the address
    survived, the label and the person's name did not.
    """

    def test_a_same_address_row_keeps_the_better_label(self):
        """Torque Power Diesel: `Main` and `Source` both hold parts@, and the
        Source row is the one with somebody's name on it."""
        merged = merge_ordered(
            [
                {"label": MAIN, "email": "parts@torquepower.example"},
                {"label": SOURCE, "email": "parts@torquepower.example",
                 "name": "Parts team"},
                {"label": SOURCE_CC, "email": "tasmin@torquepower.example"},
            ],
            [],
        )

        # merge_ordered returns the rows it was given, not normalised copies.
        assert [(c["label"], c.get("name", "")) for c in merged] == [
            (SOURCE, "Parts team"), (SOURCE_CC, ""),
        ], "the row kept for an address must be the one the resolver would choose"

    def test_the_lists_are_deduplicated_across_both(self):
        merged = merge_ordered(
            [{"label": SOURCE, "email": "a@x.example", "name": "A"}],
            [{"label": SOURCE, "email": "a@x.example"},
             {"label": MAIN, "email": "b@x.example"}],
        )

        assert [c["email"] for c in merged] == ["a@x.example", "b@x.example"]

    def test_a_genuine_tie_keeps_the_primary_row(self):
        """Both lists can hold a 'Source' row for the same address — the snapshot
        one is stale, so the table's is the one to keep."""
        merged = merge_ordered(
            [{"label": SOURCE, "email": "a@x.example", "name": "Current"}],
            [{"label": SOURCE, "email": "a@x.example", "name": "Stale"}],
        )

        assert merged[0].get("name") == "Current"

    def test_a_named_row_beats_a_bare_one_with_the_same_label(self):
        merged = merge_ordered(
            [{"label": SOURCE, "email": "a@x.example"}],
            [{"label": SOURCE, "email": "a@x.example", "name": "Josh"}],
        )

        assert merged[0].get("name") == "Josh"

    def test_a_row_with_no_address_is_kept(self):
        """It may be the only phone number the supplier has."""
        merged = merge_ordered(
            [{"label": MAIN, "email": "a@x.example"}],
            [{"name": "Phone Only", "phone": "07 1234"}, {"phone": "07 1234"}],
        )

        assert len(merged) == 2, "the two phone-only rows are not the same row"

    def test_the_same_phone_number_twice_is_one_row(self):
        merged = merge_ordered([], [{"phone": "07 1234", "name": "A"},
                                    {"phone": "07 1234", "name": "B"}])

        assert len(merged) == 1

    def test_junk_is_ignored(self):
        assert merge_ordered([{"email": "a@x.example"}], [None, "not a dict"]) == [
            {"email": "a@x.example"}
        ]
