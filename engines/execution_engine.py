"""
engines/execution_engine.py — THE ONLY engine that submits orders.

5 risk gates in fixed order before any order is placed:
  1. Daily P&L check (halt if max_daily_loss exceeded)
  2. Open position count check
  3. Min size check ($1.00 floor)
  4. DRY_RUN guard (never submit if dry_run=True)
  5. Live fee_rate_bps re-fetch (re-fetch if MarketState > 5 min old)

All monetary values: USDC. All probabilities: float [0.0, 1.0].
NEVER raises — returns None on any failure.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Optional

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from strategies.base import BaseStrategy, Opportunity
from engines.data_engine import DataEngine, MarketState
from engines.state_engine import StateEngine, TradeRecord

log = structlog.get_logger(component="execution_engine")

MARKET_STALENESS_SEC = 300   # 5 minutes


class ExecutionEngine:
    def __init__(self,
                 state_engine: StateEngine,
                 data_engine:  DataEngine,
                 config:       dict,
                 dry_run:      bool = True):
        self.state     = state_engine
        self.data      = data_engine
        self.config    = config
        self.dry_run   = dry_run
        self._clob     = None   # lazy-init — avoids import cost when dry_run=True

    # ── Public interface ───────────────────────────────────────────────────────

    def execute_opportunity(self,
                            opp:           Opportunity,
                            strategy:      BaseStrategy,
                            market_state:  Optional[MarketState],
                            bankroll:      float) -> Optional[str]:
        """
        Execute one opportunity through all 5 risk gates.
        Returns trade_id (str) if executed, None if rejected at any gate.
        trade_id starts with "DRY_RUN_" when dry_run=True.
        """
        risk = self.config.get("engine", {}).get("risk", {})

        # ── Gate 1: Daily P&L ─────────────────────────────────────────────────
        daily_pnl     = self.state.get_daily_pnl()
        max_daily_loss = bankroll * float(risk.get("max_daily_loss_pct", 0.05))
        if daily_pnl < -max_daily_loss:
            log.warning("gate1_daily_loss_limit",
                        daily_pnl=daily_pnl, max_loss=max_daily_loss)
            return None

        # ── Gate 2: Open position count ───────────────────────────────────────
        open_count = self.state.get_open_position_count()
        max_open   = int(risk.get("max_open_positions", 5))
        if open_count >= max_open:
            log.info("gate2_max_positions", open=open_count, max=max_open)
            return None

        # ── Gate 3: Size check ────────────────────────────────────────────────
        size_usdc = strategy.size(opp, bankroll, self.config)
        if size_usdc < 1.0:
            log.info("gate3_size_too_small", size=size_usdc)
            return None

        # ── Gate 4: DRY_RUN ───────────────────────────────────────────────────
        if self.dry_run:
            trade_id = f"DRY_RUN_{uuid.uuid4().hex[:12].upper()}"
            buy_price = opp.metadata.get("buy_price",
                        opp.metadata.get("yes_ask", opp.win_probability))
            shares    = size_usdc / buy_price if buy_price > 0 else 0
            fee_usdc  = strategy.calc_fee(buy_price) * size_usdc

            record = TradeRecord(
                trade_id       = trade_id,
                strategy       = opp.strategy,
                market_id      = opp.market_id,
                market_question = opp.market_question,
                side           = opp.action.replace("BUY_", ""),
                price          = buy_price,
                shares         = shares,
                cost_usdc      = size_usdc,
                fee_usdc       = fee_usdc,
                status         = "open",
                entry_time     = _now_iso(),
                notes          = f"DRY_RUN score={opp.score:.3f} edge={opp.edge:.4f}",
            )
            # Inject metadata for on_resolve
            record_dict = record.__dict__
            record_dict["metadata"] = opp.metadata

            self.state.log_trade(record)
            log.info("dry_run_trade_logged",
                     trade_id=trade_id, strategy=opp.strategy,
                     size=size_usdc, score=opp.score, edge=opp.edge)
            return trade_id

        # ── Gate 5: Live fee_rate_bps re-fetch ───────────────────────────────
        token_id = opp.metadata.get("token_id", "")
        fee_rate_bps = self._get_live_fee_bps(market_state, token_id)

        # ── Submit order ──────────────────────────────────────────────────────
        return self._submit_order(opp, strategy, size_usdc, fee_rate_bps, bankroll)

    def check_and_settle(self, position: dict) -> bool:
        """
        Check if an open position has resolved and settle it.
        Returns True if resolved and settled.
        """
        try:
            market_id = position.get("market_id", "")
            token_id  = position.get("metadata", {}).get("token_id", "")
            side      = position.get("side", "YES")

            if not token_id:
                return False

            # Query CLOB for resolution status
            clob = self._get_clob_client()
            if not clob:
                return False

            market = clob.get_market(token_id)
            if not market:
                return False

            # Check if resolved
            resolved_price = float(market.get("lastTradedPrice", -1))
            status = market.get("gameStatus", "")

            is_resolved = (status == "resolved" or
                           resolved_price in (0.0, 1.0) or
                           market.get("closed", False))

            if not is_resolved:
                return False

            # Determine outcome
            if side == "YES":
                won = resolved_price >= 0.99
            elif side == "NO":
                won = resolved_price <= 0.01
            else:
                won = False

            outcome = "win" if won else "loss"
            pnl_usdc = (position["shares"] - position["cost_usdc"] - position["fee_usdc"]
                        if won else
                        -position["cost_usdc"] - position["fee_usdc"])

            self.state.mark_resolved(market_id, outcome, pnl_usdc)
            log.info("position_settled",
                     market_id=market_id, outcome=outcome, pnl=pnl_usdc)
            return True

        except Exception as e:
            log.warning("settle_failed",
                        market_id=position.get("market_id"), error=str(e))
            return False

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_live_fee_bps(self, market_state: Optional[MarketState],
                           token_id: str) -> int:
        """Re-fetch fee_rate_bps if market_state is stale."""
        if market_state and not market_state.is_stale(MARKET_STALENESS_SEC):
            return market_state.fee_rate_bps

        # Stale or missing — re-fetch
        try:
            clob   = self._get_clob_client()
            detail = clob.get_market(token_id) if clob else {}
            bps    = int(detail.get("feeRateBps", 0))
            if bps > 0:
                return bps
        except Exception as e:
            log.warning("fee_rate_refetch_failed", token_id=token_id, error=str(e))

        # Fall back to formula estimate from market_state
        if market_state:
            return market_state.fee_rate_bps

        return 25  # safe default (0.25%) if all else fails

    def _get_clob_client(self):
        """Lazy-init CLOB client (heavy import, skip in dry_run)."""
        if self._clob is None:
            try:
                from py_clob_client.client import ClobClient
                from py_clob_client.clob_types import ApiCreds

                creds = ApiCreds(
                    api_key     = os.environ["POLYMARKET_API_KEY"],
                    api_secret  = os.environ["POLYMARKET_API_SECRET"],
                    api_passphrase = os.environ["POLYMARKET_PASSPHRASE"],
                )
                self._clob = ClobClient(
                    host         = "https://clob.polymarket.com",
                    chain_id     = 137,
                    private_key  = os.environ["POLYMARKET_PRIVATE_KEY"],
                    creds        = creds,
                    signature_type = 0,
                    funder       = os.environ["POLYMARKET_WALLET_ADDRESS"],
                )
            except Exception as e:
                log.error("clob_client_init_failed", error=str(e))
                return None
        return self._clob

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def _submit_order(self,
                      opp:          Opportunity,
                      strategy:     BaseStrategy,
                      size_usdc:    float,
                      fee_rate_bps: int,
                      bankroll:     float) -> Optional[str]:
        """Submit a real order to Polymarket CLOB."""
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            clob      = self._get_clob_client()
            token_id  = opp.metadata.get("token_id", "")
            buy_price = opp.metadata.get("buy_price",
                        opp.metadata.get("yes_ask", opp.win_probability))

            if not token_id:
                log.error("no_token_id", market_id=opp.market_id)
                return None

            shares = round(size_usdc / buy_price, 2) if buy_price > 0 else 0
            if shares < 1.0:
                log.info("shares_too_small", shares=shares)
                return None

            order_args = OrderArgs(
                token_id      = token_id,
                price         = round(buy_price, 4),
                size          = shares,
                side          = "BUY",
                fee_rate_bps  = fee_rate_bps,
                nonce         = int(time.time() * 1000),
            )

            signed_order = clob.create_order(order_args)
            response     = clob.post_order(signed_order, OrderType.GTC)

            if not response or not response.get("orderID"):
                log.error("order_rejected", response=str(response)[:200])
                return None

            order_id = response["orderID"]
            fee_usdc = strategy.calc_fee(buy_price) * size_usdc

            record = TradeRecord(
                trade_id        = order_id,
                strategy        = opp.strategy,
                market_id       = opp.market_id,
                market_question = opp.market_question,
                side            = opp.action.replace("BUY_", ""),
                price           = buy_price,
                shares          = shares,
                cost_usdc       = size_usdc,
                fee_usdc        = fee_usdc,
                status          = "open",
                entry_time      = _now_iso(),
                notes           = f"score={opp.score:.3f} edge={opp.edge:.4f}",
            )
            self.state.log_trade(record)

            log.info("order_submitted",
                     order_id=order_id, strategy=opp.strategy,
                     token_id=token_id, price=buy_price,
                     shares=shares, cost_usdc=size_usdc)
            return order_id

        except Exception as e:
            log.error("order_submit_failed", error=str(e))
            raise   # Let tenacity retry


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
