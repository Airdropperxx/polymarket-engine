"""
engines/execution_engine.py

The ONLY engine that submits orders.
In dry-run mode: logs every opportunity with full metadata to data/scan_log.json
so we can mine patterns without spending real money.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from strategies.base import BaseStrategy, Opportunity
from engines.data_engine import DataEngine, MarketState
from engines.state_engine import StateEngine, TradeRecord

log = structlog.get_logger(component="execution_engine")

SCAN_LOG   = Path("data/scan_log.json")
MARKET_STALENESS_SEC = 300


class ExecutionEngine:
    def __init__(self,
                 state_engine: StateEngine,
                 data_engine:  DataEngine,
                 config:       dict,
                 dry_run:      bool = True):
        self.state    = state_engine
        self.data     = data_engine
        self.config   = config
        self.dry_run  = dry_run
        self._clob    = None

    # ------------------------------------------------------------------ #
    #  Opportunity logging (dry-run data collection)                      #
    # ------------------------------------------------------------------ #

    def log_opportunity(self, opp: Opportunity, executed: bool,
                        reason_skipped: str = "") -> None:
        """
        Append every opportunity seen to data/scan_log.json.
        This builds the dataset for pattern analysis regardless of execution.
        """
        try:
            SCAN_LOG.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts":            datetime.now(timezone.utc).isoformat(),
                "ts_unix":       int(time.time()),
                "strategy":      opp.strategy,
                "market_id":     opp.market_id,
                "question":      opp.market_question[:120],
                "action":        opp.action,
                "edge":          round(opp.edge, 5),
                "probability":   round(opp.win_probability, 4),
                "score":         round(opp.score, 4),
                "days_to_res":   round(opp.time_to_resolution_sec / 86400, 3),
                "executed":      executed,
                "skipped_reason": reason_skipped,
                # Rich metadata for pattern mining
                "category":      opp.metadata.get("category", ""),
                "volume_24h":    opp.metadata.get("volume_24h", 0),
                "buy_price":     opp.metadata.get("buy_price", 0),
                "fee":           opp.metadata.get("fee", 0),
                "spread":        opp.metadata.get("spread", 0),
                "fee_rate_bps":  opp.metadata.get("fee_rate_bps", 0),
                "num_legs":      opp.metadata.get("num_legs", 1),
                "total_ask":     opp.metadata.get("total_ask", 0),
                "hour_utc":      datetime.now(timezone.utc).hour,
                "weekday":       datetime.now(timezone.utc).weekday(),
            }

            # Load existing log
            if SCAN_LOG.exists():
                try:
                    existing = json.loads(SCAN_LOG.read_text())
                except Exception:
                    existing = []
            else:
                existing = []

            existing.append(entry)

            # Keep last 5000 entries to avoid file bloat
            if len(existing) > 5000:
                existing = existing[-5000:]

            SCAN_LOG.write_text(json.dumps(existing, indent=2))

        except Exception as e:
            log.warning("scan_log_write_failed", error=str(e))

    # ------------------------------------------------------------------ #
    #  Main execution                                                      #
    # ------------------------------------------------------------------ #

    def execute_opportunity(self,
                            opp:          Opportunity,
                            strategy:     BaseStrategy,
                            market_state: Optional[MarketState],
                            bankroll:     float) -> Optional[str]:
        risk = self.config.get("engine", {}).get("risk", {})

        # Gate 1: daily P&L
        daily_pnl     = self.state.get_daily_pnl()
        max_daily_loss = bankroll * float(risk.get("max_daily_loss_pct", 0.05))
        if daily_pnl < -max_daily_loss:
            self.log_opportunity(opp, False, "daily_loss_limit")
            return None

        # Gate 2: open positions
        open_count = self.state.get_open_position_count()
        max_open   = int(risk.get("max_open_positions", 50))
        if open_count >= max_open:
            self.log_opportunity(opp, False, "max_positions")
            return None

        # Gate 3: size
        size_usdc = strategy.size(opp, bankroll, self.config)
        if size_usdc < 1.0:
            self.log_opportunity(opp, False, "size_too_small")
            return None

        # Gate 4: DRY_RUN — log and record but don't submit
        if self.dry_run:
            trade_id  = f"DRY_{uuid.uuid4().hex[:10].upper()}"
            buy_price = opp.metadata.get("buy_price", opp.win_probability)
            shares    = round(size_usdc / buy_price, 4) if buy_price > 0 else 0
            fee_usdc  = strategy.calc_fee(buy_price) * buy_price * size_usdc

            record = TradeRecord(
                trade_id        = trade_id,
                strategy        = opp.strategy,
                market_id       = opp.market_id,
                market_question = opp.market_question,
                side            = opp.action.replace("BUY_", ""),
                price           = buy_price,
                shares          = shares,
                cost_usdc       = size_usdc,
                fee_usdc        = fee_usdc,
                status          = "open",
                entry_time      = datetime.now(timezone.utc).isoformat(),
                notes           = (f"DRY score={opp.score:.3f} "
                                   f"edge={opp.edge:.4f} "
                                   f"p={opp.win_probability:.3f}"),
            )
            self.state.log_trade(record)
            self.log_opportunity(opp, True, "")

            log.info("dry_run_trade",
                     trade_id=trade_id,
                     strategy=opp.strategy,
                     question=opp.market_question[:60],
                     size=size_usdc,
                     edge=opp.edge,
                     probability=opp.win_probability,
                     days_left=round(opp.time_to_resolution_sec / 86400, 1))
            return trade_id

        # Gate 5: live fee re-fetch
        token_id     = opp.metadata.get("token_id", "")
        fee_rate_bps = self._get_live_fee_bps(market_state, token_id)

        # Submit real order
        self.log_opportunity(opp, True, "")
        return self._submit_order(opp, strategy, size_usdc, fee_rate_bps)

    def check_and_settle(self, position: dict) -> bool:
        """Check if an open position has resolved and settle it."""
        try:
            market_id = position.get("market_id", "")
            token_id  = position.get("metadata", {}).get("token_id", "")
            side      = position.get("side", "YES")

            if not token_id:
                return False

            clob = self._get_clob_client()
            if not clob:
                return False

            market         = clob.get_market(token_id)
            resolved_price = float(market.get("lastTradedPrice", -1))
            is_resolved    = (market.get("closed", False)
                              or resolved_price in (0.0, 1.0)
                              or market.get("gameStatus") == "resolved")

            if not is_resolved:
                return False

            won      = (resolved_price >= 0.99 if side == "YES"
                        else resolved_price <= 0.01)
            outcome  = "win" if won else "loss"
            pnl_usdc = (position.get("shares", 0) - position.get("cost_usdc", 0)
                        - position.get("fee_usdc", 0) if won
                        else -position.get("cost_usdc", 0)
                             - position.get("fee_usdc", 0))

            self.state.mark_resolved(market_id, outcome, pnl_usdc)
            log.info("position_settled",
                     market_id=market_id, outcome=outcome, pnl=pnl_usdc)
            return True

        except Exception as e:
            log.warning("settle_failed",
                        market_id=position.get("market_id"), error=str(e))
            return False

    # ------------------------------------------------------------------ #
    #  Internals                                                           #
    # ------------------------------------------------------------------ #

    def _get_live_fee_bps(self, market_state: Optional[MarketState],
                           token_id: str) -> int:
        if market_state and not market_state.is_stale(MARKET_STALENESS_SEC):
            return market_state.fee_rate_bps
        try:
            clob   = self._get_clob_client()
            detail = clob.get_market(token_id) if clob else {}
            bps    = int(detail.get("feeRateBps", 0))
            if bps > 0:
                return bps
        except Exception:
            pass
        return market_state.fee_rate_bps if market_state else 200

    def _get_clob_client(self):
        if self._clob is None:
            try:
                from py_clob_client.client import ClobClient
                from py_clob_client.clob_types import ApiCreds
                creds = ApiCreds(
                    api_key        = os.environ["POLYMARKET_API_KEY"],
                    api_secret     = os.environ["POLYMARKET_API_SECRET"],
                    api_passphrase = os.environ["POLYMARKET_PASSPHRASE"],
                )
                self._clob = ClobClient(
                    host           = "https://clob.polymarket.com",
                    chain_id       = 137,
                    private_key    = os.environ["POLYMARKET_PRIVATE_KEY"],
                    creds          = creds,
                    signature_type = 0,
                    funder         = os.environ["POLYMARKET_WALLET_ADDRESS"],
                )
            except Exception as e:
                log.error("clob_client_init_failed", error=str(e))
                return None
        return self._clob

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def _submit_order(self, opp, strategy, size_usdc, fee_rate_bps):
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            clob      = self._get_clob_client()
            token_id  = opp.metadata.get("token_id", "")
            buy_price = opp.metadata.get("buy_price", opp.win_probability)
            shares    = round(size_usdc / buy_price, 2) if buy_price > 0 else 0

            if shares < 1.0 or not token_id:
                return None

            order_args = OrderArgs(
                token_id     = token_id,
                price        = round(buy_price, 4),
                size         = shares,
                side         = "BUY",
                fee_rate_bps = fee_rate_bps,
                nonce        = int(time.time() * 1000),
            )
            signed   = clob.create_order(order_args)
            response = clob.post_order(signed, OrderType.GTC)
            if not response or not response.get("orderID"):
                return None

            order_id = response["orderID"]
            fee_usdc = strategy.calc_fee(buy_price) * buy_price * size_usdc
            self.state.log_trade(TradeRecord(
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
                entry_time      = datetime.now(timezone.utc).isoformat(),
                notes           = f"LIVE score={opp.score:.3f}",
            ))
            log.info("order_submitted", order_id=order_id,
                     strategy=opp.strategy, size=size_usdc)
            return order_id

        except Exception as e:
            log.error("order_submit_failed", error=str(e))
            raise
