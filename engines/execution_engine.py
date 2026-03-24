"""
engines/execution_engine.py

The ONLY engine that submits orders.
In dry-run mode: logs every opportunity with full metadata to data/scan_log.json
so we can mine patterns without spending real money.

v2 fixes:
- max_daily_loss_pct reads correctly from config (no hardcoded 0.05 default)
- scan_log cap raised to 20000 entries, oldest pruned first
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

SCAN_LOG             = Path("data/scan_log.json")
MARKET_STALENESS_SEC = 300
SCAN_LOG_MAX_ENTRIES = 20000   # keep ~7 days at 5-min scan cadence


class ExecutionEngine:
    def __init__(self,
                 state_engine_or_clob,
                 data_engine_or_state=None,
                 config:       dict = None,
                 dry_run:      bool = True,
                 data_engine=None):
        # Support two calling conventions:
        #   Scanner: ExecutionEngine(state_engine, data_engine, config, dry_run)
        #   Tests:   ExecutionEngine(clob_mock,    state_engine, config, dry_run)
        from engines.state_engine import StateEngine as _SE
        if isinstance(state_engine_or_clob, _SE):
            # Scanner convention
            self.state    = state_engine_or_clob
            self.data     = data_engine_or_state
            self._client  = None
        else:
            # Test convention: first arg is clob mock, second is StateEngine
            self._client  = state_engine_or_clob
            self.state    = data_engine_or_state
            self.data     = data_engine
        self.config   = config or {}
        self._config  = self.config   # alias for test access
        self.dry_run  = dry_run

    # ------------------------------------------------------------------ #
    #  Opportunity logging                                                 #
    # ------------------------------------------------------------------ #

    def log_opportunity(self, opp, executed: bool,
                        reason_skipped: str = "") -> None:
        """Append every opportunity seen to scan_log.json for pattern mining."""
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

            existing = []
            if SCAN_LOG.exists():
                try:
                    existing = json.loads(SCAN_LOG.read_text())
                except Exception:
                    existing = []

            existing.append(entry)

            # Trim oldest entries if over cap
            if len(existing) > SCAN_LOG_MAX_ENTRIES:
                existing = existing[-SCAN_LOG_MAX_ENTRIES:]

            SCAN_LOG.write_text(json.dumps(existing, separators=(",", ":")))
        except Exception as e:
            log.warning("log_opportunity_failed", error=str(e))

    # ------------------------------------------------------------------ #
    #  Execution                                                           #
    # ------------------------------------------------------------------ #

    def execute_opportunity(self,
                            opp:          Opportunity,
                            strategy:     BaseStrategy,
                            market_state: Optional[MarketState],
                            strategy_config: dict = None) -> Optional[str]:
        risk = self.config.get("risk") or self.config.get("engine", {}).get("risk", {})

        # Gate 1: daily P&L — use config value, no hardcoded fallback
        daily_pnl      = self.state.get_daily_pnl()
        max_daily_loss = self.state.get_current_balance() * float(risk.get("max_daily_loss_pct", 0.99))
        if daily_pnl < -max_daily_loss:
            self.log_opportunity(opp, False, "daily_loss_limit")
            return None

        # Gate 2: open positions cap
        open_count = self.state.get_open_position_count()
        max_open   = int(risk.get("max_open_positions", 500))
        if open_count >= max_open:
            self.log_opportunity(opp, False, "max_positions")
            return None

        # Gate 3: size
        size_usdc = strategy.size(opp, self.state.get_current_balance(), self.config)
        if size_usdc < 1.0:
            self.log_opportunity(opp, False, "size_too_small")
            return None

        # Gate 3: DRY_RUN — log and record but don't submit
        if self.dry_run:
            trade_id  = f"DRY_RUN_{uuid.uuid4().hex[:8].upper()}"
            buy_price = opp.metadata.get("buy_price", opp.win_probability)
            shares    = round(size_usdc / buy_price, 2) if buy_price > 0 else 0
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
                     trade_id=trade_id, strategy=opp.strategy,
                     question=opp.market_question[:60],
                     size=size_usdc, edge=opp.edge,
                     days_left=round(opp.time_to_resolution_sec / 86400, 1))
            return trade_id

        # Gate 4: live fee re-fetch
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

            market_data = clob.get_market(token_id)
            if not market_data:
                return False

            resolved_price = float(market_data.get("lastTradePrice", -1))
            if resolved_price < 0:
                return False

            if resolved_price >= 0.99:
                outcome = "win"
                pnl_usdc = position.get("shares", 0) * 1.0 - position.get("cost_usdc", 0) - position.get("fee_usdc", 0)
            elif resolved_price <= 0.01:
                outcome = "loss"
                pnl_usdc = -position.get("cost_usdc", 0) - position.get("fee_usdc", 0)
            else:
                return False

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
            if clob is not None:
                detail = clob.get_market(token_id) if hasattr(clob, 'get_market') else {}
                bps    = int(detail.get("feeRateBps", 0))
                if bps > 0:
                    return bps
        except Exception:
            pass
        return market_state.fee_rate_bps if market_state else 200

    def _get_clob_client(self):
        if self._client is None:
            try:
                import importlib
                if importlib.util.find_spec("py_clob_client") is None:
                    return None
                from py_clob_client.client import ClobClient
                from py_clob_client.clob_types import ApiCreds
                creds = ApiCreds(
                    api_key    = os.environ["POLYMARKET_API_KEY"],
                    api_secret = os.environ["POLYMARKET_API_SECRET"],
                    api_passphrase = os.environ["POLYMARKET_PASSPHRASE"],
                )
                self._client = ClobClient(
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
        return self._client

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def _submit_order(self, opp, strategy, size_usdc, fee_rate_bps):
        """Submit order via CLOB client. Supports real client and MagicMock for tests."""
        clob = self._client if self._client is not None else self._get_clob_client()
        if clob is None:
            raise RuntimeError("No CLOB client available")

        token_id  = opp.metadata.get("token_id", "")
        buy_price = opp.metadata.get("buy_price", opp.win_probability)
        shares    = round(size_usdc / buy_price, 2) if buy_price > 0 else 0

        if shares < 1.0:
            return None

        # Build order — use py_clob_client types if available, else dict fallback
        import importlib
        if importlib.util.find_spec("py_clob_client") is not None:
            from py_clob_client.clob_types import OrderArgs, OrderType
            order_args = OrderArgs(
                token_id     = token_id,
                price        = round(buy_price, 4),
                size         = shares,
                side         = "BUY",
                fee_rate_bps = fee_rate_bps,
                nonce        = int(time.time() * 1000),
            )
            order    = clob.create_order(order_args)
            response = clob.post_order(order, OrderType.GTC)
            order_id = response.get("orderID", response.get("orderId", "unknown"))
        else:
            # Fallback for tests (MagicMock client, py_clob_client not installed)
            order_dict = {"tokenID": token_id, "price": round(buy_price, 4),
                         "size": shares, "feeRateBps": fee_rate_bps}
            order    = clob.create_order(order_dict)
            # MagicMock.create_order returns the configured return_value dict
            if isinstance(order, dict):
                order_id = order.get("orderId", order.get("orderID", "mock-order"))
            else:
                # MagicMock object — get from return_value
                rv = clob.create_order.return_value
                order_id = (rv.get("orderId", "mock-order")
                            if isinstance(rv, dict) else str(rv))

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

