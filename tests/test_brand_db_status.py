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
        from includes.dashboard.models import Brand

        suffix = _uuid.uuid4().hex[:6]
        self._brand(db_session, f"Toyzz {suffix}", netsuite_id="NS-EXACT")
        self._brand(db_session, f"Toyzz {suffix} Parts", netsuite_id="NS-PARTS")

        # "Other" is a formal NetSuite brand and resolves through the normal
        # lookup. Neutralise any pre-existing 'Other' rows inside this
        # transaction (inactive rows are ignored by the lookup) so the
        # assertion is deterministic across environments.
        for b in db_session.query(Brand).filter(Brand.name == "Other").all():
            b.isinactive = True
        db_session.flush()
        other_ns_id = f"NS-OTHER-{suffix}"
        self._brand(db_session, "Other", netsuite_id=other_ns_id)

        rfq = self._rfq(
            {"line": 1, "brand": f"Toyzz {suffix}"},   # exact → linked
            {"line": 2, "brand": f"Zzz {suffix}"},     # unknown → not linked
            {"line": 3, "brand": "toyzz"},             # near only → not linked
            {"line": 4, "brand": ""},                  # blank → None
            {"line": 5, "brand": "Other"},             # formal NS record → linked
            {"line": 6, "brand": "n/a"},               # non-brand exclusion → not linked
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
        assert by_line[5]["brand_ns_id"] == other_ns_id
        assert by_line[5]["brand_is_excluded"] is True
        assert by_line[6]["brand_ns_id"] is None
        assert by_line[6]["brand_is_excluded"] is True

    def test_sync_all_clean_flag(self, db_session):
        """sync_all_clean: True when every line is on the OP and none dirty."""
        import uuid as _uuid
        from includes.dashboard.routes.rfqs import _rfq_sync_readiness

        suffix = _uuid.uuid4().hex[:6]
        brand = f"Toyzz {suffix}"
        self._brand(db_session, brand, netsuite_id="NS-EXACT")

        snapshot = {
            "part_number": "BOLT-123",
            "sale_price": 22.0,
            "cost_price": 10.5,
            "cost_currency": "AUD",
            "quantity": 4,
            "department_id": "8",
            "supplier_ns_id": "77",
            "brand_ns_id": "NS-EXACT",
        }

        def _make():
            return {
                "items": [{
                    "line": 1,
                    "part_number": "BOLT-123",
                    "brand": brand,
                    "cost_price": 10.5,
                    "sale_price": 22.0,
                    "quantity": 4,
                    "department_id": "8",
                    "suppliers": [{
                        "quote_status": "selected",
                        "netsuite_id": "77",
                        "quote_currency": "AUD",
                    }],
                }],
                "opportunity_id": "opp-uuid-1",
                "opportunity_sync_state": {
                    "last_synced_at": "2026-09-10T00:00:00Z",
                    "lines": [1],
                    "items": {"1": "555"},
                    "snapshot": {"1": snapshot},
                },
            }

        with patch("includes.dashboard.routes._helpers.get_session", return_value=db_session), \
             patch("includes.tools.product_tools.get_session", return_value=db_session):
            clean = _make()
            result = _rfq_sync_readiness(clean)
            assert result["sync_all_clean"] is True
            assert result["sync_dirty_count"] == 0
            assert result["sync_orphan_lines"] == []

            # Any edit re-activates the button
            dirty = _make()
            dirty["items"][0]["quantity"] = 9
            result = _rfq_sync_readiness(dirty)
            assert result["sync_all_clean"] is False
            assert result["sync_dirty_count"] == 1

            # A line removed from the RFQ but still on the OP re-activates too
            orphan = _make()
            orphan["items"] = []
            result = _rfq_sync_readiness(orphan)
            assert result["sync_all_clean"] is False
            assert result["sync_orphan_lines"] == [1]
