"""Tests for scripts/scan_supplier_duplicates.py — pair scoring (pure)."""

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
