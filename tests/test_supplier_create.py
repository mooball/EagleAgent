"""Tests for the local supplier quick-add service.

Two halves:

* pure validation/formatting — no database, so these always run;
* creation — against a real Postgres session wrapped in a SAVEPOINT that is
  rolled back at the end (same pattern as ``tests/test_supplier_matching.py``).

The assertions about Contact rows and match keys are the point of the file. A
supplier row on its own is invisible to inbound-email matching, which is exactly
how ~900 web-discovered suppliers ended up permanently unlinked.
"""

import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from includes.dashboard.models import Contact, Supplier, SupplierMatchKey
from includes.dashboard.supplier_create import (
    LOOKUP_LIMIT,
    SUPPLIER_FIELDS,
    create_local_supplier,
    describe_matches,
    field_options,
    find_duplicates,
    lookup_suppliers,
    validate,
)


@pytest.fixture
def db_session():
    """Session with a SAVEPOINT so a helper's commit cannot end the outer
    transaction — everything rolls back at the end."""
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


def _unique(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


class TestFieldSpec:
    def test_requires_name_and_email_only(self):
        required = {f.key for f in SUPPLIER_FIELDS if f.required}
        assert required == {"name", "email"}

    def test_carries_no_netsuite_only_fields(self):
        """Category, tax item and friends belong to the promotion step — asking
        for them here is what turns a quick add into a form people abandon."""
        keys = {f.key for f in SUPPLIER_FIELDS}
        assert not keys & {"category", "category_id", "tax_item", "subsidiary",
                           "custom_form", "taxitem", "is_person"}

    def test_option_lists_resolve_for_every_select(self):
        options = field_options()
        for spec in SUPPLIER_FIELDS:
            if spec.kind == "select":
                assert spec.options in options, f"{spec.key} has no option source"
                assert options[spec.options], f"{spec.options} is empty"


class TestValidate:
    def test_name_and_email_are_required(self):
        clean, errors = validate({})
        assert "name" in errors and "email" in errors
        assert clean["name"] == ""

    def test_rejects_an_email_that_is_not_one(self):
        _, errors = validate({"name": "Acme", "email": "sales@acme"})
        assert "email" in errors

    def test_defaults_currency_to_aud(self):
        clean, errors = validate({"name": "Acme", "email": "a@acme.com"})
        assert errors == {}
        assert clean["currency"] == "AUD"

    def test_rejects_unknown_currency_and_country(self):
        _, errors = validate({
            "name": "Acme", "email": "a@acme.com",
            "currency": "XYZ", "country": "ZZ",
        })
        assert "currency" in errors and "country" in errors

    def test_normalises_country_and_currency_case(self):
        clean, errors = validate({
            "name": "Acme", "email": "a@acme.com",
            "currency": "usd", "country": "us",
        })
        assert errors == {}
        assert clean["currency"] == "USD"
        assert clean["country"] == "US"

    def test_bare_url_gets_a_scheme(self):
        clean, _ = validate({
            "name": "Acme", "email": "a@acme.com", "url": "www.acme.com.au",
        })
        assert clean["url"] == "https://www.acme.com.au"

    def test_handles_a_repeated_form_field(self):
        """``form_to_data`` yields a list for a repeated key; validate must not
        stringify it into "['AU']"."""
        clean, errors = validate({
            "name": "Acme", "email": "a@acme.com", "country": ["AU"],
        })
        assert errors == {}
        assert clean["country"] == "AU"

    def test_trims_whitespace(self):
        clean, _ = validate({"name": "  Acme  ", "email": " a@acme.com "})
        assert clean["name"] == "Acme"
        assert clean["email"] == "a@acme.com"


class TestDescribeMatches:
    def test_reports_a_confident_match_and_near_misses_separately(self):
        existing = Supplier(id=uuid.uuid4(), name="Acme Fasteners Pty Ltd")
        similar = Supplier(id=uuid.uuid4(), name="Acme Fasteners AU")
        match = type("M", (), {
            "supplier": existing,
            "near_misses": [{
                "supplier": similar, "confidence": 0.62,
                "rejected_because": "country_mismatch",
            }],
        })()

        report = describe_matches(match)

        assert report.has_match is True
        assert [row["name"] for row in report.display] == [
            "Acme Fasteners Pty Ltd", "Acme Fasteners AU",
        ]
        assert report.display[0]["kind"] == "existing"
        assert report.display[1]["kind"] == "similar"
        assert report.display[1]["confidence"] == 62
        assert report.near_misses == match.near_misses

    def test_no_match_reports_nothing(self):
        report = describe_matches(type("M", (), {"supplier": None, "near_misses": []})())
        assert report.has_match is False
        assert report.display == []


class TestFindDuplicates:
    def test_finds_an_existing_supplier_by_name(self, db_session):
        name = _unique("Dupco Fasteners")
        db_session.add(Supplier(name=name, source="netsuite"))
        db_session.flush()

        report = find_duplicates(name, session=db_session)

        assert report.has_match is True
        assert report.display[0]["name"] == name

    def test_does_not_flag_an_unrelated_name(self, db_session):
        report = find_duplicates(_unique("Totally Unrelated"), session=db_session)
        assert report.has_match is False
        assert report.display == []


class TestCreateLocalSupplier:
    def _clean(self, **overrides):
        clean, errors = validate({
            "name": _unique("Newly Added Pty Ltd"),
            "email": "sales@newlyadded.com.au",
            "contact_name": "Jo Bloggs",
            "phone": "02 9999 0000",
            "country": "AU",
            "currency": "AUD",
            "terms": "30 Days",
            "city": "Sydney",
            "postcode": "2000",
            **overrides,
        })
        assert errors == {}, errors
        return clean

    def test_marks_the_row_manual_and_records_who_made_it(self, db_session):
        clean = self._clean()

        supplier = create_local_supplier(
            clean, user_email="tom@eagle-exports.com", session=db_session
        )

        assert supplier.source == "manual"
        assert supplier.modified_by == "user:tom@eagle-exports.com"
        assert supplier.name == clean["name"]
        assert supplier.currency == "AUD"
        assert supplier.terms == "30 Days"
        assert supplier.country == "AU"
        assert supplier.city == "Sydney"
        assert supplier.netsuite_id is None

    def test_writes_a_contact_row_for_email_matching(self, db_session):
        clean = self._clean(email="orders@newlyadded.com.au")

        supplier = create_local_supplier(
            clean, user_email="tom@eagle-exports.com", session=db_session
        )
        db_session.flush()

        contacts = (
            db_session.query(Contact)
            .filter(Contact.supplier_id == supplier.id)
            .all()
        )
        assert len(contacts) == 1
        assert contacts[0].email == "orders@newlyadded.com.au"
        assert contacts[0].fullname == "Jo Bloggs"
        assert contacts[0].phone == "02 9999 0000"
        assert contacts[0].label == "Main"

    def test_mirrors_the_contact_into_the_jsonb_column(self, db_session):
        """The RFQ quotation tab and the NetSuite modal prefill from
        ``suppliers.contacts[0]`` — a Contact row alone is not enough."""
        clean = self._clean(email="prefill@newlyadded.com.au")

        supplier = create_local_supplier(
            clean, user_email="tom@eagle-exports.com", session=db_session
        )

        assert supplier.contacts[0]["email"] == "prefill@newlyadded.com.au"
        assert supplier.contacts[0]["name"] == "Jo Bloggs"

    def test_rebuilds_match_keys_including_the_email_domain(self, db_session):
        clean = self._clean(email="keys@matchkeys-demo.com.au")

        supplier = create_local_supplier(
            clean, user_email="tom@eagle-exports.com", session=db_session
        )
        db_session.flush()

        keys = {
            (row.key_type, row.key_value)
            for row in db_session.query(SupplierMatchKey)
            .filter(SupplierMatchKey.supplier_id == supplier.id)
            .all()
        }
        assert any(kind == "name" for kind, _ in keys), keys
        # domain_key() reduces a host to its registrable stem, so acme.com and
        # acme.com.au deliberately share a key — that is the "wider candidate
        # signal" the matcher corroborates with name similarity.
        assert ("domain", "matchkeys-demo") in keys, keys

    def test_excludes_free_mail_domains_from_match_keys(self, db_session):
        """A gmail contact must not make every gmail supplier look related."""
        clean = self._clean(email="someone@gmail.com")

        supplier = create_local_supplier(
            clean, user_email="tom@eagle-exports.com", session=db_session
        )
        db_session.flush()

        kinds = {
            row.key_type
            for row in db_session.query(SupplierMatchKey)
            .filter(SupplierMatchKey.supplier_id == supplier.id)
            .all()
        }
        assert kinds == {"name"}, kinds

    def test_omits_the_contact_when_none_was_given(self, db_session):
        clean, errors = validate({"name": _unique("Bare Minimum")})
        # email is required, so reach the no-contact branch the only way a real
        # submission can: an email but no name or phone.
        assert "email" in errors
        clean, errors = validate({
            "name": _unique("Bare Minimum"), "email": "only@bare.com",
        })
        assert errors == {}

        supplier = create_local_supplier(
            clean, user_email="tom@eagle-exports.com", session=db_session
        )
        db_session.flush()

        contacts = (
            db_session.query(Contact)
            .filter(Contact.supplier_id == supplier.id)
            .all()
        )
        assert len(contacts) == 1          # only the email was supplied
        assert contacts[0].fullname is None
        assert contacts[0].phone is None

    def test_queues_rejected_near_misses_for_review(self, db_session):
        from includes.dashboard.models import SupplierDuplicateCandidate

        existing = Supplier(name=_unique("Acme Fasteners"), source="netsuite")
        db_session.add(existing)
        db_session.flush()

        spec = self._clean()
        near_misses = [{
            "supplier": existing, "confidence": 0.71,
            "rejected_because": "country_mismatch",
        }]

        supplier = create_local_supplier(
            spec, user_email="tom@eagle-exports.com",
            near_misses=near_misses, session=db_session,
        )
        db_session.flush()

        pairs = (
            db_session.query(SupplierDuplicateCandidate)
            .filter(
                (SupplierDuplicateCandidate.primary_id == supplier.id)
                | (SupplierDuplicateCandidate.duplicate_id == supplier.id)
            )
            .all()
        )
        assert len(pairs) == 1
        assert pairs[0].status == "proposed"
        assert pairs[0].reasons == ["country_mismatch"]

    def test_survives_a_failing_near_miss_nomination(self, db_session, monkeypatch):
        """The supplier is the thing the user asked for — a review-queue write
        must never cost them the record."""
        import includes.dashboard.supplier_dedup as dedup

        def _boom(*_args, **_kwargs):
            raise RuntimeError("queue unavailable")

        monkeypatch.setattr(dedup, "nominate_near_misses", _boom)

        existing = Supplier(name=_unique("Exploding Co"), source="netsuite")
        db_session.add(existing)
        db_session.flush()

        supplier = create_local_supplier(
            self._clean(), user_email="tom@eagle-exports.com",
            near_misses=[{"supplier": existing, "confidence": 0.5,
                          "rejected_because": "name_similarity"}],
            session=db_session,
        )

        assert supplier.id is not None


class TestLookupSuppliers:
    """The widget's first screen: type a name, get ranked candidates.

    Goes through ``supplier_lookup`` so "which suppliers are searchable" stays
    defined in one place. What matters here is the ranking (an alphabetical list
    is useless as a typeahead), the wildcard escaping, and that the rows carry
    enough to choose between two suppliers with the same name.
    """

    def _add(self, session, name, **overrides):
        row = Supplier(name=name, source="manual", **overrides)
        session.add(row)
        session.flush()
        return row

    def test_a_query_under_the_floor_never_reaches_the_table(self):
        """One character matches most of the table — a full scan for a list
        nobody asked for."""
        assert lookup_suppliers("k") == []
        assert lookup_suppliers(" ") == []
        assert lookup_suppliers("") == []
        assert lookup_suppliers(None) == []

    def test_wildcards_are_literal(self, db_session):
        """Without escaping, "%" is LIKE's own wildcard: typing it matches every
        supplier in the table."""
        self._add(db_session, _unique("Percent Co"))

        assert lookup_suppliers("%", session=db_session) == []
        assert lookup_suppliers("_", session=db_session) == []
        assert lookup_suppliers("%%", session=db_session) == []

    def test_an_exact_name_outranks_longer_ones(self, db_session):
        stem = _unique("Zebra Bearings")
        exact = self._add(db_session, stem)
        prefixed = self._add(db_session, f"{stem} Holdings Pty Ltd")
        contained = self._add(db_session, f"Global {stem}")

        rows = lookup_suppliers(stem, session=db_session)

        assert [row["id"] for row in rows] == [
            str(exact.id), str(prefixed.id), str(contained.id)
        ], (
            "exact, then prefix, then contains — alphabetical ordering is what "
            "made 'Sydney Tools' unreachable within the limit"
        )

    def test_rows_carry_what_a_row_needs_to_be_choosable(self, db_session):
        name = _unique("Kraft Lookup")
        self._add(db_session, name, country="Australia", currency="AUD")

        row = next(r for r in lookup_suppliers(name, session=db_session)
                   if r["name"] == name)

        assert row["country"] == "Australia"
        assert row["currency"] == "AUD"
        assert row["email"] == "", "no contact yet, and that is a valid state"
        assert isinstance(row["id"], str), "the id goes straight into a form field"

    def test_the_contact_email_is_resolved(self, db_session):
        name = _unique("Contacted Co")
        supplier = self._add(db_session, name)
        db_session.add(Contact(supplier_id=supplier.id, email="main@contacted.example",
                               label="Main"))
        db_session.flush()

        row = next(r for r in lookup_suppliers(name, session=db_session)
                   if r["name"] == name)

        assert row["email"] == "main@contacted.example"

    def test_the_jsonb_contacts_are_the_fallback(self, db_session):
        """``suppliers.contacts`` holds entries the Contact table can lack — it is
        what the RFQ and NetSuite forms prefill from."""
        name = _unique("Jsonb Co")
        self._add(db_session, name, contacts=[{"email": "legacy@jsonb.example"}])

        row = next(r for r in lookup_suppliers(name, session=db_session)
                   if r["name"] == name)

        assert row["email"] == "legacy@jsonb.example"

    def test_an_inactive_supplier_is_not_offered(self, db_session):
        name = _unique("Retired Co")
        self._add(db_session, name, isinactive=True)

        assert lookup_suppliers(name, session=db_session) == []

    def test_a_merged_duplicate_is_not_offered(self, db_session):
        """This is a *linking* flow: a merged-away duplicate must never be
        attached to an RFQ, which is what linking it would do."""
        name = _unique("Merged Co")
        primary = self._add(db_session, _unique("Primary Co"))
        self._add(db_session, name, use_instead=primary.id)

        assert lookup_suppliers(name, session=db_session) == []

    def test_the_result_count_is_capped(self, db_session):
        stem = _unique("Capped Co")
        for index in range(12):
            self._add(db_session, f"{stem} Branch {index:02d}")

        rows = lookup_suppliers(stem, session=db_session)

        assert len(rows) == LOOKUP_LIMIT
