"""
tests/test_integration.py
=========================
End-to-end integration tests for the Polymarket engine.

Tests:
1. Full cycle in DRY_RUN mode - no real orders submitted
2. All three strategies scan without error
3. SignalEngine exception isolation
4. ReviewEngine skips on no trades
"""

from unittest.mock import MagicMock, patch

import pytest

from engines.state_engine import StateEngine
from engines.data_engine import DataEngine, MarketState
from engines.execution_engine import ExecutionEngine
from engines.signal_engine import SignalEngine
from engines.review_engine import ReviewEngine
from strategies.s10_near_resolution import NearResolutionStrategy
from strategies.s1_negrisk_arb import NegRiskStrategy
from strategies.s8_logical_arb import LogicalArbStrategy


@pytest.fixture
def mock_clob():
    """Mock CLOB client for testing."""
    clob = MagicMock()
    clob.create_order.return_value = {"orderId": "order-123"}
    clob.get_market.return_value = {"feeRateBps": 15}
    return clob


@pytest.fixture
def store(tmp_path):
    """Fresh StateEngine for each test."""
    return StateEngine(
        db_path=str(tmp_path / "test.db"),
        lessons_path=str(tmp_path / "lessons.json"),
        initial_balance=100.0,
    )


@pytest.fixture
def sample_market():
    return MarketState(
        market_id="mkt-001",
        question="Will BTC close above $90K?",
        yes_token_id="yes-001",
        no_token_id="no-001",
        yes_price=0.94,
        no_price=0.06,
        yes_bid=0.93,
        no_bid=0.05,
        volume_24h=15000.0,
        end_date_iso="2099-12-31T23:59:00Z",
        seconds_to_resolution=900,
        negrisk_group_id=None,
        category="crypto",
        fee_rate_bps=15,
    )


class TestFullCycle:
    """Test the full scan cycle in DRY_RUN mode."""

    def test_full_cycle_dry_run_no_real_orders(self, store, mock_clob):
        """DRY_RUN=true must NEVER call clob.create_order."""
        config = {
            "risk": {
                "max_daily_loss_pct": 0.05,
                "max_open_positions": 5,
                "max_position_pct": 0.15,
            },
            "allocations": {
                "s10_near_resolution": 0.40,
                "s1_negrisk_arb": 0.40,
                "s8_logical_arb": 0.20,
            },
            "engine": {},
        }

        execution = ExecutionEngine(mock_clob, store, config, dry_run=True)

        s10 = NearResolutionStrategy()
        s1 = NegRiskStrategy()
        s8 = LogicalArbStrategy()

        signal = SignalEngine(
            data_engine=MagicMock(),
            execution_engine=execution,
            strategies=[s10, s1, s8],
            config=config,
        )

        result = signal.run_one_cycle()

        assert result is not None
        assert "markets_scanned" in result
        mock_clob.create_order.assert_not_called()

    def test_all_three_strategies_scan_without_error(self, store, mock_clob):
        """All 3 strategies must run without raising exceptions."""
        config = {
            "risk": {"max_position_pct": 0.15},
            "allocations": {"s10_near_resolution": 0.40, "s1_negrisk_arb": 0.40},
            "engine": {},
        }

        execution = ExecutionEngine(mock_clob, store, config, dry_run=True)

        strategies = [
            NearResolutionStrategy(),
            NegRiskStrategy(),
            LogicalArbStrategy(),
        ]

        signal = SignalEngine(
            data_engine=MagicMock(),
            execution_engine=execution,
            strategies=strategies,
            config=config,
        )

        result = signal.run_one_cycle()
        assert result is not None
        assert result.get("markets_scanned", 0) >= 0


class TestSignalEngineIsolation:
    """Test that broken strategies don't crash the engine."""

    def test_signal_engine_exception_isolation(self, store, mock_clob):
        """Broken strategy must not crash signal engine."""

        class BrokenStrategy:
            def scan(self, markets, negrisk_groups, config):
                raise Exception("Simulated crash!")

            def score(self, opp, config):
                return 0.0

            def size(self, opp, bankroll, config):
                return 1.0

            def on_resolve(self, resolution):
                return {}

        config = {"risk": {}, "allocations": {}, "engine": {}}
        execution = ExecutionEngine(mock_clob, store, config, dry_run=True)

        signal = SignalEngine(
            data_engine=MagicMock(),
            execution_engine=execution,
            strategies=[BrokenStrategy()],
            config=config,
        )

        result = signal.run_one_cycle()
        assert result is not None
        assert "error" in result or result.get("opportunities_found", 0) == 0


class TestReviewEngine:
    """Test ReviewEngine behavior."""

    def test_review_engine_skips_on_no_trades(self, store):
        """run_after_resolution must return status:skipped when no trades."""
        review = ReviewEngine(store, {})
        result = review.run_after_resolution("fake-market-id")
        assert result.get("status") == "skipped"
        assert result.get("reason") == "no_trades"