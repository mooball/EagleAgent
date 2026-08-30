"""Tests for scripts/scan_supplier_duplicates.py.

Pair scoring is pure; the upsert tests run against the real dev database
because uq_dup_candidate_pair behaviour on flipped (A,B)/(B,A) rows is
exactly what the upsert must tolerate.
"""

import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from includes.dashboard.models import Supplier, SupplierDuplicateCandidate
from scripts.scan_supplier_duplicates import PairInfo, score_pair


class TestScorePair:
    def test_identical_normalised_name_is_certain(self):
        info = PairInfo(name_sim=1.0, name_keys_equal=True)
        confidence, reasons, tier = score_pair(info)
        assert confidence == 0.98
        assert "normalised_name_identical" in reasons
        assert tier == "certain"

    def test_word_swapped_names_not_certain(self):
        # 'signs safety' vs 'safety signs': trigram sets are identical
        # (sim 1.0) but the keys are not — must not be 'certain'.
        info = PairInfo(name_sim=1.0, name_keys_equal=False)
        confidence, reasons, tier = score_pair(info)
        assert confidence == 0.9
        assert tier == "review"

    def test_shared_domain_with_high_sim_is_certain(self):
        info = PairInfo(name_sim=0.85, shared_domains={"acmeparts"})
        confidence, reasons, tier = score_pair(info)
        assert confidence >= 0.85
        assert "shared_domain" in reasons
        assert tier == "certain"

    def test_shared_domain_only_is_review(self):
        info = PairInfo(name_sim=None, shared_domains={"acmeparts"})
        confidence, reasons, tier = score_pair(info)
        assert confidence == 0.7
        assert "shared_domain_only" in reasons
        assert tier == "review"

    def test_sim_only_is_review(self):
        info = PairInfo(name_sim=0.66)
        confidence, reasons, tier = score_pair(info)
        assert confidence == 0.66
        assert tier == "review"

    def test_country_mismatch_caps_confidence(self):
        info = PairInfo(name_sim=1.0, country_a="AU", country_b="US")
        confidence, reasons, tier = score_pair(info)
        assert confidence == 0.55
        assert "country_mismatch" in reasons

    def test_country_match_does_not_cap(self):
        info = PairInfo(name_sim=0.9, country_a="AU", country_b="AU")
        confidence, _reasons, tier = score_pair(info)
        assert confidence == 0.9
        assert tier == "review"  # certain needs shared domain (or identical name)

    def test_identical_name_with_disagreeing_domains_is_review(self):
        info = PairInfo(name_keys_equal=True, domains_a={"signsofsafety"}, domains_b={"eurosigns"})
        confidence, reasons, tier = score_pair(info)
        assert confidence == 0.5
        assert "domain_disagreement" in reasons
        assert tier == "review"

    def test_similar_name_with_disagreeing_domains_is_capped(self):
        info = PairInfo(name_sim=0.9, domains_a={"yalematerials"}, domains_b={"materialshandling"})
        confidence, reasons, tier = score_pair(info)
        assert confidence == 0.5
        assert "domain_disagreement" in reasons
        assert tier == "review"

    def test_currency_mismatch_is_review(self):
        info = PairInfo(name_keys_equal=True, currency_a={"gbp"}, currency_b={"eur"})
        confidence, reasons, tier = score_pair(info)
        assert confidence == 0.5
        assert "currency_mismatch" in reasons
        assert tier == "review"

    def test_same_currency_does_not_cap(self):
        info = PairInfo(name_keys_equal=True, currency_a={"usd"}, currency_b={"usd"})
        confidence, reasons, tier = score_pair(info)
        assert confidence == 0.98
        assert "currency_mismatch" not in reasons
        assert tier == "certain"

    def test_identical_name_with_shared_domain_is_certain(self):
        info = PairInfo(name_keys_equal=True, shared_domains={"acmeparts"},
                        domains_a={"acmeparts"}, domains_b={"acmeparts"})
        confidence, reasons, tier = score_pair(info)
        assert confidence == 0.98
        assert "domain_disagreement" not in reasons
        assert tier == "certain"

    def test_candidate_tier_domain_disagreement_is_review(self):
        from scripts.scan_supplier_duplicates import candidate_tier
        assert candidate_tier(0.6, ["normalised_name_identical", "domain_disagreement"]) == "review"
        assert candidate_tier(0.98, ["normalised_name_identical"]) == "certain"


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

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess, trans):
        if trans.nested and not trans._parent.nested:
            sess.begin_nested()

    session.close = lambda: None
    yield session
    transaction.rollback()
    connection.close()


def _supplier(session, name, netsuite=True):
    sup = Supplier(
        name=name,
        netsuite_id=f"NS-{uuid.uuid4().hex[:8]}" if netsuite else None,
        source="netsuite" if netsuite else "web",
    )
    session.add(sup)
    session.flush()
    return sup


def _scan_pairs_including(db_session, extra_pairs):
    """Build a pairs dict like a full scan: one entry per unordered pair
    already in the queue (so the stale-cleanup pass leaves pre-existing
    rows alone), plus the test's own pairs."""
    pairs = {}
    seen = set()
    for r in db_session.query(SupplierDuplicateCandidate).all():
        if r.primary_id is None or r.duplicate_id is None:
            continue
        key = frozenset((r.primary_id, r.duplicate_id))
        if key in seen:
            continue
        seen.add(key)
        pairs[(r.primary_id, r.duplicate_id)] = PairInfo(name_keys_equal=True)
    pairs.update(extra_pairs)
    return pairs


class TestUpsertCandidates:
    """Regression: uq_dup_candidate_pair is on the ORDERED pair, so flipped
    rows (A,B) and (B,A) can coexist for one supplier pair. The upsert must
    collapse them instead of UPDATing one into the other's unique key."""

    def _pair_rows(self, db_session, a, b):
        return db_session.query(SupplierDuplicateCandidate).filter(
            SupplierDuplicateCandidate.primary_id.in_([a.id, b.id]),
            SupplierDuplicateCandidate.duplicate_id.in_([a.id, b.id]),
        ).all()

    def test_flipped_duplicate_rows_do_not_crash(self, db_session):
        from scripts.scan_supplier_duplicates import _upsert_candidates
        a = _supplier(db_session, "Alpha Pty Ltd")
        b = _supplier(db_session, "Beta Pty Ltd")

        # The exact prod failure: both orientations present, both proposed.
        twin = SupplierDuplicateCandidate(
            primary_id=b.id, duplicate_id=a.id, source="auto",
            status="proposed", confidence=0.8,
        )
        db_session.add_all([
            SupplierDuplicateCandidate(
                primary_id=a.id, duplicate_id=b.id, source="auto",
                status="proposed", confidence=0.8,
            ),
            twin,
        ])
        db_session.flush()

        pairs = _scan_pairs_including(
            db_session, {(a.id, b.id): PairInfo(name_keys_equal=True)}
        )
        _upsert_candidates(db_session, pairs, 0.75)
        db_session.flush()

        rows = self._pair_rows(db_session, a, b)
        assert len(rows) == 1
        # Equal NetSuite sides tie-break toward (a, b).
        assert (rows[0].primary_id, rows[0].duplicate_id) == (a.id, b.id)
        assert rows[0].status == "proposed"
        assert rows[0].confidence == 0.98
        assert twin.id not in {r.id for r in rows}

    def test_keeps_row_already_oriented_like_fresh_pick(self, db_session):
        from scripts.scan_supplier_duplicates import _upsert_candidates
        a = _supplier(db_session, "Web Co", netsuite=False)
        b = _supplier(db_session, "NS Co")

        db_session.add_all([
            SupplierDuplicateCandidate(
                primary_id=a.id, duplicate_id=b.id, source="auto",
                status="proposed", confidence=0.8,
            ),
            SupplierDuplicateCandidate(
                primary_id=b.id, duplicate_id=a.id, source="auto",
                status="proposed", confidence=0.8,
            ),
        ])
        db_session.flush()

        pairs = _scan_pairs_including(
            db_session, {(a.id, b.id): PairInfo(name_keys_equal=True)}
        )
        _upsert_candidates(db_session, pairs, 0.75)
        db_session.flush()

        rows = self._pair_rows(db_session, a, b)
        assert len(rows) == 1
        # NetSuite side always wins the primary slot → (b, a) survives.
        assert (rows[0].primary_id, rows[0].duplicate_id) == (b.id, a.id)

    def test_decided_row_survives_stray_proposed_twin(self, db_session):
        from scripts.scan_supplier_duplicates import _upsert_candidates
        a = _supplier(db_session, "Alpha Pty Ltd")
        b = _supplier(db_session, "Beta Pty Ltd")

        decided = SupplierDuplicateCandidate(
            primary_id=a.id, duplicate_id=b.id, source="auto",
            status="merged", confidence=0.8,
        )
        stray = SupplierDuplicateCandidate(
            primary_id=b.id, duplicate_id=a.id, source="auto",
            status="proposed", confidence=0.8,
        )
        db_session.add_all([decided, stray])
        db_session.flush()

        pairs = _scan_pairs_including(
            db_session, {(a.id, b.id): PairInfo(name_keys_equal=True)}
        )
        _upsert_candidates(db_session, pairs, 0.75)
        db_session.flush()

        rows = self._pair_rows(db_session, a, b)
        assert len(rows) == 1
        assert rows[0].id == decided.id
        assert rows[0].status == "merged"
        assert (rows[0].primary_id, rows[0].duplicate_id) == (a.id, b.id)

    def test_below_confidence_cleans_all_proposed_twins(self, db_session):
        from scripts.scan_supplier_duplicates import _upsert_candidates
        a = _supplier(db_session, "Alpha Pty Ltd")
        b = _supplier(db_session, "Beta Pty Ltd")

        db_session.add_all([
            SupplierDuplicateCandidate(
                primary_id=a.id, duplicate_id=b.id, source="auto",
                status="proposed", confidence=0.8,
            ),
            SupplierDuplicateCandidate(
                primary_id=b.id, duplicate_id=a.id, source="auto",
                status="proposed", confidence=0.8,
            ),
        ])
        db_session.flush()

        pairs = _scan_pairs_including(
            db_session, {(a.id, b.id): PairInfo(name_sim=0.6)}  # below 0.75 floor
        )
        _upsert_candidates(db_session, pairs, 0.75)
        db_session.flush()

        assert self._pair_rows(db_session, a, b) == []
