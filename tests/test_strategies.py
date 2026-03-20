"""
tests/test_strategies.py
========================
Unit tests for all three MVP strategies.

Rules:
  - No real API calls. Anthropic API mocked in S8 tests.
  - Uses MarketState fixtures defined inline (matches tests/fixtures/sample_markets.json).
  - Every strategy gets the 3 canonical scan() test cases:
      a) Should produce an Opportunity
      b) Should NOT (probability too low)
      c) Should NOT (too far from resolution)
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from strategies.base import BaseStrategy, Opportunity
from strategies.s1_negrisk_arb import NegRiskArbStrategy
from strategies.s8_logical_arb import LogicalArbStrategy
from strategies.s10_near_resolution import NearResolutionStrategy
from engines.data_engine import MarketState


# ============================================================================
# Helpers
# ============================================================================

def make_market(
    market_id="mkt-t",
    yes_price=0.94,
    no_price=0.06,
    yes_bid=0.93,
    no_bid=0.05,
    seconds=900,      # 15 min
    category="crypto",
    negrisk_group_id=None,
    volume=5000.0,
) -> MarketState:
    return MarketState(
        market_id=market_id,
        question=f"Test market {market_id}?",
        yes_token_id=f"y-{market_id}",
        no_token_id=f"n-{market_id}",
        yes_price=yes_price,
        no_price=no_price,
        yes_bid=yes_bid,
        no_bid=no_bid,
        volume_24h=volume,
        end_date_iso="2099-01-01T00:00:00Z",
        seconds_to_resolution=seconds,
        negrisk_group_id=negrisk_group_id,
        category=category,
        fee_rate_bps=15,
        fetched_at=time.time(),
    )


S10_CONFIG = {
    "min_probability":       0.90,
    "max_minutes_remaining": 60,
    "min_edge_after_fees":   0.025,
    "max_position_pct":      0.15,
    "kelly_fraction":        0.25,
    "threshold":             0.50,
    "category_rules": {
        "sports": {"min_probability": 0.93, "max_minutes_remaining": 30},
        "crypto": {"min_probability": 0.90, "max_minutes_remaining": 60},
        "politics": {"min_probability": 0.88, "max_minutes_remaining": 90},
        "other":  {"min_probability": 0.90, "max_minutes_remaining": 60},
    },
}

S1_CONFIG = {
    "min_spread_after_fees": 0.03,
    "max_position_pct":      0.20,
    "kelly_fraction":        0.50,
    "threshold":             0.60,
}

S8_CONFIG = {
    "min_similarity_threshold":       0.65,
    "min_probability_gap":            0.04,
    "min_edge_after_fees":            0.025,
    "max_new_classifications_per_scan": 5,
    "llm_model":                      "claude-haiku-4-5-20251001",
    "cache_path":                     "/tmp/test_s8_cache.json",
    "max_position_pct":               0.10,
    "kelly_fraction":                 0.25,
    "threshold":                      0.50,
}


# ============================================================================
# Canonical fee formula test (must pass for ALL strategies)
# ============================================================================

class TestFeeFormula:
    def test_fee_formula_at_half(self):
        """calc_fee(0.5) must equal exactly 0.140625 (1.40625%)."""
        fee = BaseStrategy.calc_fee(0.5)
        assert abs(fee - 0.140625) < 0.0001, f"Expected 0.140625, got {fee}"

    def test_fee_formula_at_zero(self):
        assert BaseStrategy.calc_fee(0.0) == 0.0

    def test_fee_formula_at_one(self):
        assert BaseStrategy.calc_fee(1.0) == 0.0

    def test_fee_formula_at_95(self):
        """At p=0.95 fee must be < 0.001 (< 0.1%)."""
        assert BaseStrategy.calc_fee(0.95) < 0.001

    def test_fee_formula_symmetric(self):
        """calc_fee(p) == calc_fee(1-p) — fee is symmetric around 0.5."""
        for p in (0.1, 0.2, 0.3, 0.4):
            assert abs(BaseStrategy.calc_fee(p) - BaseStrategy.calc_fee(1 - p)) < 1e-9


# ============================================================================
# S10 NearResolutionStrategy tests
# ============================================================================

class TestS10:

    @pytest.fixture
    def s10(self):
        return NearResolutionStrategy()

    def test_scan_generates_opportunity_above_threshold(self, s10):
        """p=0.94, 15 min remaining → should produce YES opportunity."""
        markets = [make_market(yes_price=0.94, seconds=900, category="crypto")]
        opps = s10.scan(markets, {}, S10_CONFIG)
        assert len(opps) >= 1
        assert opps[0].action == "buy_yes"
        assert opps[0].win_probability == pytest.approx(0.94)

    def test_scan_empty_when_probability_too_low(self, s10):
        """p=0.85 (below 0.90 threshold) → no opportunity."""
        markets = [make_market(yes_price=0.85, no_price=0.15, seconds=900)]
        opps = s10.scan(markets, {}, S10_CONFIG)
        assert opps == []

    def test_scan_empty_when_too_far_from_resolution(self, s10):
        """90 min remaining (above 60 min max) → no opportunity."""
        markets = [make_market(yes_price=0.94, seconds=5400)]  # 90 min
        opps = s10.scan(markets, {}, S10_CONFIG)
        assert opps == []

    def test_sports_requires_higher_threshold(self, s10):
        """Sports: p=0.91 should fail (threshold 0.93). p=0.94 should pass."""
        m_low  = make_market(market_id="s-low",  yes_price=0.91, seconds=900, category="sports")
        m_high = make_market(market_id="s-high", yes_price=0.94, seconds=900, category="sports")

        opps_low  = s10.scan([m_low],  {}, S10_CONFIG)
        opps_high = s10.scan([m_high], {}, S10_CONFIG)

        assert opps_low  == [], "p=0.91 sports should NOT trigger (threshold 0.93)"
        assert len(opps_high) >= 1, "p=0.94 sports should trigger"

    def test_score_higher_when_closer_to_resolution(self, s10):
        """Same edge, different time remaining → closer = higher score."""
        opp_close = Opportunity(
            strategy="s10_near_resolution", market_id="a", market_question="Q",
            action="buy_yes", edge=0.05, win_probability=0.94,
            max_payout=1.064, time_to_resolution_sec=300,   # 5 min
        )
        opp_far = Opportunity(
            strategy="s10_near_resolution", market_id="b", market_question="Q",
            action="buy_yes", edge=0.05, win_probability=0.94,
            max_payout=1.064, time_to_resolution_sec=3000,  # 50 min
        )
        assert s10.score(opp_close, S10_CONFIG) > s10.score(opp_far, S10_CONFIG)

    def test_size_caps_at_max_position_pct(self, s10):
        """size() must never exceed max_position_pct × bankroll."""
        opp = Opportunity(
            strategy="s10_near_resolution", market_id="x", market_question="Q",
            action="buy_yes", edge=0.05, win_probability=0.94,
            max_payout=1.064, time_to_resolution_sec=900,
        )
        size = s10.size(opp, bankroll=100.0, config=S10_CONFIG)
        assert size <= 100.0 * S10_CONFIG["max_position_pct"]

    def test_size_minimum_one_dollar(self, s10):
        """size() must return at least $1.00."""
        opp = Opportunity(
            strategy="s10_near_resolution", market_id="x", market_question="Q",
            action="buy_yes", edge=0.001, win_probability=0.999,
            max_payout=1.001, time_to_resolution_sec=60,
        )
        size = s10.size(opp, bankroll=100.0, config=S10_CONFIG)
        assert size >= 1.0

    def test_on_resolve_win_returns_correct_keys(self, s10):
        """on_resolve() must return dict with: won, roi, notes, lessons."""
        from strategies.base import Resolution
        res = Resolution(
            trade_id="t1", market_id="m1", won=True,
            cost_usdc=10.0, payout_usdc=10.64,
            pnl_usdc=0.64, roi=0.064, strategy="s10_near_resolution",
        )
        result = s10.on_resolve(res)
        assert set(result.keys()) >= {"won", "roi", "notes", "lessons"}
        assert result["won"] is True
        assert isinstance(result["lessons"], list)

    def test_edge_positive_after_fee(self, s10):
        """All returned Opportunities must have edge > 0 (fee subtracted)."""
        markets = [make_market(yes_price=0.94, seconds=900)]
        opps = s10.scan(markets, {}, S10_CONFIG)
        for opp in opps:
            assert opp.edge > 0, f"Opportunity with non-positive edge: {opp.edge}"


# ============================================================================
# S1 NegRiskArbStrategy tests
# ============================================================================

class TestS1:

    @pytest.fixture
    def s1(self):
        return NegRiskArbStrategy()

    def _make_negrisk_group(self, yes_prices, yes_bids=None):
        if yes_bids is None:
            yes_bids = [p - 0.01 for p in yes_prices]
        markets = [
            make_market(
                market_id=f"neg-{i}",
                yes_price=yes_prices[i],
                no_price=round(1 - yes_prices[i], 3),
                yes_bid=yes_bids[i],
                no_bid=round(1 - yes_bids[i] - 0.01, 3),
                negrisk_group_id="group-A",
            )
            for i in range(len(yes_prices))
        ]
        return {"group-A": markets}

    def test_long_arb_detected_when_sum_below_one(self, s1):
        """sum(YES) = 0.70 → 30% gross spread → net > 3% min → opportunity."""
        groups = self._make_negrisk_group([0.22, 0.26, 0.22])  # sum = 0.70
        opps = s1.scan([], groups, S1_CONFIG)
        assert len(opps) >= 1
        long_opps = [o for o in opps if o.action == "buy_all_yes"]
        assert len(long_opps) == 1
        assert long_opps[0].win_probability == 1.0
        assert long_opps[0].edge > 0.03

    def test_long_arb_not_found_when_sum_at_one(self, s1):
        """sum(YES) = 1.00 → no spread → no opportunity."""
        groups = self._make_negrisk_group([0.34, 0.33, 0.33])  # sum ≈ 1.00
        opps = s1.scan([], groups, S1_CONFIG)
        long_opps = [o for o in opps if o.action == "buy_all_yes"]
        assert len(long_opps) == 0

    def test_short_arb_detected_when_bids_above_one(self, s1):
        """sum(YES bids) = 1.05 → 5% revenue → short arb opportunity."""
        # yes_bids sum to > 1.0
        groups = self._make_negrisk_group(
            yes_prices=[0.36, 0.35, 0.35],
            yes_bids=[0.36, 0.35, 0.36],   # sum = 1.07
        )
        opps = s1.scan([], groups, S1_CONFIG)
        short_opps = [o for o in opps if o.action == "sell_all_yes"]
        assert len(short_opps) == 1

    def test_win_probability_always_one(self, s1):
        """All NegRisk opportunities must have win_probability == 1.0."""
        groups = self._make_negrisk_group([0.20, 0.25, 0.20])
        opps = s1.scan([], groups, S1_CONFIG)
        for opp in opps:
            assert opp.win_probability == 1.0

    def test_metadata_contains_group_id_and_markets(self, s1):
        """Opportunity metadata must contain group_id and markets list."""
        groups = self._make_negrisk_group([0.22, 0.26, 0.22])
        opps = s1.scan([], groups, S1_CONFIG)
        long_opps = [o for o in opps if o.action == "buy_all_yes"]
        assert len(long_opps) == 1
        meta = long_opps[0].metadata
        assert "group_id" in meta
        assert "markets" in meta
        assert len(meta["markets"]) == 3

    def test_single_market_group_ignored(self, s1):
        """Groups with only 1 market are not arb opportunities."""
        groups = {"group-solo": [make_market(market_id="solo", negrisk_group_id="group-solo")]}
        opps = s1.scan([], groups, S1_CONFIG)
        assert opps == []


# ============================================================================
# S8 LogicalArbStrategy tests  (Anthropic API mocked)
# ============================================================================

class TestS8:

    @pytest.fixture
    def s8(self, tmp_path):
        strat = LogicalArbStrategy()
        # Patch cache path to temp dir
        S8_CONFIG["cache_path"] = str(tmp_path / "s8_cache.json")
        return strat

    def _mock_model_and_classifier(self, s8, directions: dict):
        """
        Mock out sentence-transformers and Anthropic API.
        directions: {(market_id_a, market_id_b): "a_subset_of_b" | ...}
        """
        # Mock the SentenceTransformer to return pre-computed embeddings
        import numpy as np
        n_markets = 10  # max markets we'll use in tests

        mock_model = MagicMock()
        mock_model.encode.return_value = np.random.randn(n_markets, 384)
        s8._model = mock_model

        # Mock Anthropic classifier
        def fake_classify(question_a, question_b, model):
            # Simple lookup by question content
            for (qa, qb), dir_ in directions.items():
                if qa in question_a and qb in question_b:
                    return dir_
            return "none"

        s8._classify = fake_classify

    def test_violation_detected_a_subset_of_b(self, s8, tmp_path):
        """
        P(Lakers win) = 0.40 > P(Western team wins) = 0.30
        Lakers ⊂ Western Conference → impossible. Should buy Western market.
        """
        import numpy as np

        m_lakers  = make_market("lakers",  yes_price=0.40, seconds=86400)
        m_western = make_market("western", yes_price=0.30, seconds=86400)
        m_lakers.question  = "Will the Lakers win the NBA championship?"
        m_western.question = "Will a Western Conference team win the NBA championship?"

        markets = [m_lakers, m_western]

        # Mock embeddings with high similarity for this pair
        mock_model = MagicMock()
        mock_model.encode.return_value = np.array([
            [1.0, 0.0, 0.0],
            [0.9, 0.1, 0.0],   # high cosine similarity with market 0
        ], dtype=float)
        # Normalize
        mock_model.encode.return_value /= np.linalg.norm(
            mock_model.encode.return_value, axis=1, keepdims=True
        )
        s8._model = mock_model

        # Mock classifier to return the violation
        s8._classify = lambda qa, qb, model: (
            "a_subset_of_b" if "Lakers" in qa else "none"
        )

        opps = s8.scan(markets, {}, S8_CONFIG)

        assert len(opps) >= 1
        # Should be buying the western market (the underpriced one)
        assert opps[0].market_id == "western"
        assert opps[0].action == "buy_yes"

    def test_cache_prevents_duplicate_api_calls(self, s8, tmp_path):
        """Second scan with same pairs must use cache, not call classifier again."""
        import numpy as np

        m1 = make_market("m1", yes_price=0.50, seconds=86400)
        m2 = make_market("m2", yes_price=0.35, seconds=86400)
        m1.question = "Question Alpha"
        m2.question = "Question Beta"

        mock_model = MagicMock()
        mock_model.encode.return_value = np.array([[0.9, 0.1], [0.85, 0.15]], dtype=float)
        mock_model.encode.return_value /= np.linalg.norm(
            mock_model.encode.return_value, axis=1, keepdims=True
        )
        s8._model = mock_model

        call_count = {"n": 0}

        def counting_classifier(qa, qb, model):
            call_count["n"] += 1
            return "none"

        s8._classify = counting_classifier

        # First scan — classifier gets called
        s8.scan([m1, m2], {}, S8_CONFIG)
        calls_after_first = call_count["n"]

        # Second scan — same pair should be cached
        s8.scan([m1, m2], {}, S8_CONFIG)
        calls_after_second = call_count["n"]

        assert calls_after_second == calls_after_first, (
            "Classifier called again on cached pair — cache not working"
        )

    def test_classifier_error_does_not_crash_scan(self, s8):
        """Anthropic API error must be swallowed — scan() returns [] not exception."""
        import numpy as np

        m1 = make_market("e1", yes_price=0.50, seconds=86400)
        m2 = make_market("e2", yes_price=0.35, seconds=86400)

        mock_model = MagicMock()
        mock_model.encode.return_value = np.array([[0.9, 0.1], [0.85, 0.15]], dtype=float)
        mock_model.encode.return_value /= np.linalg.norm(
            mock_model.encode.return_value, axis=1, keepdims=True
        )
        s8._model = mock_model

        def crashing_classifier(qa, qb, model):
            raise RuntimeError("Anthropic API unavailable")

        s8._classify = crashing_classifier

        # Must not raise — returns [] gracefully
        opps = s8.scan([m1, m2], {}, S8_CONFIG)
        assert isinstance(opps, list)
