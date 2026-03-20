"""
engines/execution_engine.py
============================
THE ONLY ENGINE THAT PLACES REAL ORDERS.

All risk gates live here. Nothing else submits orders.

CRITICAL RULES (violation = bug):
  1. fee_rate_bps is read from MarketState.fee_rate_bps (fetched by DataEngine).
     Only re-fetches from API if MarketState is stale (> 5 min old).
     NEVER hardcoded. NEVER zero.
  2. DRY_RUN=true → NEVER calls any order submission API.
  3. Risk gates execute in fixed order. Stop at first failure.
  4. Every rejection is logged with reason, strategy, and market_id.
"""

from __future__ import annotations

import os
import time
from typing import Optional

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from engines.state_engine import StateEngine, TradeRecord
from engines.data_engine import MarketState
from strategies.base import BaseStrategy, Opportunity

log = structlog.get_logger()

_MAX_MARKET_STATE_AGE_SECONDS = 300  # re-fetch fee_rate_bps if snapshot > 5 min old


class ExecutionEngine:
    """
    Executes trading opportunities against Polymarket CLOB.
    Enforces all risk constraints before any order is submitted.
    """

    def __init__(
        self,
        clob_client,               # py-clob-client ClobClient instance
        state_engine: StateEngine,
        config: dict,
        dry_run: bool = False,
    ) -> None:
        self._client = clob_client
        self._state = state_engine
        self._config = config
        self._dry_run = dry_run or (os.environ.get("DRY_RUN", "false").lower() == "true")

        if self._dry_run:
            log.warning("execution_engine.dry_run_active")

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def execute_opportunity(
        self,
        opp: Opportunity,
        strategy: BaseStrategy,
        market_state: MarketState,
        strategy_config: dict,
    ) -> Optional[str]:
        """
        Main execution entry point.

        Returns:
            trade_id (str)  — if executed or dry-run
            None            — if rejected by any risk gate

        Risk gate order (stop at first failure):
            Gate 1: daily loss limit
            Gate 2: max open positions
            Gate 3: min trade size ($1 floor)
            Gate 4: DRY_RUN guard
            Gate 5: live order submission
        """
        balance = self._state.get_current_balance()
        risk    = self._config["risk"]

        # --- Gate 1: Daily loss limit ---
        daily_pnl      = self._state.get_daily_pnl()
        max_daily_loss = risk["max_daily_loss_pct"] * balance
        if daily_pnl <= -max_daily_loss:
            self._reject(opp, "DAILY_LOSS_LIMIT_HIT")
            log.error("execution_engine.halt", daily_pnl=daily_pnl, limit=-max_daily_loss)
            return None

        # --- Gate 2: Max open positions ---
        if self._state.get_open_position_count() >= risk["max_open_positions"]:
            self._reject(opp, "MAX_POSITIONS_REACHED")
            return None

        # --- Gate 3: Min trade size ---
        allocation = self._config["allocations"].get(opp.strategy, 0.0)
        allocated  = balance * allocation
        size_usdc  = strategy.size(opp, allocated, strategy_config)
        max_size   = risk["max_position_pct"] * balance
        size_usdc  = min(size_usdc, max_size)  # hard cap

        if size_usdc < 1.0:
            self._reject(opp, "SIZE_TOO_SMALL")
            return None

        # --- Gate 4: DRY_RUN ---
        if self._dry_run:
            trade_id = f"DRY_RUN_{opp.market_id}_{int(time.time())}"
            log.info("execution_engine.dry_run", trade_id=trade_id,
                     strategy=opp.strategy, size_usdc=size_usdc)
            return trade_id

        # --- Gate 5: Live submission ---
        fee_bps = self._get_fee_rate_bps(opp, market_state)

        order  = self._build_order(opp, size_usdc, fee_bps, market_state)
        result = self._submit_with_retry(order)
        trade_id = result.get("orderId") or result.get("id", "UNKNOWN")

        self._state.log_trade(TradeRecord(
            trade_id=trade_id,
            strategy=opp.strategy,
            market_id=opp.market_id,
            market_question=opp.market_question,
            side="YES" if opp.action in ("buy_yes", "buy_all_yes") else "NO",
            price=opp.win_probability,
            shares=size_usdc / max(opp.win_probability, 0.001),
            cost_usdc=size_usdc,
            fee_usdc=size_usdc * (fee_bps / 10000),
            status="open",
        ))

        log.info("execution_engine.trade_submitted",
                 trade_id=trade_id, strategy=opp.strategy,
                 market_id=opp.market_id, size_usdc=size_usdc, fee_bps=fee_bps)
        return trade_id

    # -----------------------------------------------------------------------
    # Fee rate — NEVER hardcoded
    # -----------------------------------------------------------------------

    def _get_fee_rate_bps(self, opp: Opportunity, market_state: MarketState) -> int:
        """
        Returns fee_rate_bps for the order.

        Priority:
          1. market_state.fee_rate_bps if snapshot is fresh (< 5 min old)
          2. Re-fetch via clob_client.get_market() if snapshot is stale
          3. Fallback: 200 bps (2%) — safe overestimate, never 0

        Note: py-clob-client 0.16 exposes get_market(token_id) which returns
        a dict including 'feeRateBps'. There is NO standalone get_fee_rate_bps()
        method in this SDK version.
        """
        # Use cached value if fresh
        if market_state and not _is_stale(market_state.fetched_at):
            bps = market_state.fee_rate_bps
            if bps and bps > 0:
                return bps

        # Re-fetch from CLOB API
        token_id = (
            market_state.yes_token_id
            if opp.action in ("buy_yes", "buy_all_yes")
            else market_state.no_token_id
        )
        try:
            mkt = self._client.get_market(token_id)
            bps = int(mkt.get("feeRateBps") or mkt.get("fee_rate_bps") or 200)
            log.info("execution_engine.fee_rate_fetched", token_id=token_id, bps=bps)
            return max(1, bps)
        except Exception as exc:
            log.warning("execution_engine.fee_rate_fallback",
                        token_id=token_id, error=str(exc))
            return 200  # 2% safe overestimate — never 0

    # -----------------------------------------------------------------------
    # Order building
    # -----------------------------------------------------------------------

    def _build_order(
        self,
        opp: Opportunity,
        size_usdc: float,
        fee_bps: int,
        market_state: MarketState,
    ) -> dict:
        """
        Build order payload for py-clob-client.

        Order types:
          buy_all_yes (NegRisk arb): FOK — Fill-or-Kill (all legs must fill)
          buy_yes / buy_no:          GTD — Good-Till-Date (near-resolution, logical arb)
          sell_all_yes (NegRisk):    FOK

        py-clob-client 0.16 order structure:
          token_id, price, size, side ('BUY'/'SELL'), order_type, feeRateBps
        """
        is_buy = opp.action in ("buy_yes", "buy_all_yes")
        token_id = market_state.yes_token_id if is_buy else market_state.no_token_id
        order_type = "FOK" if opp.action in ("buy_all_yes", "sell_all_yes") else "GTD"

        return {
            "token_id":    token_id,
            "price":       opp.win_probability,
            "size":        size_usdc,
            "side":        "BUY" if is_buy else "SELL",
            "order_type":  order_type,
            "feeRateBps":  fee_bps,    # required field — never omit, never hardcode
        }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8))
    def _submit_with_retry(self, order: dict) -> dict:
        """Submit order to Polymarket CLOB with retry on transient errors."""
        return self._client.create_order(order)

    # -----------------------------------------------------------------------
    # Logging
    # -----------------------------------------------------------------------

    def _reject(self, opp: Opportunity, reason: str) -> None:
        log.warning("execution_engine.rejected",
                    reason=reason, strategy=opp.strategy,
                    market_id=opp.market_id, action=opp.action)


def _is_stale(fetched_at: float) -> bool:
    return (time.time() - fetched_at) > _MAX_MARKET_STATE_AGE_SECONDS
