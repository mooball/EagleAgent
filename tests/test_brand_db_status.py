"""Tests for brand DB-status annotation on RFQ items (dashboard items table)."""

from unittest.mock import patch

import pytest

from includes.dashboard.routes.rfqs import _annotate_brand_db_status


def _rfq_with_items(*brands):
    return {
        "items": [
            {"line": i + 1, "brand": brand}
            for i, brand in enumerate(brands)
        ]
    }


def _result(status, alternatives=None):
    return {"status": status, "brand": None, "alternatives": alternatives or []}


class TestAnnotateBrandDbStatus:
    def test_statuses_annotated(self):
        rfq = _rfq_with_items("Toyota", "toyzz", "Zzz")
        lookup = {
            "Toyota": _result("exact", ["Toyota Parts", "Toyota Industrial"]),
            "toyzz": _result("near", ["Toyzz Parts", "Toyzz Industrial", "Toyzz Mining", "Toyzz Civil"]),
            "Zzz": _result("none"),
        }
        with patch("includes.tools.product_tools.match_brands", return_value=lookup):
            _annotate_brand_db_status(rfq)

        items = {i["line"]: i for i in rfq["items"]}
        assert items[1]["brand_db_status"] == "exact"
        assert items[1]["brand_db_alternatives"] == ["Toyota Parts", "Toyota Industrial"]
        assert items[1]["brand_db_alt_total"] == 2
        assert items[2]["brand_db_status"] == "near"
        assert items[2]["brand_db_alternatives"] == ["Toyzz Parts", "Toyzz Industrial", "Toyzz Mining"]
        assert items[2]["brand_db_alt_total"] == 4
        assert items[3]["brand_db_status"] == "none"
        assert items[3]["brand_db_alternatives"] == []

    def test_exclusions_and_blank_skipped(self):
        rfq = _rfq_with_items("Other", "n/a", "", None)
        with patch("includes.tools.product_tools.match_brands", return_value={}) as mock:
            _annotate_brand_db_status(rfq)
        # No brands worth looking up → matcher never called
        mock.assert_not_called()
        for item in rfq["items"]:
            assert "brand_db_status" not in item

    def test_lookup_failure_leaves_items_untouched(self):
        rfq = _rfq_with_items("Toyota")
        with patch("includes.tools.product_tools.match_brands", side_effect=Exception("db down")):
            _annotate_brand_db_status(rfq)
        assert "brand_db_status" not in rfq["items"][0]

    def test_no_items(self):
        with patch("includes.tools.product_tools.match_brands", return_value={}) as mock:
            _annotate_brand_db_status({"items": []})
        mock.assert_not_called()


class TestSyncReadinessBrandNsId:
    """brand_ns_id enrichment for the Quotation tab [NS] badges."""

    @pytest.fixture
    def db_session(self):
        import uuid as _uuid
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

    def _brand(self, session, name, netsuite_id="NS-111"):
        from includes.dashboard.models import Brand
        import uuid as _uuid
        b = Brand(netsuite_id=netsuite_id, name=name)
        session.add(b)
        session.flush()
        return b

    def _rfq(self, *items):
        return {"items": items}

    def test_linked_unlinked_and_near_miss(self, db_session):
        import uuid as _uuid
        suffix = _uuid.uuid4().hex[:6]
        self._brand(db_session, f"Toyzz {suffix}", netsuite_id="NS-EXACT")
        self._brand(db_session, f"Toyzz {suffix} Parts", netsuite_id="NS-PARTS")

        rfq = self._rfq(
            {"line": 1, "brand": f"Toyzz {suffix}"},   # exact → linked
            {"line": 2, "brand": f"Zzz {suffix}"},     # unknown → not linked
            {"line": 3, "brand": "toyzz"},             # near only → not linked
            {"line": 4, "brand": ""},                  # blank → None
            {"line": 5, "brand": "Other"},             # excluded → no badge
        )
        from includes.dashboard.routes.rfqs import _rfq_sync_readiness
        with patch("includes.dashboard.routes._helpers.get_session", return_value=db_session), \
             patch("includes.tools.product_tools.get_session", return_value=db_session):
            _rfq_sync_readiness(rfq)

        by_line = {i["line"]: i for i in rfq["items"]}
        assert by_line[1]["brand_ns_id"] == "NS-EXACT"
        assert by_line[2]["brand_ns_id"] is None
        assert by_line[3]["brand_ns_id"] is None
        assert by_line[4]["brand_ns_id"] is None
        assert by_line[5]["brand_ns_id"] is None
        assert by_line[5]["brand_is_excluded"] is True
