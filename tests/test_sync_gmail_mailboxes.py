"""Tests for scripts/sync_gmail_mailboxes.py — thread-based RFQ inheritance.

Regression: email #52805 was a brand-new quote request ("Quotation", no
reply headers) that Gmail folded into an older conversation by
subject/recipient similarity. The sync's Tier-1 thread match blindly
inherited the thread's RFQ link, attaching the new request to the wrong
RFQ (RFQ-2026-1275).
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from includes.dashboard.models import Customer, EmailTracking, RFQ


@pytest.fixture
def db_session():
    """DB session with SAVEPOINT so commits inside helpers don't end the
    outer transaction — everything rolls back at the end."""
    from includes.dashboard.database import _sync_url
    engine = create_engine(_sync_url(), pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection)
    session = Session(bind=connection)
    session.begin_nested()

    from sqlalchemy import event
    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess, trans):
        if trans.nested and not trans._parent.nested:
            sess.begin_nested()

    session.close = lambda: None
    yield session
    transaction.rollback()
    connection.close()


def _make_thread_row(session, thread_id="thread-old", **overrides):
    defaults = {
        "gmail_thread_id": thread_id,
        "gmail_message_id": f"msg-old-{uuid.uuid4().hex[:8]}",
        "user_email": "harry@eagle-exports.com",
        "sender_email": "robbie@customer.com",
        "direction": "received",
        "subject": "Quotation",
        "rfq_token": "RFQ-2026-1275",
        "match_type": "manual",
        "body_markdown": "Original request",
    }
    defaults.update(overrides)
    row = EmailTracking(**defaults)
    session.add(row)
    session.flush()
    return row


def _make_msg_meta(**overrides):
    meta = {
        "id": f"msg-new-{uuid.uuid4().hex[:8]}",
        "threadId": "thread-old",
        "labelIds": [],
        "from": "Richard Karo <richard@customer.com>",
        "to": "harry@eagle-exports.com",
        "cc": "",
        "subject": "Quotation",
        "date": datetime.now(timezone.utc),
        "x_eagle_rfq": None,
        "x_eagle_op": None,
        "x_eagle_opportunity": None,
        "in_reply_to": None,
        "references": None,
    }
    meta.update(overrides)
    return meta


def _make_content():
    return {
        "body_markdown": "Please quote 1 Cap Fuel-Filler-304-3885",
        "body_html": "<p>Please quote 1 Cap Fuel-Filler-304-3885</p>",
        "attachments_json": [],
        "sender_name": "Richard Karo",
        "all_recipients": ["richard@customer.com"],
    }


CUSTOMER_ID = uuid.uuid4()


def _make_customer(session, customer_id=CUSTOMER_ID):
    customer = Customer(
        id=customer_id,
        netsuite_id=f"TEST-{uuid.uuid4().hex[:8]}",
        companyname="Hebou Constructions (PNG) Ltd",
        email="richard@customer.com",
    )
    session.add(customer)
    session.flush()
    return customer


def _make_rfq(session, rfq_number="RFQ-2026-1275", netsuite_opportunity=None):
    rfq = RFQ(
        rfq_number=rfq_number,
        customer="Test Customer",
        created_by="test",
        created_date=datetime.now(timezone.utc),
        netsuite_opportunity=netsuite_opportunity,
    )
    session.add(rfq)
    session.flush()
    return rfq


class TestExtractMessageMetadata:
    def test_captures_reply_headers(self):
        from scripts.sync_gmail_mailboxes import extract_message_metadata

        service = MagicMock()
        service.users().messages().get.return_value.execute.return_value = {
            "id": "msg-1",
            "threadId": "thread-1",
            "historyId": "123",
            "labelIds": [],
            "internalDate": "1750000000000",
            "payload": {
                "headers": [
                    {"name": "From", "value": "a@b.com"},
                    {"name": "Subject", "value": "Re: Quote"},
                    {"name": "In-Reply-To", "value": "<msg-old@mail>"},
                    {"name": "References", "value": "<msg-old@mail>"},
                ]
            },
        }

        meta = extract_message_metadata(service, "msg-1")

        assert meta["in_reply_to"] == "<msg-old@mail>"
        assert meta["references"] == "<msg-old@mail>"

    def test_no_reply_headers_when_absent(self):
        from scripts.sync_gmail_mailboxes import extract_message_metadata

        service = MagicMock()
        service.users().messages().get.return_value.execute.return_value = {
            "id": "msg-2",
            "threadId": "thread-2",
            "historyId": "123",
            "labelIds": [],
            "internalDate": "1750000000000",
            "payload": {
                "headers": [
                    {"name": "From", "value": "a@b.com"},
                    {"name": "Subject", "value": "Quotation"},
                ]
            },
        }

        meta = extract_message_metadata(service, "msg-2")

        assert meta["in_reply_to"] is None
        assert meta["references"] is None


class TestThreadMatchRfqInheritance:
    def _run_process(self, db_session, msg_meta, thread_row, contact_match):
        from scripts import sync_gmail_mailboxes as sync_mod

        sync_mod._quote_pipeline_candidates.clear()
        with patch.object(sync_mod, "extract_message_metadata", return_value=msg_meta), \
             patch.object(sync_mod, "fetch_message_content", return_value=_make_content()), \
             patch.object(sync_mod, "match_by_contact", return_value=contact_match):

            return sync_mod.process_message(
                db_session,
                service=None,
                user_email="harry@eagle-exports.com",
                message_id=msg_meta["id"],
                domain_index={},
            )

    def test_reply_inherits_rfq_link(self, db_session):
        thread_row = _make_thread_row(db_session)
        msg_meta = _make_msg_meta(
            threadId=thread_row.gmail_thread_id,
            in_reply_to="<old@mail>",
            references="<old@mail>",
        )

        result = self._run_process(
            db_session, msg_meta, thread_row,
            {"match_type": None, "supplier_id": None, "customer_id": None},
        )

        assert result == "tier1"
        row = db_session.query(EmailTracking).filter(
            EmailTracking.gmail_message_id == msg_meta["id"]
        ).first()
        assert row is not None
        assert row.rfq_token == "RFQ-2026-1275"
        assert row.match_type == "manual"
        assert row.body_markdown == "Please quote 1 Cap Fuel-Filler-304-3885"

    def test_new_message_folded_into_thread_does_not_inherit_rfq(self, db_session):
        _make_customer(db_session)
        thread_row = _make_thread_row(db_session)
        msg_meta = _make_msg_meta(threadId=thread_row.gmail_thread_id)  # no reply headers

        result = self._run_process(
            db_session, msg_meta, thread_row,
            {"match_type": "domain", "supplier_id": None, "customer_id": CUSTOMER_ID},
        )

        assert result == "tier1"
        row = db_session.query(EmailTracking).filter(
            EmailTracking.gmail_message_id == msg_meta["id"]
        ).first()
        assert row is not None
        assert row.rfq_token is None
        assert row.rfq_id is None
        # Entity re-matched by contact — customer still linked
        assert row.customer_id == CUSTOMER_ID
        assert row.match_type == "domain"
        # Content still captured (no placeholder row)
        assert row.body_markdown == "Please quote 1 Cap Fuel-Filler-304-3885"

    def test_folded_message_with_rfq_number_in_subject_links_rfq(self, db_session):
        """Supplier emails a fresh message (no reply headers) quoting the
        RFQ number in the subject — should still link to that RFQ."""
        _make_customer(db_session)
        _make_rfq(db_session, rfq_number="RFQ-2026-1275")
        thread_row = _make_thread_row(db_session)
        msg_meta = _make_msg_meta(
            threadId=thread_row.gmail_thread_id,
            subject="Quotation for RFQ-2026-1275",
        )

        result = self._run_process(
            db_session, msg_meta, thread_row,
            {"match_type": "domain", "supplier_id": None, "customer_id": CUSTOMER_ID},
        )

        assert result == "tier1"
        row = db_session.query(EmailTracking).filter(
            EmailTracking.gmail_message_id == msg_meta["id"]
        ).first()
        assert row is not None
        assert row.rfq_token == "RFQ-2026-1275"
        assert row.rfq_id == "RFQ-2026-1275"
        # Entities still re-matched by contact
        assert row.customer_id == CUSTOMER_ID
        assert row.match_type == "domain"
        assert row.body_markdown == "Please quote 1 Cap Fuel-Filler-304-3885"

    def test_folded_message_with_op_number_in_subject_links_rfq(self, db_session):
        """Fresh message quoting the NetSuite Opportunity number links to the
        RFQ that carries that opportunity (even a different one than the
        thread's own RFQ)."""
        _make_customer(db_session)
        _make_rfq(db_session, rfq_number="RFQ-2026-3000", netsuite_opportunity="OP72655")
        thread_row = _make_thread_row(db_session)
        msg_meta = _make_msg_meta(
            threadId=thread_row.gmail_thread_id,
            subject="Quote for OP72655",
        )

        result = self._run_process(
            db_session, msg_meta, thread_row,
            {"match_type": "domain", "supplier_id": None, "customer_id": CUSTOMER_ID},
        )

        assert result == "tier1"
        row = db_session.query(EmailTracking).filter(
            EmailTracking.gmail_message_id == msg_meta["id"]
        ).first()
        assert row is not None
        assert row.rfq_token == "RFQ-2026-3000"
        assert row.rfq_id == "RFQ-2026-3000"
        assert row.opportunity_id == "OP72655"
        assert row.customer_id == CUSTOMER_ID
        assert row.match_type == "domain"

    def test_folded_message_with_unresolvable_subject_number_not_linked(self, db_session):
        """RFQ number in subject but no matching RFQ row exists — no link
        (safety: never fabricate a link)."""
        _make_customer(db_session)
        thread_row = _make_thread_row(db_session)
        msg_meta = _make_msg_meta(
            threadId=thread_row.gmail_thread_id,
            subject="Quotation for RFQ-2026-9999",
        )

        result = self._run_process(
            db_session, msg_meta, thread_row,
            {"match_type": "domain", "supplier_id": None, "customer_id": CUSTOMER_ID},
        )

        assert result == "tier1"
        row = db_session.query(EmailTracking).filter(
            EmailTracking.gmail_message_id == msg_meta["id"]
        ).first()
        assert row is not None
        assert row.rfq_token is None
        assert row.rfq_id is None
        assert row.customer_id == CUSTOMER_ID
