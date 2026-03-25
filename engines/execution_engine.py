"""
engines/execution_engine.py

The ONLY engine that submits orders.

FIXES (v_fix1):
  - check_and_settle: uses Gamma API via trade_analytics — no CLOB, no token_id needed
    Dry trades previously ALWAYS returned False (token_id not stored in DB)
  - fee_usdc: now calc_fee(buy_price) * size_usdc  (canonical formula)
  - execute_opportunity: stores edge, ev, kelly in notes for review engine
  - Resolution now logs full P&L math on every settled trade
"""
from __future__ import annotations
import json, os, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from strategies.base import BaseStrategy, Opportunity
from engines.data_engine import DataEngine, MarketState
from engines.state_engine import StateEngine, TradeRecord
from engines.trade_analytics import (
    calc_shares, calc_actual_cost, calc_fee_usdc, calc_pnl,
    calc_expected_value, calc_kelly_fraction, calc_edge,
    fetch_market_resolution, is_market_resolved,
)

log = structlog.get_logger(component="execution_engine")
SCAN_LOG             = Path("data/scan_log.json")
MARKET_STALENESS_SEC = 300
SCAN_LOG_MAX_ENTRIES = 20000



def _build_trade_notes(opp, edge: float, ev: float, kelly: float) -> str:
    """Build trade notes string. For S1 NegRisk, includes leg market IDs for resolution."""
    base = (f"DRY score={opp.score:.4f} edge={edge:.4f} "
            f"p={opp.win_probability:.4f} ev={ev:.4f} kelly={kelly:.4f}")
    # For NegRisk arb: store individual leg market IDs so resolution can find them
    # (the group_id stored as market_id is not queryable via Gamma API)
    legs = opp.metadata.get("legs", [])
    if legs:
        leg_ids = ",".join(str(leg.get("market_id","")) for leg in legs if leg.get("market_id"))
        if leg_ids:
            base += f" leg_ids={leg_ids}"
    return base

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

    # ── Opportunity logging ────────────────────────────────────────────────────

    def log_opportunity(self, opp: Opportunity, executed: bool,
                        reason_skipped: str = "") -> None:
        try:
            SCAN_LOG.parent.mkdir(parents=True, exist_ok=True)
            entries = []
            if SCAN_LOG.exists():
                try: entries = json.loads(SCAN_LOG.read_text())
                except: entries = []
            buy_price = opp.metadata.get("buy_price", opp.win_probability)
            ev    = calc_expected_value(opp.win_probability, buy_price)
            kelly = calc_kelly_fraction(opp.win_probability, buy_price)
            is_negrisk_log = opp.action == "BUY_ALL_YES"
            buy_price_log = (float(opp.metadata.get("total_ask", opp.win_probability))
                             if is_negrisk_log
                             else opp.metadata.get("buy_price", opp.win_probability))
            if is_negrisk_log:
                shares_log  = 1   # 1 share-set per NegRisk arb
                actual_cost = round(buy_price_log, 4)
            else:
                shares_log  = int(1.0 / buy_price_log) if buy_price_log > 0 else 0
                actual_cost = round(shares_log * buy_price_log, 4)
            entries.append({
                "ts":             datetime.now(timezone.utc).isoformat(),
                "strategy":       opp.strategy,
                "market_id":      opp.market_id,
                "question":       opp.market_question,
                "action":         opp.action,
                "score":          round(opp.score, 4),
                "edge":           round(opp.edge, 4),
                "win_probability": round(opp.win_probability, 4),
                "buy_price":      round(buy_price, 4),
                "ev":             round(ev, 4),
                "kelly_frac":     round(kelly, 4),
                "ttl_sec":        opp.time_to_resolution_sec,
                "executed":       executed,
                "reason_skipped": reason_skipped,
                # Trade sizing detail
                "budget_usdc":    1.0,
                "shares":         shares_log,
                "actual_cost":    actual_cost,
                "unused_cash":    round(1.0 - actual_cost, 4),
                # Strategy metadata for pattern analysis
                "category":       opp.metadata.get("category", ""),
                "volume_24h":     opp.metadata.get("volume_24h", 0),
                "spread":         opp.metadata.get("spread", 0),
                "fee":            opp.metadata.get("fee", 0),
                "minutes_left":   opp.metadata.get("minutes_left", round(opp.time_to_resolution_sec/60,1)),
                "days_left":      round(opp.time_to_resolution_sec/86400, 2),
                "num_legs":       opp.metadata.get("num_legs", 1),
                "total_ask":      opp.metadata.get("total_ask", 0),
                "observer_flagged": opp.metadata.get("observer_flagged", False),
            })
            if len(entries) > SCAN_LOG_MAX_ENTRIES:
                entries = entries[-SCAN_LOG_MAX_ENTRIES:]
            SCAN_LOG.write_text(json.dumps(entries, indent=2))
        except Exception as e:
            log.warning("log_opportunity_failed", error=str(e))

    # ── Execution ──────────────────────────────────────────────────────────────

    def execute_opportunity(self,
                            opp:          Opportunity,
                            strategy:     BaseStrategy,
                            market_state: Optional[MarketState],
                            bankroll:     float) -> Optional[str]:
        risk = self.config.get("engine", {}).get("risk", {})

        # Gate 1: daily P&L limit
        daily_pnl      = self.state.get_daily_pnl()
        max_daily_loss = bankroll * float(risk.get("max_daily_loss_pct", 0.99))
        if daily_pnl < -max_daily_loss:
            self.log_opportunity(opp, False, "daily_loss_limit")
            return None

        # Gate 2: open position count
        open_count = self.state.get_open_position_count()
        max_open   = int(risk.get("max_open_positions", 50))
        if open_count >= max_open:
            self.log_opportunity(opp, False, "max_positions")
            return None

        # Gate 3: position size — always $1 in dry-run for uniform comparison
        size_usdc = 1.0 if self.dry_run else strategy.size(opp, bankroll, self.config)
        if not self.dry_run and size_usdc < 1.0:
            self.log_opportunity(opp, False, "size_too_small")
            return None

        # Gate 4: DRY_RUN
        if self.dry_run:
            trade_id  = f"DRY_{uuid.uuid4().hex[:10].upper()}"
            # For S1 NegRisk (BUY_ALL_YES): effective price = total_ask across all legs
            # For S10/S11 (BUY_YES/BUY_NO): price is the single-leg ask
            is_negrisk  = opp.action == "BUY_ALL_YES"
            if is_negrisk:
                buy_price   = float(opp.metadata.get("total_ask", opp.win_probability))
                num_legs    = int(opp.metadata.get("num_legs", 1))
                # For NegRisk: 1 "share set" = buy 1 share of each leg
                # cost = total_ask (sum of all leg ask prices), shares = 1 set
                shares      = 1
                actual_cost = round(buy_price, 6)   # cost of 1 share set
                fee_usdc    = calc_fee_usdc(actual_cost, buy_price / max(num_legs, 1))
            else:
                buy_price   = opp.metadata.get("buy_price", opp.win_probability)
                # Integer shares: floor($1 / price), actual cost = shares * price
                shares      = calc_shares(size_usdc, buy_price)
                actual_cost = calc_actual_cost(shares, buy_price)
                fee_usdc    = calc_fee_usdc(actual_cost, buy_price)
            edge        = calc_edge(opp.win_probability, buy_price)
            ev          = calc_expected_value(opp.win_probability, buy_price)
            kelly       = calc_kelly_fraction(opp.win_probability, buy_price)
            open_ts     = datetime.now(timezone.utc).isoformat()
            record = TradeRecord(
                trade_id        = trade_id,
                strategy        = opp.strategy,
                market_id       = opp.market_id,
                market_question = opp.market_question,
                side            = opp.action.replace("BUY_", ""),
                price           = buy_price,
                shares          = shares,
                cost_usdc       = actual_cost,
                fee_usdc        = fee_usdc,
                status          = "open",
                entry_time      = open_ts,
                notes           = _build_trade_notes(opp, edge, ev, kelly)
                                  + f" open_price_ts={open_ts} open_price={buy_price}",
            )
            self.state.log_trade(record)
            self.log_opportunity(opp, True, "")
            log.info("dry_run_trade",
                     trade_id=trade_id, strategy=opp.strategy,
                     question=opp.market_question[:60], side=record.side,
                     price=buy_price, shares=shares, cost=actual_cost,
                     fee=fee_usdc, edge=edge, ev=ev, kelly=kelly,
                     days_left=round(opp.time_to_resolution_sec / 86400, 1))
            return trade_id

        # Gate 5: live fee re-fetch
        token_id     = opp.metadata.get("token_id", "")
        fee_rate_bps = self._get_live_fee_bps(market_state, token_id)
        self.log_opportunity(opp, True, "")
        return self._submit_order(opp, strategy, size_usdc, fee_rate_bps)

    # ── Settlement: Gamma API (dry + live) ────────────────────────────────────

    def check_and_settle(self, position: dict) -> bool:
        """
        Check if an open position has resolved and settle it.
        FIXED: uses Gamma REST API by market_id — no CLOB, no token_id needed.
        Previously ALL dry trades failed here because token_id was never stored.
        """
        try:
            market_id = position.get("market_id", "")
            side      = position.get("side", "YES")
            shares    = float(position.get("shares",   0) or 0)
            cost_usdc = float(position.get("cost_usdc", 0) or 0)
            fee_usdc  = float(position.get("fee_usdc",  0) or 0)
            trade_id  = position.get("trade_id", "")
            is_dry    = str(trade_id).startswith("DRY")

            if not market_id:
                log.warning("settle_no_market_id", trade_id=trade_id)
                return False

            # Try Gamma API first (works for all trades)
            # Pass trade notes so hex NegRisk group IDs can extract leg market IDs
            trade_notes = position.get("notes", "") or ""
            ms = fetch_market_resolution(market_id, trade_notes=trade_notes)

            if ms is None and not is_dry:
                # Gamma unavailable — fallback to CLOB for live trades only
                return self._settle_via_clob(position)

            if ms is None:
                return False  # Gamma down, dry trade — retry next cycle

            if not is_market_resolved(ms):
                return False  # Still live

            resolved_yes_price = ms.get("yes_price", -1.0)
            if resolved_yes_price < 0:
                log.warning("settle_no_price", market_id=market_id)
                return False

            outcome, net_pnl = calc_pnl(side, shares, cost_usdc, fee_usdc, resolved_yes_price)
            if outcome == "open":
                return False

            self.state.mark_resolved(market_id, outcome, net_pnl)
            roi = round(net_pnl / cost_usdc * 100, 2) if cost_usdc > 0 else 0
            log.info("position_settled",
                     trade_id=trade_id, market_id=market_id, side=side,
                     outcome=outcome, yes_price=resolved_yes_price,
                     shares=shares, cost=cost_usdc, fee=fee_usdc,
                     pnl=net_pnl, roi_pct=roi, is_dry=is_dry)
            return True

        except Exception as e:
            log.error("settle_failed", market_id=position.get("market_id"), error=str(e))
            return False

    def _settle_via_clob(self, position: dict) -> bool:
        """Fallback: settle live trade via CLOB."""
        try:
            market_id = position.get("market_id", "")
            side      = position.get("side", "YES")
            shares    = float(position.get("shares",   0) or 0)
            cost_usdc = float(position.get("cost_usdc", 0) or 0)
            fee_usdc  = float(position.get("fee_usdc",  0) or 0)
            token_id  = ""
            # Try to extract token_id from notes
            for part in (position.get("notes","") or "").split():
                if part.startswith("token_id="):
                    token_id = part.split("=",1)[1]
            if not token_id: return False
            clob = self._get_clob_client()
            if not clob: return False
            mkt  = clob.get_market(token_id)
            rp   = float(mkt.get("lastTradePrice", -1))
            if rp < 0 or not (mkt.get("closed") or rp in (0.0,1.0)): return False
            outcome, net_pnl = calc_pnl(side, shares, cost_usdc, fee_usdc, rp)
            if outcome == "open": return False
            self.state.mark_resolved(market_id, outcome, net_pnl)
            log.info("position_settled_clob", market_id=market_id, outcome=outcome, pnl=net_pnl)
            return True
        except Exception as e:
            log.error("clob_settle_failed", error=str(e))
            return False

    # ── CLOB helpers ──────────────────────────────────────────────────────────

    def _get_live_fee_bps(self, market_state, token_id: str) -> int:
        if market_state and not market_state.is_stale(MARKET_STALENESS_SEC):
            return market_state.fee_rate_bps
        try:
            clob   = self._get_clob_client()
            detail = clob.get_market(token_id) if clob else {}
            bps    = int(detail.get("feeRateBps", 0))
            if bps > 0: return bps
        except: pass
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
                    host="https://clob.polymarket.com", chain_id=137,
                    private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
                    creds=creds, signature_type=0,
                    funder=os.environ["POLYMARKET_WALLET_ADDRESS"],
                )
            except Exception as e:
                log.error("clob_init_failed", error=str(e)); return None
        return self._clob

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def _submit_order(self, opp, strategy, size_usdc, fee_rate_bps):
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            clob      = self._get_clob_client()
            token_id  = opp.metadata.get("token_id", "")
            buy_price = opp.metadata.get("buy_price", opp.win_probability)
            shares      = calc_shares(size_usdc, buy_price)
            actual_cost_live = calc_actual_cost(shares, buy_price)
            fee_usdc  = calc_fee_usdc(actual_cost_live, buy_price)
            if not clob or not token_id:
                log.error("submit_no_clob_or_token"); return None
            order  = clob.create_order(OrderArgs(price=buy_price, size=shares, side="BUY", token_id=token_id))
            result = clob.post_order(order, OrderType.GTC)
            oid    = result.get("orderID", "")
            trade_id = f"LIVE_{oid[:12]}" if oid else f"LIVE_{uuid.uuid4().hex[:10].upper()}"
            edge = calc_edge(opp.win_probability, buy_price)
            ev   = calc_expected_value(opp.win_probability, buy_price)
            self.state.log_trade(TradeRecord(
                trade_id=trade_id, strategy=opp.strategy, market_id=opp.market_id,
                market_question=opp.market_question, side=opp.action.replace("BUY_",""),
                price=buy_price, shares=shares, cost_usdc=size_usdc, fee_usdc=fee_usdc,
                status="open", entry_time=datetime.now(timezone.utc).isoformat(),
                notes=f"LIVE order_id={oid} score={opp.score:.4f} edge={edge:.4f} ev={ev:.4f}",
            ))
            log.info("order_placed", trade_id=trade_id, order_id=oid, size=size_usdc,
                     price=buy_price, shares=shares, edge=edge, ev=ev)
            return trade_id
        except Exception as e:
            log.error("submit_order_failed", error=str(e)); raise
