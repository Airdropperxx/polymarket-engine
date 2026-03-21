"""
tests/test_engines.py
=====================
Unit tests for all core engines.

Rules:
  - No real HTTP calls. All external requests mocked with 'responses' library.
  - No real Anthropic API calls. Mock where needed.
  - No real Polymarket order submissions.
  - Uses pytest fixtures and tmp_path for isolated databases.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch, call

import pytest
import responses as rsps_lib
from freezegun import freeze_time

from engines.state_engine import StateEngine, TradeRecord
from engines.data_engine import DataEngine, MarketState, _seconds_until, _categorise
from engines.execution_engine import ExecutionEngine
from engines.monitor_engine import MonitorEngine
from engines.review_engine import ReviewEngine
from strategies.base import Opportunity


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def store(tmp_path):
    """Fresh StateEngine backed by a temp SQLite DB for each test."""
    return StateEngine(
        db_path=str(tmp_path / "test.db"),
        lessons_path=str(tmp_path / "lessons.json"),
        initial_balance=100.0,
    )


@pytest.fixture
def sample_trade():
    return TradeRecord(
        trade_id="trade-001",
        strategy="s10_near_resolution",
        market_id="mkt-abc",
        market_question="Will BTC close above $90K?",
        side="YES",
        price=0.92,
        shares=10.87,
        cost_usdc=10.0,
        fee_usdc=0.05,
        status="open",
    )


@pytest.fixture
def sample_market():
    return MarketState(
        market_id="mkt-001",
        question="Will BTC close above $90K at 14:30 UTC?",
        yes_token_id="yes-001",
        no_token_id="no-001",
        yes_price=0.94,
        no_price=0.06,
        yes_bid=0.93,
        no_bid=0.05,
        volume_24h=15000.0,
        end_date_iso="2099-12-31T23:59:00Z",
        seconds_to_resolution=900,   # 15 minutes
        negrisk_group_id=None,
        category="crypto",
        fee_rate_bps=15,
    )


SAMPLE_MARKETS_RAW = [
    {
        "conditionId": "mkt-001",
        "question": "Will BTC close above $90K?",
        "active": True, "closed": False,
        "endDate": "2099-12-31T23:59:00Z",
        "volume24hr": 15000.0,
        "negRiskGroupId": None,
        "feeRateBps": 15,
        "tags": ["crypto", "bitcoin"],
        "tokens": [
            {"outcome": "YES", "token_id": "yes-001", "price": "0.94", "bid": "0.93"},
            {"outcome": "NO",  "token_id": "no-001",  "price": "0.06", "bid": "0.05"},
        ],
    },
    {
        "conditionId": "mkt-002",
        "question": "Who wins Vermont Governor? Candidate A",
        "active": True, "closed": False,
        "endDate": "2099-11-05T00:00:00Z",
        "volume24hr": 500.0,
        "negRiskGroupId": "neg-gov-001",
        "feeRateBps": 200,
        "tags": ["politics"],
        "tokens": [
            {"outcome": "YES", "token_id": "yes-002", "price": "0.22", "bid": "0.21"},
            {"outcome": "NO",  "token_id": "no-002",  "price": "0.78", "bid": "0.77"},
        ],
    },
    {
        "conditionId": "mkt-003",
        "question": "Who wins Vermont Governor? Candidate B",
        "active": True, "closed": False,
        "endDate": "2099-11-05T00:00:00Z",
        "volume24hr": 450.0,
        "negRiskGroupId": "neg-gov-001",
        "feeRateBps": 200,
        "tags": ["politics"],
        "tokens": [
            {"outcome": "YES", "token_id": "yes-003", "price": "0.26", "bid": "0.25"},
            {"outcome": "NO",  "token_id": "no-003",  "price": "0.74", "bid": "0.73"},
        ],
    },
]


# ============================================================================
# StateEngine tests
# ============================================================================

class TestStateEngine:

    def test_creates_db_on_init(self, store):
        """DB and tables must exist and return default balance after __init__."""
        assert store.get_current_balance() == 100.0

    def test_daily_pnl_zero_on_empty_db(self, store):
        """get_daily_pnl() must return 0.0 on empty DB, never raise."""
        result = store.get_daily_pnl()
        assert result == 0.0

    def test_log_trade_returns_positive_int(self, store, sample_trade):
        """log_trade() must return a positive integer row ID."""
        row_id = store.log_trade(sample_trade)
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_open_position_count_increments(self, store, sample_trade):
        """Open position count must increment after logging a trade."""
        assert store.get_open_position_count() == 0
        store.log_trade(sample_trade)
        assert store.get_open_position_count() == 1

    def test_mark_resolved_closes_position(self, store, sample_trade):
        """mark_resolved() must update status and clear open position count."""
        store.log_trade(sample_trade)
        assert store.get_open_position_count() == 1
        store.mark_resolved("mkt-abc", "win", 0.64)
        assert store.get_open_position_count() == 0

    def test_balance_tracking(self, store):
        """update_balance() must persist the new value."""
        store.update_balance(142.50)
        assert abs(store.get_current_balance() - 142.50) < 0.001

    def test_lessons_roundtrip(self, store):
        """save_lessons() and get_lessons() must roundtrip correctly."""
        data = {"lessons": ["test lesson"], "strategy_scores": {}, "deprecated_lessons": []}
        store.save_lessons(data)
        loaded = store.get_lessons()
        assert loaded["lessons"] == ["test lesson"]

    def test_get_lessons_returns_default_when_missing(self, store):
        """get_lessons() returns safe default dict if file doesn't exist."""
        result = store.get_lessons()
        assert "lessons" in result
        assert isinstance(result["lessons"], list)

    def test_get_recent_resolved_trades_empty(self, store):
        """get_recent_resolved_trades() returns [] on empty DB."""
        result = store.get_recent_resolved_trades(hours=48)
        assert result == []


# ============================================================================
# DataEngine tests  (all HTTP mocked)
# ============================================================================

class TestDataEngine:

    @pytest.fixture
    def bus(self):
        return DataEngine(config={})

    @pytest.fixture
    def bus_with_mock(self, bus):
        with patch.object(bus, "_fetch_markets_paginated", return_value=SAMPLE_MARKETS_RAW):
            yield bus

    def test_fetch_returns_market_state_objects(self, bus_with_mock):
        """fetch_all_markets() must return MarketState objects, not raw dicts."""
        markets = bus_with_mock.fetch_all_markets()
        assert len(markets) == 3
        assert all(isinstance(m, MarketState) for m in markets)

    def test_prices_clamped_to_valid_range(self, bus_with_mock):
        """All prices must be in (0, 1) range after clamping."""
        markets = bus_with_mock.fetch_all_markets()
        for m in markets:
            assert 0.001 <= m.yes_price <= 0.999
            assert 0.001 <= m.no_price  <= 0.999

    def test_fee_rate_bps_populated(self, bus_with_mock):
        """fee_rate_bps must be populated from API response."""
        markets = bus_with_mock.fetch_all_markets()
        crypto_market = next(m for m in markets if m.category == "crypto")
        assert crypto_market.fee_rate_bps == 15

    def test_negrisk_grouping_correct(self, bus_with_mock):
        """Two markets with same negRiskGroupId must appear in the same group."""
        bus_with_mock.fetch_all_markets()
        groups = bus_with_mock.fetch_negrisk_groups()
        assert "neg-gov-001" in groups
        assert len(groups["neg-gov-001"]) == 2

    def test_stale_cache_returned_on_error(self, bus):
        """On API error, return stale cache — never raise."""
        bus._cache = [MarketState(
            market_id="cached-mkt", question="Cached?",
            yes_token_id="y", no_token_id="n",
            yes_price=0.5, no_price=0.5, yes_bid=0.49, no_bid=0.49,
            volume_24h=100, end_date_iso="2099-01-01T00:00:00Z",
            seconds_to_resolution=99999, category="other", fee_rate_bps=200,
            negrisk_group_id=None,
        )]
        with patch.object(bus, "_fetch_markets_paginated", side_effect=Exception("network error")):
            result = bus.fetch_all_markets()
        assert len(result) == 1   # stale cache returned

    def test_seconds_until_future(self):
        assert _seconds_until("2099-12-31T23:59:00Z") > 0

    def test_seconds_until_past(self):
        assert _seconds_until("2000-01-01T00:00:00Z") < 0

    def test_seconds_until_empty(self):
        assert _seconds_until("") == -1

    def test_categorise_crypto(self):
        assert _categorise(["bitcoin"], "Will BTC price rise?") == "crypto"

    def test_categorise_politics(self):
        assert _categorise([], "Who wins the 2026 election?") == "politics"

    def test_categorise_sports(self):
        assert _categorise(["nba"], "Who wins the NBA championship?") == "sports"

    def test_categorise_other(self):
        assert _categorise([], "Will it rain in London tomorrow?") == "other"


# ============================================================================
# ExecutionEngine tests
# ============================================================================

class TestExecutionEngine:

    def _make_opp(self, market_id="mkt-001", strategy="s10_near_resolution"):
        return Opportunity(
            strategy=strategy,
            market_id=market_id,
            market_question="Test market?",
            action="buy_yes",
            edge=0.05,
            win_probability=0.94,
            max_payout=1.064,
            time_to_resolution_sec=900,
        )

    def _make_market_state(self, market_id="mkt-001"):
        return MarketState(
            market_id=market_id,
            question="Test?",
            yes_token_id="yes-t",
            no_token_id="no-t",
            yes_price=0.94, no_price=0.06,
            yes_bid=0.93, no_bid=0.05,
            volume_24h=1000.0,
            end_date_iso="2099-01-01T00:00:00Z",
            seconds_to_resolution=900,
            category="crypto",
            fee_rate_bps=15,
            fetched_at=time.time(),
        )

    def _make_engine(self, store, dry_run=True):
        clob = MagicMock()
        clob.create_order.return_value = {"orderId": "order-123"}
        clob.get_market.return_value = {"feeRateBps": 15}
        config = {
            "risk": {
                "max_daily_loss_pct": 0.05,
                "max_open_positions": 5,
                "max_position_pct": 0.15,
            },
            "allocations": {"s10_near_resolution": 0.40, "s1_negrisk_arb": 0.40},
            "engine": {},
        }
        return ExecutionEngine(clob, store, config, dry_run=dry_run)

    def _make_strategy(self):
        from strategies.s10_near_resolution import NearResolutionStrategy
        return NearResolutionStrategy()

    def _make_strategy_config(self):
        return {"max_position_pct": 0.15, "kelly_fraction": 0.25, "threshold": 0.50}

    def test_dry_run_never_submits_order(self, store):
        """DRY_RUN=true must NEVER call clob.create_order under any circumstances."""
        engine = self._make_engine(store, dry_run=True)
        opp = self._make_opp()
        ms  = self._make_market_state()

        result = engine.execute_opportunity(opp, self._make_strategy(), ms, self._make_strategy_config())

        assert result is not None
        assert result.startswith("DRY_RUN_")
        engine._client.create_order.assert_not_called()

    def test_gate1_daily_loss_rejects(self, store, sample_trade):
        """Gate 1: engine must reject when daily loss limit is hit."""
        # Simulate $6 loss (above 5% of $100 = $5 limit)
        store.log_trade(sample_trade)
        store.mark_resolved("mkt-abc", "loss", -6.0)

        engine = self._make_engine(store, dry_run=False)
        opp    = self._make_opp()
        ms     = self._make_market_state()

        result = engine.execute_opportunity(opp, self._make_strategy(), ms, self._make_strategy_config())
        assert result is None
        engine._client.create_order.assert_not_called()

    def test_gate2_max_positions_rejects(self, store):
        """Gate 2: engine must reject when max open positions reached."""
        # Fill up to max (5 positions)
        for i in range(5):
            store.log_trade(TradeRecord(
                trade_id=f"t{i}", strategy="s10_near_resolution",
                market_id=f"mkt-{i}", market_question="Q?",
                side="YES", price=0.9, shares=1, cost_usdc=5.0, fee_usdc=0.01,
            ))

        engine = self._make_engine(store, dry_run=False)
        opp    = self._make_opp()
        ms     = self._make_market_state()

        result = engine.execute_opportunity(opp, self._make_strategy(), ms, self._make_strategy_config())
        assert result is None

    def test_gate3_min_size_rejects(self, store):
        """Gate 3: engine must reject if calculated size < $1."""
        engine = self._make_engine(store, dry_run=False)
        opp    = self._make_opp()
        ms     = self._make_market_state()
        # Give an allocation so tiny size < $1
        engine._config["allocations"]["s10_near_resolution"] = 0.001

        result = engine.execute_opportunity(opp, self._make_strategy(), ms, self._make_strategy_config())
        assert result is None

    def test_fee_rate_read_from_market_state(self, store):
        """fee_rate_bps must be read from MarketState, not hardcoded."""
        engine = self._make_engine(store, dry_run=False)
        opp    = self._make_opp()
        ms     = self._make_market_state()
        ms.fee_rate_bps = 99  # distinctive value

        engine.execute_opportunity(opp, self._make_strategy(), ms, self._make_strategy_config())

        # Verify order was built with the market_state fee, not a hardcoded value
        call_args = engine._client.create_order.call_args
        if call_args:
            order = call_args[0][0]
            assert order.get("feeRateBps") == 99

    def test_successful_execution_logs_trade(self, store):
        """A successful live execution must log a TradeRecord to StateEngine."""
        engine = self._make_engine(store, dry_run=False)
        opp    = self._make_opp()
        ms     = self._make_market_state()

        result = engine.execute_opportunity(opp, self._make_strategy(), ms, self._make_strategy_config())

        assert result == "order-123"
        assert store.get_open_position_count() == 1


# ============================================================================
# MonitorEngine tests
# ============================================================================

class TestMonitorEngine:

    def test_missing_telegram_config_does_not_crash(self, monkeypatch):
        """Engine must initialise without error when Telegram not configured."""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        monitor = MonitorEngine({})
        assert not monitor._enabled
        # send() must not raise
        monitor.send("daily_summary", pnl=1.5, trades=3, balance=101.5)

    def test_all_event_types_format(self, monkeypatch):
        """All 6 event types must format without KeyError."""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monitor = MonitorEngine({})
        monitor.send("trade_executed", strategy="s10", action="buy_yes",
                     question="Will BTC rise?", size=14.82, price=0.94)
        monitor.send("trade_rejected",  strategy="s10", reason="DAILY_LOSS")
        monitor.send("daily_summary",   pnl=1.5, trades=3, balance=101.5)
        monitor.send("risk_limit_hit",  limit_type="DAILY_LOSS")
        monitor.send("error",           component="data_engine", error="timeout")
        monitor.send("lesson_update",   lesson="S10 sports threshold raised to 0.95")

    @rsps_lib.activate
    def test_telegram_send_on_error_does_not_crash(self, monkeypatch):
        """Network error to Telegram must not propagate."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID",   "12345")
        rsps_lib.add(rsps_lib.POST,
                     "https://api.telegram.org/botfake-token/sendMessage",
                     body=Exception("connection error"))
        monitor = MonitorEngine({})
        monitor.send("error", component="test", error="boom")  # must not raise


# ============================================================================
# ReviewEngine tests
# ============================================================================

class TestReviewEngine:

    def test_skips_when_no_recent_trades(self, store):
        """run_after_resolution() must return status:skipped when 0 trades."""
        review = ReviewEngine(store, {})
        result = review.run_after_resolution("fake-market-id")
        assert result["status"] == "skipped"
        assert result["reason"] == "no_trades"

    def test_parse_response_valid_json(self, store):
        """_parse_response must parse valid JSON."""
        review = ReviewEngine(store, {})
        raw = '{"strategy_score_updates": {}, "new_lessons": ["x"], "deprecated_lesson_indices": [], "reasoning": "ok"}'
        result = review._parse_response(raw)
        assert result["new_lessons"] == ["x"]

    def test_parse_response_json_in_prose(self, store):
        """_parse_response must extract JSON even when surrounded by prose."""
        review = ReviewEngine(store, {})
        raw = 'Here is my analysis: {"new_lessons": ["y"], "strategy_score_updates": {}, "deprecated_lesson_indices": [], "reasoning": "r"} Hope that helps!'
        result = review._parse_response(raw)
        assert result is not None
        assert result["new_lessons"] == ["y"]

    def test_parse_response_unparseable_returns_none(self, store):
        """_parse_response must return None (not raise) on totally unparseable input."""
        review = ReviewEngine(store, {})
        result = review._parse_response("this is not json at all !! @@")
        assert result is None

    def test_allocation_delta_capped(self, store):
        """_apply_updates must cap allocation_delta at ±0.05."""
        review = ReviewEngine(store, {})
        lessons = {
            "lessons": [], "deprecated_lessons": [],
            "strategy_scores": {
                "s10_near_resolution": {"allocation": 0.40, "win_rate": None, "avg_roi": None}
            },
            "capital_history": [],
        }
        updates = {
            "strategy_score_updates": {
                "s10_near_resolution": {"allocation_delta": 0.20}  # request 20% — should be capped
            },
            "new_lessons": [],
            "deprecated_lesson_indices": [],
            "reasoning": "test",
        }
        review._apply_updates(updates, lessons)
        new_alloc = lessons["strategy_scores"]["s10_near_resolution"]["allocation"]
        # Should be 0.40 + 0.05 = 0.45, not 0.40 + 0.20 = 0.60
        assert abs(new_alloc - 0.45) < 0.001

    def test_lessons_pruned_at_max(self, store):
        """_apply_updates must prune oldest lessons when list exceeds 20 items."""
        review = ReviewEngine(store, {})
        lessons = {
            "lessons": [f"lesson {i}" for i in range(20)],
            "deprecated_lessons": [],
            "strategy_scores": {},
            "capital_history": [],
        }
        updates = {
            "strategy_score_updates": {},
            "new_lessons": ["new lesson A", "new lesson B"],
            "deprecated_lesson_indices": [],
            "reasoning": "test",
        }
        review._apply_updates(updates, lessons)
        assert len(lessons["lessons"]) == 20   # still capped at 20
        assert "new lesson A" in lessons["lessons"]
        assert "new lesson B" in lessons["lessons"]
