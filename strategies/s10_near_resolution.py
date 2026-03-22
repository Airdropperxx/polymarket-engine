"""
strategies/s10_near_resolution.py -- Near-resolution harvest.

Based on live Polymarket data (March 2026):
  - Apple AAPL close above $210 end of March: 98%
  - Israel launches Lebanon ground offensive by March 31: 98%  
  - Bitcoin price milestones already hit: 100%
  - Many end-of-month binary markets at 90-99%

These markets have very low fee impact at high p, but the key insight is:
  The REAL edge comes from the SPREAD, not a theoretical probability gap.
  We BUY at the ASK price. If yes_price=0.98 and we can buy at ask=0.982,
  we pay 0.982 to collect 1.00 -- that's 1.8% gross, minus fees.

For S10 to find opportunities at the current $100 bankroll level:
  1. Lower min_probability to 0.85 (many real markets sit here)
  2. Widen time window to 7 days (end-of-month markets are the target)
  3. Lower min_edge to 0.001 (fees are tiny at p>0.85)
  4. Use volume_24h > 100 USDC (not 500)

KEY FIX: seconds_to_resolution is compared against max_minutes * 60.
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

        max_minutes  = int(cfg.get("max_minutes_remaining", 10080))  # 7 days default
        max_seconds  = max_minutes * 60   # CORRECT: compare seconds to seconds
        min_prob     = float(cfg.get("min_probability", 0.85))
        min_volume   = float(cfg.get("min_volume_24h", 100.0))
        max_spread   = float(cfg.get("max_spread", 0.05))
        min_edge     = float(cfg.get("min_edge_after_fees", 0.001))

        opps = []

        for market in markets:
            # Must have time remaining
            if market.seconds_to_resolution <= 0:
                continue

            # Must be within time window
            if market.seconds_to_resolution > max_seconds:
                continue

            # Must have volume
            if market.volume_24h < min_volume:
                continue

            # Find the high-probability side
            if market.yes_price >= min_prob:
                side        = "YES"
                buy_price   = market.yes_ask
                probability = market.yes_price
                token_id    = market.yes_token_id
                spread      = market.yes_ask - market.yes_bid
            elif market.no_price >= min_prob:
                side        = "NO"
                buy_price   = market.no_ask
                probability = market.no_price
                token_id    = market.no_token_id
                spread      = market.no_ask - market.no_bid
            else:
                continue

            # Spread filter
            if spread > max_spread:
                continue

            # Edge calculation: fee is position-level, multiply by buy_price for per-share
            fee  = self.calc_fee(buy_price) * buy_price
            edge = probability - buy_price - fee

            if edge < min_edge:
                continue

            days_left    = market.seconds_to_resolution / 86400
            minutes_left = market.seconds_to_resolution // 60

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
                    "minutes_left": minutes_left,
                    "days_left":    round(days_left, 2),
                    "category":     market.category,
                    "fee_rate_bps": market.fee_rate_bps,
                },
            ))

        log.info("s10_scan_complete",
                 markets_scanned=len(markets),
                 max_seconds=max_seconds,
                 min_prob=min_prob,
                 opportunities_found=len(opps))
        return opps

    def score(self, opp: Opportunity, config: dict) -> float:
        meta = opp.metadata
        cfg  = config.get("s10_near_resolution", {})

        max_sec    = int(cfg.get("max_minutes_remaining", 10080)) * 60
        # Time urgency: closer to resolution = higher score
        time_frac  = 1.0 - (opp.time_to_resolution_sec / max(max_sec, 1))
        time_score = min(1.0, max(0.0, time_frac))

        # Probability score: maps [0.85, 1.0] -> [0.0, 1.0]
        prob_score = min(1.0, max(0.0, (opp.win_probability - 0.85) / 0.15))

        # Edge score
        edge_score = min(1.0, max(0.0, opp.edge / 0.05))

        # Volume score (log scale)
        vol = meta.get("volume_24h", 100)
        vol_score = min(1.0, math.log10(max(vol, 100)) / math.log10(100000))

        # Category bonus: crypto/stock price markets resolve cleanly
        cat_bonus = {
            "crypto":   0.05,
            "sports":   0.03,
            "politics": -0.02,
            "other":    0.0,
        }.get(meta.get("category", "other"), 0.0)

        raw = (time_score * 0.35
             + prob_score * 0.30
             + edge_score * 0.20
             + vol_score  * 0.15
             + cat_bonus)

        return round(min(1.0, max(0.0, raw)), 4)

    def size(self, opp: Opportunity, bankroll: float, config: dict) -> float:
        cfg        = config.get("s10_near_resolution", {})
        max_pct    = float(cfg.get("max_position_pct", 0.15))
        kelly_frac = float(cfg.get("kelly_fraction", 0.25))

        buy_price = opp.metadata.get("buy_price", opp.win_probability)
        b         = (1.0 - buy_price) / buy_price if buy_price > 0 else 0
        p         = opp.win_probability
        q         = 1.0 - p
        kelly     = (b * p - q) / b if b > 0 else 0.0
        position  = kelly * kelly_frac * bankroll

        return round(max(1.0, min(max_pct * bankroll, position)), 2)

    def on_resolve(self, trade: dict, outcome: str, config: dict) -> Resolution:
        cost     = trade.get("cost_usdc", 0.0)
        fee      = trade.get("fee_usdc", 0.0)
        shares   = trade.get("shares", 0.0)
        meta     = trade.get("metadata", {})
        won      = (outcome == "win")
        payout   = shares * 1.0 if won else 0.0
        pnl      = payout - cost - fee
        roi      = pnl / cost if cost > 0 else 0.0
        cat      = meta.get("category", "unknown")
        prob     = meta.get("probability", 0)
        days     = meta.get("days_left", 0)

        lessons = []
        if won:
            lessons.append(
                f"S10 WIN: {cat} p={prob:.2f} {days:.1f}d left ROI={roi:.1%}"
            )
        else:
            lessons.append(
                f"S10 LOSS: {cat} p={prob:.2f} {days:.1f}d left. "
                "Consider raising min_probability or reducing days window."
            )

        return Resolution(
            trade_id    = trade.get("trade_id", ""),
            market_id   = trade.get("market_id", ""),
            won         = won,
            cost_usdc   = round(cost, 4),
            payout_usdc = round(payout, 4),
            pnl_usdc    = round(pnl, 4),
            roi         = round(roi, 4),
            strategy    = self.name,
            notes       = f"{'WIN' if won else 'LOSS'}: {cat} p={prob:.2f} ROI={roi:.1%}",
            lessons     = lessons,
        )
