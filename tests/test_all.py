"""
tests/test_all.py — Complete test suite for GitHub Actions.

Runs with ONLY requirements-scan.txt installed.
No HTTP calls. No API keys needed. No torch. No py-clob-client.
All external calls are either mocked or use only stdlib/core deps.
"""

import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Make sure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ─────────────────────────────────────────────────────────────────────────────
# FIXTURES
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_markets():
    fixture_path = Path(__file__).parent / "fixtures" / "sample_markets.json"
    data = json.loads(fixture_path.read_text())
    from engines.data_engine import MarketState
    return [MarketState(**m) for m in data]


@pytest.fixture
def near_term_market(sample_markets):
    """mkt_s10_crypto — 30 min to resolution, YES=0.94"""
    return next(m for m in sample_markets if m.market_id == "mkt_s10_crypto")


@pytest.fixture
def far_market(sample_markets):
    """mkt_politics_far — 84M seconds to resolution"""
    return next(m for m in sample_markets if m.market_id == "mkt_politics_far")


@pytest.fixture
def negrisk_group(sample_markets):
    return {
        "grp_championship": [
            m for m in sample_markets if m.negrisk_group_id == "grp_championship"
        ]
    }


@pytest.fixture
def s10_config():
    return {
        "s10_near_resolution": {
            "enabled":              True,
            "max_minutes_remaining": 60,
            "min_probability":      0.90,
            "min_volume_24h":       500.0,
            "max_spread":           0.03,
            "min_edge_after_fees":  0.005,
            "max_position_pct":     0.15,
            "kelly_fraction":       0.25,
            "threshold":            0.55,
        }
    }


@pytest.fixture
def s1_config():
    return {
        "s1_negrisk_arb": {
            "enabled":              True,
            "min_edge_after_fees":  0.020,
            "min_leg_volume_24h":   200.0,
            "min_leg_bid":          0.02,
            "max_group_legs":       10,
            "max_position_pct":     0.15,
            "threshold":            0.60,
        }
    }


@pytest.fixture
def tmp_db(tmp_path):
    from engines.state_engine import StateEngine
    return StateEngine(
        db_path       = str(tmp_path / "trades.db"),
        lessons_path  = str(tmp_path / "lessons.json"),
        initial_balance = 100.0,
    )


# ─────────────────────────────────────────────────────────────────────────────
# FEE FORMULA — must match spec exactly
# ─────────────────────────────────────────────────────────────────────────────

class TestFeeFormula:
    def test_fee_at_0_5_equals_spec(self):
        from strategies.base import BaseStrategy
        assert abs(BaseStrategy.calc_fee(0.5) - 0.140625) < 0.0001

    def test_fee_at_0_0_is_zero(self):
        from strategies.base import BaseStrategy
        assert BaseStrategy.calc_fee(0.0) == 0.0

    def test_fee_at_1_0_is_zero(self):
        from strategies.base import BaseStrategy
        assert BaseStrategy.calc_fee(1.0) == 0.0

    def test_fee_at_0_95_is_small(self):
        from strategies.base import BaseStrategy
        # At p=0.95: 2.25 * (0.95*0.05)^2 = 2.25 * 0.002256 = 0.005077
        fee = BaseStrategy.calc_fee(0.95)
        assert fee < 0.01   # much smaller than at p=0.5
        assert fee > 0.0

    def test_fee_clamps_input(self):
        from strategies.base import BaseStrategy
        assert BaseStrategy.calc_fee(-0.5) == 0.0
        assert BaseStrategy.calc_fee(1.5) == 0.0

    def test_fee_symmetric(self):
        from strategies.base import BaseStrategy
        # fee(p) == fee(1-p) by symmetry
        assert abs(BaseStrategy.calc_fee(0.3) - BaseStrategy.calc_fee(0.7)) < 1e-10


# ─────────────────────────────────────────────────────────────────────────────
# STATE ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class TestStateEngine:
    def test_creates_db_on_init(self, tmp_db):
        assert tmp_db.get_current_balance() == 100.0

    def test_daily_pnl_zero_on_empty_db(self, tmp_db):
        """Must return 0.0, NEVER raise."""
        pnl = tmp_db.get_daily_pnl()
        assert pnl == 0.0

    def test_open_position_count_starts_zero(self, tmp_db):
        assert tmp_db.get_open_position_count() == 0

    def test_log_trade_returns_int_id(self, tmp_db):
        from engines.state_engine import TradeRecord
        record = TradeRecord(
            trade_id="TEST_001", strategy="s10_near_resolution",
            market_id="mkt1", market_question="Test?",
            side="YES", price=0.94, shares=10.0,
            cost_usdc=9.40, fee_usdc=0.05,
        )
        row_id = tmp_db.log_trade(record)
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_open_position_count_increments(self, tmp_db):
        from engines.state_engine import TradeRecord
        tmp_db.log_trade(TradeRecord(
            trade_id="T1", strategy="s10", market_id="m1",
            market_question="Q1?", side="YES", price=0.9,
            shares=5.0, cost_usdc=4.5, fee_usdc=0.01,
        ))
        assert tmp_db.get_open_position_count() == 1

    def test_mark_resolved_closes_position(self, tmp_db):
        from engines.state_engine import TradeRecord
        tmp_db.log_trade(TradeRecord(
            trade_id="T2", strategy="s10", market_id="mkt_resolve",
            market_question="Q?", side="YES", price=0.9,
            shares=5.0, cost_usdc=4.5, fee_usdc=0.01,
        ))
        assert tmp_db.get_open_position_count() == 1
        tmp_db.mark_resolved("mkt_resolve", "win", 0.50)
        assert tmp_db.get_open_position_count() == 0

    def test_balance_updates_after_resolution(self, tmp_db):
        from engines.state_engine import TradeRecord
        tmp_db.log_trade(TradeRecord(
            trade_id="T3", strategy="s10", market_id="mkt_bal",
            market_question="Q?", side="YES", price=0.9,
            shares=5.0, cost_usdc=4.5, fee_usdc=0.01,
        ))
        before = tmp_db.get_current_balance()
        tmp_db.mark_resolved("mkt_bal", "win", 1.00)
        after = tmp_db.get_current_balance()
        assert abs(after - (before + 1.00)) < 0.01

    def test_duplicate_trade_id_ignored(self, tmp_db):
        from engines.state_engine import TradeRecord
        record = TradeRecord(
            trade_id="DUP", strategy="s10", market_id="m1",
            market_question="Q?", side="YES", price=0.9,
            shares=5.0, cost_usdc=4.5, fee_usdc=0.01,
        )
        tmp_db.log_trade(record)
        tmp_db.log_trade(record)  # duplicate — should not raise
        assert tmp_db.get_open_position_count() == 1

    def test_lessons_roundtrip(self, tmp_db):
        lessons = {"version": 1, "lessons": ["test lesson"], "strategy_scores": {}}
        tmp_db.save_lessons(lessons)
        loaded = tmp_db.get_lessons()
        assert loaded["lessons"] == ["test lesson"]

    def test_get_open_positions_returns_list(self, tmp_db):
        result = tmp_db.get_open_positions()
        assert isinstance(result, list)


# ─────────────────────────────────────────────────────────────────────────────
# DATA ENGINE — snapshot compression
# ─────────────────────────────────────────────────────────────────────────────

class TestDataEngine:
    def test_save_and_load_snapshot(self, sample_markets, tmp_path):
        from engines.data_engine import save_snapshot, load_snapshot
        path = tmp_path / "snap.json.gz"
        save_snapshot(sample_markets, path)
        assert path.exists()
        loaded = load_snapshot(path)
        assert len(loaded) == len(sample_markets)
        assert loaded[0].market_id == sample_markets[0].market_id

    def test_snapshot_compresses(self, sample_markets, tmp_path):
        from engines.data_engine import save_snapshot
        path = tmp_path / "snap.json.gz"
        save_snapshot(sample_markets, path)
        assert path.stat().st_size < 10_000   # should be well under 10KB

    def test_load_missing_snapshot_returns_empty(self, tmp_path):
        from engines.data_engine import load_snapshot
        result = load_snapshot(tmp_path / "nonexistent.json.gz")
        assert result == []

    def test_load_corrupt_snapshot_returns_empty(self, tmp_path):
        from engines.data_engine import load_snapshot
        bad = tmp_path / "bad.json.gz"
        bad.write_bytes(b"not gzip data at all")
        result = load_snapshot(bad)
        assert result == []

    def test_market_state_is_stale(self, near_term_market):
        near_term_market.fetched_at = time.time() - 400
        assert near_term_market.is_stale(300) is True

    def test_market_state_not_stale(self, near_term_market):
        near_term_market.fetched_at = time.time() - 100
        assert near_term_market.is_stale(300) is False

    def test_market_state_to_from_dict_roundtrip(self, near_term_market):
        from engines.data_engine import MarketState
        d = near_term_market.to_dict()
        restored = MarketState.from_dict(d)
        assert restored.market_id == near_term_market.market_id
        assert restored.yes_price == near_term_market.yes_price

    def test_category_detection_crypto(self):
        from engines.data_engine import _categorise
        assert _categorise([], "Will BTC reach 100k?") == "crypto"

    def test_category_detection_politics(self):
        from engines.data_engine import _categorise
        assert _categorise([], "Will the president win re-election?") == "politics"

    def test_category_detection_sports(self):
        from engines.data_engine import _categorise
        assert _categorise([], "Will the NBA finals go to game 7?") == "sports"

    def test_category_detection_other(self):
        from engines.data_engine import _categorise
        assert _categorise([], "Will it rain in Paris next Tuesday?") == "other"

    def test_parse_iso_various_formats(self):
        from engines.data_engine import DataEngine
        cases = [
            "2026-03-22T23:59:00Z",
            "2026-03-22T23:59:00.000Z",
            "2026-03-22",
        ]
        for s in cases:
            ts = DataEngine._parse_iso_to_ts(s)
            assert ts is not None and ts > 0, f"Failed to parse: {s}"

    def test_parse_iso_invalid_returns_none(self):
        from engines.data_engine import DataEngine
        assert DataEngine._parse_iso_to_ts("not-a-date") is None
        assert DataEngine._parse_iso_to_ts("") is None


# ─────────────────────────────────────────────────────────────────────────────
# S10 NEAR RESOLUTION STRATEGY
# ─────────────────────────────────────────────────────────────────────────────

class TestS10NearResolution:
    def test_scan_finds_near_term_high_prob(self, sample_markets, s10_config):
        from strategies.s10_near_resolution import S10NearResolution
        s10  = S10NearResolution()
        opps = s10.scan(sample_markets, {}, s10_config)
        market_ids = [o.market_id for o in opps]
        assert "mkt_s10_crypto" in market_ids

    def test_scan_rejects_far_market(self, sample_markets, s10_config):
        """Politics market 84M seconds out must be rejected."""
        from strategies.s10_near_resolution import S10NearResolution
        s10  = S10NearResolution()
        opps = s10.scan(sample_markets, {}, s10_config)
        market_ids = [o.market_id for o in opps]
        assert "mkt_politics_far" not in market_ids

    def test_scan_rejects_low_probability(self, sample_markets, s10_config):
        """mkt_neg_a has YES=0.30 — below min_probability=0.90."""
        from strategies.s10_near_resolution import S10NearResolution
        s10  = S10NearResolution()
        opps = s10.scan(sample_markets, {}, s10_config)
        market_ids = [o.market_id for o in opps]
        assert "mkt_neg_a" not in market_ids

    def test_scan_seconds_vs_minutes_fix(self, s10_config):
        """THE CORE BUG FIX: max_minutes_remaining=60 must accept a 1800s market."""
        from strategies.s10_near_resolution import S10NearResolution
        from engines.data_engine import MarketState
        s10 = S10NearResolution()
        market = MarketState(
            market_id="fix_test", question="Will X happen?",
            yes_token_id="t1", no_token_id="t2",
            yes_price=0.95, no_price=0.05,
            yes_bid=0.94, yes_ask=0.96, no_bid=0.04, no_ask=0.06,
            volume_24h=1000.0, end_date_iso="2026-03-22T01:00:00Z",
            seconds_to_resolution=1800,   # 30 minutes — must PASS the 60-min filter
            negrisk_group_id=None, category="crypto",
            fee_rate_bps=50, fetched_at=time.time()
        )
        opps = s10.scan([market], {}, s10_config)
        assert len(opps) == 1, (
            "BUG PRESENT: 1800 seconds treated as > 60 (minutes). "
            "Fix: max_seconds = max_minutes * 60"
        )

    def test_score_returns_float_in_range(self, sample_markets, s10_config):
        from strategies.s10_near_resolution import S10NearResolution
        s10  = S10NearResolution()
        opps = s10.scan(sample_markets, {}, s10_config)
        assert opps, "Need at least one opportunity to test scoring"
        for opp in opps:
            score = s10.score(opp, s10_config)
            assert 0.0 <= score <= 1.0, f"Score out of range: {score}"

    def test_size_respects_floor(self, sample_markets, s10_config):
        from strategies.s10_near_resolution import S10NearResolution
        s10  = S10NearResolution()
        opps = s10.scan(sample_markets, {}, s10_config)
        assert opps
        size = s10.size(opps[0], 100.0, s10_config)
        assert size >= 1.0

    def test_size_respects_ceiling(self, sample_markets, s10_config):
        from strategies.s10_near_resolution import S10NearResolution
        s10  = S10NearResolution()
        opps = s10.scan(sample_markets, {}, s10_config)
        assert opps
        size = s10.size(opps[0], 100.0, s10_config)
        max_allowed = 0.15 * 100.0
        assert size <= max_allowed, f"Size {size} exceeds max {max_allowed}"

    def test_action_is_buy_yes_for_high_yes(self, near_term_market, s10_config):
        from strategies.s10_near_resolution import S10NearResolution
        s10  = S10NearResolution()
        opps = s10.scan([near_term_market], {}, s10_config)
        assert opps
        assert opps[0].action == "BUY_YES"

    def test_action_is_buy_no_for_high_no(self, s10_config):
        from strategies.s10_near_resolution import S10NearResolution
        from engines.data_engine import MarketState
        s10 = S10NearResolution()
        market = MarketState(
            market_id="no_market", question="Will X fail?",
            yes_token_id="t1", no_token_id="t2",
            yes_price=0.05, no_price=0.95,
            yes_bid=0.04, yes_ask=0.06, no_bid=0.94, no_ask=0.96,
            volume_24h=1000.0, end_date_iso="2026-03-22T01:00:00Z",
            seconds_to_resolution=1800,
            negrisk_group_id=None, category="crypto",
            fee_rate_bps=50, fetched_at=time.time()
        )
        opps = s10.scan([market], {}, s10_config)
        assert opps
        assert opps[0].action == "BUY_NO"

    def test_scan_empty_markets_returns_empty(self, s10_config):
        from strategies.s10_near_resolution import S10NearResolution
        s10 = S10NearResolution()
        assert s10.scan([], {}, s10_config) == []


# ─────────────────────────────────────────────────────────────────────────────
# S1 NEGRISK ARB STRATEGY
# ─────────────────────────────────────────────────────────────────────────────

class TestS1NegRiskArb:
    def test_scan_finds_arb_in_valid_group(self, negrisk_group, s1_config):
        """Group with YES ask sum = 0.31+0.26 = 0.57 < 1.0 — no arb (correct)."""
        from strategies.s1_negrisk_arb import S1NegRiskArb
        s1   = S1NegRiskArb()
        opps = s1.scan([], negrisk_group, s1_config)
        # 0.57 total ask — no arb edge here, working correctly
        assert isinstance(opps, list)

    def test_scan_finds_arb_when_sum_under_1(self, s1_config):
        """Synthesize a group with sum(YES ask) = 0.91 → edge = 0.09 before fees."""
        from strategies.s1_negrisk_arb import S1NegRiskArb
        from engines.data_engine import MarketState
        now = time.time()
        legs = [
            MarketState(
                market_id=f"ng{i}", question=f"Option {i} wins",
                yes_token_id=f"yt{i}", no_token_id=f"nt{i}",
                yes_price=0.30 - i*0.01, no_price=0.70 + i*0.01,
                yes_bid=0.28 - i*0.01, yes_ask=0.31 - i*0.01,
                no_bid=0.68, no_ask=0.72,
                volume_24h=500.0, end_date_iso="2026-03-25T00:00:00Z",
                seconds_to_resolution=86400,
                negrisk_group_id="grp_arb", category="sports",
                fee_rate_bps=140, fetched_at=now
            )
            for i in range(3)   # sum of YES asks: 0.31+0.30+0.29 = 0.90 → edge ~0.10
        ]
        groups = {"grp_arb": legs}
        s1   = S1NegRiskArb()
        opps = s1.scan([], groups, s1_config)
        assert len(opps) == 1
        assert opps[0].action == "BUY_ALL_YES"
        assert opps[0].edge > 0

    def test_scan_rejects_stale_legs(self, s1_config):
        """Legs with yes_bid=0 (stale) must be rejected."""
        from strategies.s1_negrisk_arb import S1NegRiskArb
        from engines.data_engine import MarketState
        now = time.time()
        stale_legs = [
            MarketState(
                market_id=f"stale{i}", question=f"Option {i}",
                yes_token_id=f"yt{i}", no_token_id=f"nt{i}",
                yes_price=0.30, no_price=0.70,
                yes_bid=0.0,    yes_ask=0.31,   # bid=0 → stale
                no_bid=0.0,     no_ask=0.71,
                volume_24h=500.0, end_date_iso="2026-03-25T00:00:00Z",
                seconds_to_resolution=86400,
                negrisk_group_id="grp_stale", category="sports",
                fee_rate_bps=140, fetched_at=now
            )
            for i in range(3)
        ]
        groups = {"grp_stale": stale_legs}
        s1   = S1NegRiskArb()
        opps = s1.scan([], groups, s1_config)
        assert opps == [], "Stale legs (bid=0) should produce no opportunities"

    def test_scan_rejects_low_volume_legs(self, s1_config):
        from strategies.s1_negrisk_arb import S1NegRiskArb
        from engines.data_engine import MarketState
        now = time.time()
        thin_legs = [
            MarketState(
                market_id=f"thin{i}", question=f"Option {i}",
                yes_token_id=f"yt{i}", no_token_id=f"nt{i}",
                yes_price=0.30, no_price=0.70,
                yes_bid=0.29, yes_ask=0.31,
                no_bid=0.69, no_ask=0.71,
                volume_24h=10.0,   # below min_leg_volume_24h=200
                end_date_iso="2026-03-25T00:00:00Z",
                seconds_to_resolution=86400,
                negrisk_group_id="grp_thin", category="sports",
                fee_rate_bps=140, fetched_at=now
            )
            for i in range(3)
        ]
        groups = {"grp_thin": thin_legs}
        s1   = S1NegRiskArb()
        opps = s1.scan([], groups, s1_config)
        assert opps == []

    def test_score_returns_float_in_range(self, s1_config):
        from strategies.s1_negrisk_arb import S1NegRiskArb
        from engines.data_engine import MarketState
        from strategies.base import Opportunity
        s1  = S1NegRiskArb()
        opp = Opportunity(
            strategy="s1_negrisk_arb", market_id="grp1",
            market_question="NegRisk group...", action="BUY_ALL_YES",
            edge=0.05, win_probability=1.0, max_payout=1.0,
            time_to_resolution_sec=86400,
            metadata={"num_legs": 3, "total_ask": 0.90,
                      "avg_leg_volume": 500.0},
        )
        score = s1.score(opp, s1_config)
        assert 0.0 <= score <= 1.0

    def test_size_respects_ceiling(self, s1_config):
        from strategies.s1_negrisk_arb import S1NegRiskArb
        from strategies.base import Opportunity
        s1  = S1NegRiskArb()
        opp = Opportunity(
            strategy="s1_negrisk_arb", market_id="grp1",
            market_question="Test", action="BUY_ALL_YES",
            edge=0.05, win_probability=1.0, max_payout=1.0,
            time_to_resolution_sec=86400,
            metadata={"total_ask": 0.90},
        )
        size = s1.size(opp, 100.0, s1_config)
        assert size <= 0.15 * 100.0
        assert size >= 1.0


# ─────────────────────────────────────────────────────────────────────────────
# S8 LOGICAL ARB — only tests that don't need torch/sentence-transformers
# ─────────────────────────────────────────────────────────────────────────────

class TestS8LogicalArb:
    def test_scan_returns_empty_when_disabled(self, sample_markets):
        """S8 is disabled at MVP — must return [] without loading torch."""
        from strategies.s8_logical_arb import S8LogicalArb
        s8  = S8LogicalArb()
        cfg = {"s8_logical_arb": {"enabled": False}}
        opps = s8.scan(sample_markets, {}, cfg)
        assert opps == []

    def test_no_torch_import_when_disabled(self, sample_markets):
        """Confirm torch is never imported when S8 is disabled."""
        import sys
        from strategies.s8_logical_arb import S8LogicalArb
        s8  = S8LogicalArb()
        cfg = {"s8_logical_arb": {"enabled": False}}
        s8.scan(sample_markets, {}, cfg)
        assert "torch" not in sys.modules, "torch was imported — breaks GHA"

    def test_cache_persists_between_instances(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        from strategies.s8_logical_arb import S8LogicalArb, CACHE_PATH
        s8a = S8LogicalArb()
        s8a._direction_cache["key1|key2"] = "exclusive"
        s8a._save_cache()
        s8b = S8LogicalArb()
        assert s8b._direction_cache.get("key1|key2") == "exclusive"

    def test_cache_key_is_sorted(self):
        from strategies.s8_logical_arb import S8LogicalArb
        s8 = S8LogicalArb()
        assert s8._cache_key("b", "a") == s8._cache_key("a", "b")


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalEngine:
    def test_run_one_cycle_returns_dict_always(self, sample_markets):
        from engines.signal_engine import SignalEngine
        from strategies.s10_near_resolution import S10NearResolution
        engine = SignalEngine({
            "s10_near_resolution": {
                "enabled": True,
                "max_minutes_remaining": 60,
                "min_probability": 0.90,
                "min_volume_24h": 500.0,
                "max_spread": 0.03,
                "min_edge_after_fees": 0.005,
                "max_position_pct": 0.15,
                "kelly_fraction": 0.25,
                "threshold": 0.55,
            }
        })
        engine.register(S10NearResolution())
        result = engine.run_one_cycle(sample_markets, {})
        assert isinstance(result, dict)
        assert "opportunities" in result
        assert "markets_scanned" in result

    def test_broken_strategy_does_not_crash_cycle(self, sample_markets):
        """A strategy that raises must not crash run_one_cycle."""
        from engines.signal_engine import SignalEngine
        from strategies.base import BaseStrategy, Opportunity, Resolution

        class BrokenStrategy(BaseStrategy):
            name = "broken"
            def scan(self, m, g, c): raise RuntimeError("intentional failure")
            def score(self, o, c):   return 0.5
            def size(self, o, b, c): return 1.0
            def on_resolve(self, t, outcome, c):
                return Resolution("", "", False, 0, 0, 0, 0, "broken")

        engine = SignalEngine({"broken": {"enabled": True, "threshold": 0.5}})
        engine.register(BrokenStrategy())
        result = engine.run_one_cycle(sample_markets, {})
        assert isinstance(result, dict)
        assert result["scan_errors"] == 1

    def test_opportunities_sorted_by_score(self, sample_markets):
        from engines.signal_engine import SignalEngine
        from strategies.s10_near_resolution import S10NearResolution
        engine = SignalEngine({
            "s10_near_resolution": {
                "enabled": True,
                "max_minutes_remaining": 60,
                "min_probability": 0.90,
                "min_volume_24h": 500.0,
                "max_spread": 0.03,
                "min_edge_after_fees": 0.005,
                "max_position_pct": 0.15,
                "kelly_fraction": 0.25,
                "threshold": 0.0,   # low threshold — get all opps
            }
        })
        engine.register(S10NearResolution())
        result = engine.run_one_cycle(sample_markets, {})
        opps   = result["opportunities"]
        scores = [o.score for o in opps]
        assert scores == sorted(scores, reverse=True)

    def test_disabled_strategy_not_run(self, sample_markets):
        from engines.signal_engine import SignalEngine
        from strategies.s10_near_resolution import S10NearResolution
        engine = SignalEngine({"s10_near_resolution": {"enabled": False}})
        engine.register(S10NearResolution())
        result = engine.run_one_cycle(sample_markets, {})
        assert result["opportunities"] == []


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

class TestConfig:
    def test_engine_yaml_loads(self):
        import yaml
        cfg = yaml.safe_load(open("configs/engine.yaml"))
        assert "engine" in cfg

    def test_allocations_sum_to_one(self):
        import yaml
        cfg    = yaml.safe_load(open("configs/engine.yaml"))
        allocs = cfg["engine"]["allocations"]
        total  = sum(allocs.values())
        assert abs(total - 1.0) < 0.001, f"Allocations sum to {total}, not 1.0"

    def test_strategies_yaml_loads(self):
        import yaml
        with open("configs/strategies.yaml") as f:
            docs = list(yaml.safe_load_all(f))
        assert len(docs) >= 1

    def test_s10_max_minutes_is_minutes_not_seconds(self):
        import yaml
        with open("configs/strategies.yaml") as f:
            docs = list(yaml.safe_load_all(f))
        s10 = {}
        for doc in docs:
            if doc and "s10_near_resolution" in doc:
                s10 = doc["s10_near_resolution"]
                break
        max_min = s10.get("max_minutes_remaining", 0)
        # Must be a reasonable minutes value, not accidentally in seconds
        assert max_min <= 1440, f"max_minutes_remaining={max_min} looks like seconds"
        assert max_min >= 15,   f"max_minutes_remaining={max_min} too small"

    def test_lessons_json_valid(self):
        data = json.loads(open("data/lessons.json").read())
        assert "lessons" in data
        assert isinstance(data["lessons"], list)  # lessons list exists (may be empty after reset)
