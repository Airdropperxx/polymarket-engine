"""
strategies/s10_near_resolution.py — Near-resolution harvest.

Filters: min_probability <= price <= max_probability (excludes resolved markets at 1.0)
Category comes from MarketState.category (fixed in data_engine).
"""

from __future__ import annotations
import math
import structlog
from strategies.base import BaseStrategy, Opportunity, Resolution
from engines.data_engine import MarketState

log = structlog.get_logger(component="s10_near_resolution")


class S10NearResolution(BaseStrategy):
    name = "s10_near_resolution"

    def scan(self, markets: list, groups: dict, config: dict) -> list[Opportunity]:
        cfg = config.get("s10_near_resolution", {})

        max_minutes = int(cfg.get("max_minutes_remaining", 10080))
        max_seconds = max_minutes * 60          # compare seconds to seconds
        min_prob    = float(cfg.get("min_probability",     0.85))
        max_prob    = float(cfg.get("max_probability",     0.989))  # exclude resolved
        min_volume  = float(cfg.get("min_volume_24h",      100.0))
        max_spread  = float(cfg.get("max_spread",          0.05))
        min_edge    = float(cfg.get("min_edge_after_fees", 0.001))

        opps = []
        for market in markets:
            if market.seconds_to_resolution <= 0:
                continue
            if market.seconds_to_resolution > max_seconds:
                continue
            if market.volume_24h < min_volume:
                continue

            # Find high-probability side — exclude already-resolved (>= max_prob)
            if min_prob <= market.yes_price <= max_prob:
                side, buy_price = "YES", market.yes_ask
                probability, token_id = market.yes_price, market.yes_token_id
                spread = market.yes_ask - market.yes_bid
            elif min_prob <= market.no_price <= max_prob:
                side, buy_price = "NO", market.no_ask
                probability, token_id = market.no_price, market.no_token_id
                spread = market.no_ask - market.no_bid
            else:
                continue

            if spread > max_spread:
                continue

            fee  = self.calc_fee(buy_price) * buy_price
            edge = probability - buy_price - fee
            if edge < min_edge:
                continue

            opps.append(Opportunity(
                strategy              = self.name,
                market_id             = market.market_id,
                market_question       = market.question,
                action                = f"BUY_{side}",
                edge                  = round(edge, 5),
                win_probability       = round(probability, 4),
                max_payout            = 1.0,
                time_to_resolution_sec = market.seconds_to_resolution,
                metadata={
                    "token_id":     token_id,
                    "buy_price":    round(buy_price, 4),
                    "probability":  round(probability, 4),
                    "spread":       round(spread, 4),
                    "fee":          round(fee, 6),
                    "volume_24h":   market.volume_24h,
                    "minutes_left": market.seconds_to_resolution // 60,
                    "days_left":    round(market.seconds_to_resolution / 86400, 2),
                    "category":     market.category,   # from data_engine
                    "fee_rate_bps": market.fee_rate_bps,
                    "end_date":     market.end_date_iso,
                },
            ))

        log.info("s10_scan_complete",
                 markets_scanned=len(markets),
                 opportunities_found=len(opps),
                 max_seconds=max_seconds,
                 min_prob=min_prob,
                 max_prob=max_prob)
        return opps

    def score(self, opp: Opportunity, config: dict) -> float:
        meta    = opp.metadata
        cfg     = config.get("s10_near_resolution", {})
        max_sec = int(cfg.get("max_minutes_remaining", 10080)) * 60

        time_score = min(1.0, max(0.0, 1.0 - opp.time_to_resolution_sec / max(max_sec, 1)))
        prob_score = min(1.0, max(0.0, (opp.win_probability - 0.85) / 0.15))
        edge_score = min(1.0, opp.edge / 0.05)
        vol        = meta.get("volume_24h", 100)
        vol_score  = min(1.0, math.log10(max(vol, 100)) / math.log10(100000))

        cat_bonus = {
            "finance":     0.08, "crypto":     0.06,
            "sports":      0.04, "tech":       0.03,
            "other":       0.00, "politics":  -0.02,
            "geopolitics":-0.04,
        }.get(meta.get("category", "other"), 0.0)

        raw = (time_score * 0.35 + prob_score * 0.30
             + edge_score * 0.20 + vol_score  * 0.15 + cat_bonus)
        return round(min(1.0, max(0.0, raw)), 4)

    def size(self, opp: Opportunity, bankroll: float, config: dict) -> float:
        cfg       = config.get("s10_near_resolution", {})
        max_pct   = float(cfg.get("max_position_pct", 0.40))
        kf        = float(cfg.get("kelly_fraction",   0.25))
        buy_price = opp.metadata.get("buy_price", opp.win_probability)
        b = (1.0 - buy_price) / buy_price if buy_price > 0 else 0
        p = opp.win_probability
        kelly = max(0, (b * p - (1 - p)) / b) if b > 0 else 0
        return round(max(1.0, min(max_pct * bankroll, kelly * kf * bankroll)), 2)

    def on_resolve(self, trade: dict, outcome: str, config: dict) -> Resolution:
        cost   = trade.get("cost_usdc", 0.0)
        fee    = trade.get("fee_usdc", 0.0)
        shares = trade.get("shares", 0.0)
        meta   = trade.get("metadata", {})
        won    = outcome == "win"
        payout = shares if won else 0.0
        pnl    = payout - cost - fee
        roi    = pnl / cost if cost > 0 else 0.0
        return Resolution(
            trade_id=trade.get("trade_id",""), market_id=trade.get("market_id",""),
            won=won, cost_usdc=round(cost,4), payout_usdc=round(payout,4),
            pnl_usdc=round(pnl,4), roi=round(roi,4), strategy=self.name,
            notes=f"{'WIN' if won else 'LOSS'}: {meta.get('category','?')} "
                  f"p={meta.get('probability',0):.2f} ROI={roi:.1%}",
            lessons=[f"S10 {'WIN' if won else 'LOSS'}: {meta.get('category','?')} "
                     f"p={meta.get('probability',0):.2f} {meta.get('days_left',0):.1f}d ROI={roi:.1%}"],
        )
