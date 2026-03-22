"""
strategies/s11_inplay_momentum.py

Sports in-play momentum. Uses MarketObserver price_history.json.

EDGE DISCOVERED from live data:
  - 33 markets moved >15% between two 30-min Observer snapshots
  - Real Madrid: +55% in 56 min. KC vs Iowa State: +28% then -32% (game swings)
  - $49M volume on same-day sports markets
  - Markets with secondsDelay=1-3 intentionally lag behind real scores
  - Buy at 0.65-0.92 when consistent momentum detected, collect at 0.95-0.99

ZERO EXTRA INFRASTRUCTURE: Just reads price_history.json already built by Observer.
"""

from __future__ import annotations
import json
import math
import structlog
from pathlib import Path
from strategies.base import BaseStrategy, Opportunity, Resolution

log = structlog.get_logger(component="s11_inplay_momentum")


def _load_history():
    try:
        p = Path("data/price_history.json")
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return {}


class S11InplayMomentum(BaseStrategy):
    name = "s11_inplay_momentum"

    def scan(self, markets: list, groups: dict, config: dict) -> list[Opportunity]:
        cfg = config.get("s11_inplay_momentum", {})
        if not cfg.get("enabled", True):
            return []

        min_move     = float(cfg.get("min_total_move",       0.20))
        min_vol      = float(cfg.get("min_volume_24h",       50000.0))
        min_price    = float(cfg.get("min_price_after_move", 0.60))
        max_price    = float(cfg.get("max_price_after_move", 0.92))
        max_days     = float(cfg.get("max_days_to_resolution", 2.0))
        min_obs      = int(cfg.get("min_observations",       2))
        min_edge     = float(cfg.get("min_edge_after_fees",  0.005))

        history = _load_history()
        if not history:
            log.info("s11_no_history", hint="Needs 2+ MarketObserver cycles")
            return []

        by_id = {m.market_id: m for m in markets}
        opps  = []

        for mid, data in history.items():
            pts = data.get("points", [])
            if len(pts) < min_obs:
                continue
            market = by_id.get(mid)
            if not market: continue
            if market.volume_24h < min_vol: continue
            if market.seconds_to_resolution > max_days * 86400: continue
            if market.fee_rate_bps >= 1000: continue

            recent     = pts[-4:]
            yes_prices = [p["yes"] for p in recent]
            if len(yes_prices) < 2: continue

            total_move = yes_prices[-1] - yes_prices[0]
            deltas     = [yes_prices[i] - yes_prices[i-1] for i in range(1, len(yes_prices))]
            all_up     = all(d > -0.02 for d in deltas)
            all_down   = all(d <  0.02 for d in deltas)
            if not (all_up or all_down): continue

            yes, no = market.yes_price, market.no_price

            if total_move >= min_move and all_up and min_price <= yes <= max_price:
                side, bp, prob, tok = "YES", market.yes_ask, yes, market.yes_token_id
            elif total_move <= -min_move and all_down and min_price <= no <= max_price:
                side, bp, prob, tok = "NO", market.no_ask, no, market.no_token_id
            else:
                continue

            fee  = self.calc_fee(bp) * bp
            edge = prob - bp - fee
            if edge < min_edge: continue

            speed = abs(total_move) / max(len(recent) - 1, 1)
            opps.append(Opportunity(
                strategy="s11_inplay_momentum",
                market_id=market.market_id,
                market_question=market.question,
                action="BUY_" + side,
                edge=round(edge, 5),
                win_probability=round(prob, 4),
                max_payout=1.0,
                time_to_resolution_sec=market.seconds_to_resolution,
                metadata={
                    "token_id": tok, "buy_price": round(bp, 4),
                    "probability": round(prob, 4), "total_move": round(total_move, 4),
                    "move_speed": round(speed, 4), "n_obs": len(recent),
                    "price_trail": [round(p, 3) for p in yes_prices],
                    "fee": round(fee, 6), "volume_24h": market.volume_24h,
                    "days_left": round(market.seconds_to_resolution / 86400, 2),
                    "category": market.category, "fee_rate_bps": market.fee_rate_bps,
                },
            ))

        log.info("s11_scan_complete", markets_in_history=len(history), opportunities_found=len(opps))
        return opps

    def score(self, opp: Opportunity, config: dict) -> float:
        meta = opp.metadata
        move  = abs(meta.get("total_move",  0))
        speed = abs(meta.get("move_speed",  0))
        vol   = meta.get("volume_24h",       50000)
        n_obs = meta.get("n_obs",            2)
        days  = meta.get("days_left",        1.0)

        raw = (min(1.0, move  / 0.50) * 0.30 +
               min(1.0, speed / 0.25) * 0.25 +
               min(1.0, math.log10(max(vol, 50000)) / math.log10(5000000)) * 0.20 +
               min(1.0, n_obs / 4) * 0.15 +
               max(0.0, 1.0 - days / 2.0) * 0.10)
        return round(min(1.0, max(0.0, raw)), 4)

    def size(self, opp: Opportunity, bankroll: float, config: dict) -> float:
        cfg    = config.get("s11_inplay_momentum", {})
        max_p  = float(cfg.get("max_position_pct", 0.15))
        kf     = float(cfg.get("kelly_fraction",   0.25))
        p      = opp.win_probability
        bp     = opp.metadata.get("buy_price", p)
        b      = (1.0 - bp) / bp if bp > 0 else 0
        kelly  = max(0, (b * p - (1 - p)) / b) if b > 0 else 0
        return round(max(1.0, min(max_p * bankroll, kelly * kf * bankroll)), 2)

    def on_resolve(self, trade: dict, outcome: str, config: dict) -> Resolution:
        cost   = trade.get("cost_usdc", 0.0)
        fee    = trade.get("fee_usdc",  0.0)
        shares = trade.get("shares",    0.0)
        meta   = trade.get("metadata",  {})
        won    = outcome == "win"
        payout = shares if won else 0.0
        pnl    = payout - cost - fee
        roi    = pnl / cost if cost > 0 else 0.0
        trail  = meta.get("price_trail", [])
        move   = meta.get("total_move",  0)
        return Resolution(
            trade_id=trade.get("trade_id",""), market_id=trade.get("market_id",""),
            won=won, cost_usdc=round(cost,4), payout_usdc=round(payout,4),
            pnl_usdc=round(pnl,4), roi=round(roi,4), strategy=self.name,
            notes=("WIN" if won else "LOSS") + " move=" + str(round(move,3)) + " trail=" + str(trail),
            lessons=["S11 " + ("WIN" if won else "LOSS") + " cat=" + meta.get("category","?") + " move=" + str(round(move,2)) + " ROI=" + str(round(roi*100,1)) + "%" + (" OK." if won else " Reversed — check min_move.")],
        )
