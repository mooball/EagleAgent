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
