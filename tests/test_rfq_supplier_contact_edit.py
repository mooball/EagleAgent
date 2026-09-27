"""Choosing which contact an RFQ emails, and saving that choice.

Two things made this the long-standing sore spot on the Suppliers tab:

* the default recipient came from a stale snapshot, so the address had to be
  retyped on every send (fixed in ``supplier_contacts`` — see
  ``test_supplier_contacts.py`` and ``test_rfq_supplier_email.py``);
* saving a correction edited **every** contact of that supplier and deactivated
  all but one row in the contacts table. A one-off fix for one RFQ therefore
  flattened the supplier's contact list everywhere: the widget, the supplier
  page, and inbound-email matching, which reads the same table.

These tests pin the saving side: one contact changes, the others survive.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from includes.dashboard.models import Contact, RFQ, RFQItem, Supplier

RFQ_NUMBER = "RFQ-TEST-CONTACT"


def _rfq(db_session):
    """A draft RFQ, with the NOT NULL columns these tests do not care about."""
    rfq = RFQ(
        rfq_number=RFQ_NUMBER, customer="Acme", status="draft",
        created_by="tom@eagle-exports.com",
        created_date=datetime.now(timezone.utc),
    )
    db_session.add(rfq)
    db_session.flush()
    return rfq


@pytest.fixture
def db_session():
    """Session with a SAVEPOINT so the endpoint's commit cannot end the outer
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


@pytest.fixture
def endpoint(db_session, monkeypatch):
    """The endpoint's body, with its session and its RFQ read stubbed.

    ``_get_rfq_dict_sync`` opens its own session, which could not see rows created
    inside this test's savepoint — and the endpoint only reads it to check that
    the RFQ exists.
    """
    import includes.dashboard.routes.rfqs as rfqs
    import includes.tools.quote_tools as quote_tools

    monkeypatch.setattr(rfqs._helpers, "get_session", lambda: db_session)
    monkeypatch.setattr(
        quote_tools, "_get_rfq_dict_sync", lambda rfq_id: {"id": rfq_id, "items": []}
    )
    return rfqs


@pytest.fixture
def rfq_with_contacts(db_session):
    """A shortlisted supplier with a Go Source contact and a generic mailbox."""
    supplier = Supplier(id=uuid.uuid4(), name="Sydney Tools Pty Ltd", source="netsuite")
    db_session.add(supplier)
    db_session.flush()

    source = Contact(supplier_id=supplier.id, label="Source", fullname="Josh Carter",
                     email="joshuac@sydneytools.example")
    main = Contact(supplier_id=supplier.id, label="Main",
                   email="info@sydneytools.example")
    # The generic mailbox is inserted first on purpose: an implementation that
    # took "whichever row the database returns first" would pick it, and this
    # fixture would have hidden that.
    db_session.add_all([main, source])

    rfq = _rfq(db_session)
    item = RFQItem(
        rfq_id=rfq.id, line=1, input_description="DRILL", part_number="D-1",
        suppliers=[{
            "supplier_id": str(supplier.id),
            "name": supplier.name,
            "status": "shortlisted",
            "contacts": [
                {"id": str(main.id), "label": "Main", "email": "info@sydneytools.example"},
                {"id": str(source.id), "label": "Source", "name": "Josh Carter",
                 "email": "joshuac@sydneytools.example"},
            ],
        }],
    )
    db_session.add(item)
    db_session.flush()
    return {"rfq": rfq, "item": item, "supplier": supplier,
            "source": source, "main": main}


class FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


async def _update(rfqs, body, rfq_id=RFQ_NUMBER):
    return await rfqs.api_update_supplier_contact(FakeRequest(body), rfq_id, {})


def _rows(db_session, supplier_id):
    return {
        row.label: row
        for row in db_session.query(Contact).filter(Contact.supplier_id == supplier_id)
    }


class TestChoosingAContact:
    @pytest.mark.asyncio
    async def test_a_chosen_contact_is_recorded_on_the_line(
        self, db_session, rfq_with_contacts, endpoint
    ):
        """The choice has to survive: it is what the next send uses, and what the
        widget shows when the supplier is picked again."""
        c = rfq_with_contacts

        resp = await _update(endpoint, {
            "supplier_id": str(c["supplier"].id),
            "name": c["supplier"].name,
            "contact_id": str(c["main"].id),
            "email": "accounts@sydneytools.example",
            "contact_name": "Accounts",
        })

        assert resp.status_code == 200, resp.body
        sup = c["item"].suppliers[0]
        assert sup["contact_id"] == str(c["main"].id)
        assert sup["contact_email"] == "accounts@sydneytools.example"
        assert sup["contact_name"] == "Accounts"

        chosen = next(x for x in sup["contacts"] if x["id"] == str(c["main"].id))
        assert chosen["email"] == "accounts@sydneytools.example"

    @pytest.mark.asyncio
    async def test_the_other_contacts_are_not_rewritten(
        self, db_session, rfq_with_contacts, endpoint
    ):
        """The bug: every contact was given the new address, so choosing one
        address for one RFQ destroyed the rest of the list."""
        c = rfq_with_contacts

        await _update(endpoint, {
            "supplier_id": str(c["supplier"].id),
            "name": c["supplier"].name,
            "contact_id": str(c["main"].id),
            "email": "accounts@sydneytools.example",
        })

        sup = c["item"].suppliers[0]
        source_row = next(x for x in sup["contacts"] if x["id"] == str(c["source"].id))
        assert source_row["email"] == "joshuac@sydneytools.example", (
            "the Go Source contact keeps its own address"
        )

    @pytest.mark.asyncio
    async def test_the_other_contacts_stay_active_in_the_table(
        self, db_session, rfq_with_contacts, endpoint
    ):
        """Deactivating them hid the supplier's contacts from every other flow —
        the widget, the supplier page, and inbound-email matching."""
        c = rfq_with_contacts

        await _update(endpoint, {
            "supplier_id": str(c["supplier"].id),
            "name": c["supplier"].name,
            "contact_id": str(c["main"].id),
            "email": "accounts@sydneytools.example",
            "contact_name": "Accounts",
        })

        rows = _rows(db_session, c["supplier"].id)
        assert set(rows) == {"Source", "Main"}
        assert all(row.isinactive is False for row in rows.values()), (
            "a one-off correction must not retire the supplier's other contacts"
        )

    @pytest.mark.asyncio
    async def test_the_row_named_by_id_is_the_one_updated(
        self, db_session, rfq_with_contacts, endpoint
    ):
        """The old code updated ``existing[0]`` with no ordering — whichever row
        the database happened to return."""
        c = rfq_with_contacts

        await _update(endpoint, {
            "supplier_id": str(c["supplier"].id),
            "name": c["supplier"].name,
            "contact_id": str(c["source"].id),
            "email": "josh.new@sydneytools.example",
            "contact_name": "Josh Carter",
        })

        rows = _rows(db_session, c["supplier"].id)
        assert rows["Source"].email == "josh.new@sydneytools.example"
        assert rows["Main"].email == "info@sydneytools.example"


class TestWithoutAPick:
    @pytest.mark.asyncio
    async def test_a_retyped_address_updates_the_contact_we_would_have_emailed(
        self, db_session, rfq_with_contacts, endpoint
    ):
        """Free text still works — the supplier's address may simply have changed
        — and it lands on the Go Source contact rather than an arbitrary row."""
        c = rfq_with_contacts

        await _update(endpoint, {
            "supplier_id": str(c["supplier"].id),
            "name": c["supplier"].name,
            "email": "new@sydneytools.example",
        })

        rows = _rows(db_session, c["supplier"].id)
        assert rows["Source"].email == "new@sydneytools.example"
        assert rows["Main"].email == "info@sydneytools.example"

    @pytest.mark.asyncio
    async def test_a_supplier_with_no_contacts_gets_one(self, db_session, endpoint):
        supplier = Supplier(id=uuid.uuid4(), name="Brand New Co", source="manual")
        db_session.add(supplier)
        db_session.flush()
        rfq = _rfq(db_session)
        item = RFQItem(rfq_id=rfq.id, line=1, suppliers=[{
            "supplier_id": str(supplier.id), "name": supplier.name,
            "status": "shortlisted", "contacts": [],
        }])
        db_session.add(item)
        db_session.flush()

        resp = await _update(endpoint, {
            "supplier_id": str(supplier.id),
            "name": supplier.name,
            "email": "hello@brandnew.example",
            "contact_name": "Sam",
        })

        assert resp.status_code == 200, resp.body
        rows = _rows(db_session, supplier.id)
        assert list(rows) == ["Source"], "a new contact is the Go Source contact"
        assert rows["Source"].email == "hello@brandnew.example"
        assert item.suppliers[0]["contact_email"] == "hello@brandnew.example"


class TestValidation:
    @pytest.mark.asyncio
    async def test_an_address_is_required(self, db_session, rfq_with_contacts, endpoint):
        resp = await _update(endpoint, {"name": "Sydney Tools Pty Ltd", "email": "rubbish"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_a_supplier_name_is_required(self, db_session, rfq_with_contacts, endpoint):
        assert (await _update(endpoint, {"email": "a@b.com"})).status_code == 400

    @pytest.mark.asyncio
    async def test_an_unknown_supplier_is_404(self, db_session, rfq_with_contacts, endpoint):
        resp = await _update(endpoint, {"name": "Nobody Pty Ltd", "email": "a@b.com"})
        assert resp.status_code == 404
