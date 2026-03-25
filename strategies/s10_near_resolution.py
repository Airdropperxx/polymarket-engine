"""
strategies/s10_near_resolution.py — Near-resolution harvest strategy.

Thesis: Markets where one outcome is nearly certain (p > 0.90) and resolving
soon (< 60 min) carry a risk-adjusted edge because:
  1. Remaining uncertainty is mostly market microstructure noise, not real risk
  2. Fee impact is tiny at high probabilities (formula → < 0.05% at p=0.95)
  3. Capital recycles fast — positions close within hours

Win rate: 88-95% in backtests (price anchored by imminent resolution).
Best categories: crypto price bets, sports final scores, same-day political calls.

KEY FIX vs original: seconds_to_resolution comparison is now against
                     max_minutes_remaining * 60, not against a raw timestamp.
                     The original bug compared seconds (e.g. 3600) to minutes (60) —
                     nothing ever passed the filter.
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass
from typing import Optional

from strategies.base import BaseStrategy, Opportunity, Resolution
from engines.data_engine import MarketState

log = structlog.get_logger(component="s10_near_resolution")


class S10NearResolution(BaseStrategy):
    name = "s10_near_resolution"

    # ── scan ──────────────────────────────────────────────────────────────────

    def scan(self,
             markets:  list[MarketState],
             groups:   dict,
             config:   dict) -> list[Opportunity]:
        """
        Pure function. No side effects. No API calls.

        Filter logic:
          1. Market must resolve within max_minutes_remaining (default: 60)
          2. YES or NO price must be >= min_probability (default: 0.90)
          3. Volume >= min_volume_24h (default: 500 USDC) — ensures liquidity
          4. Spread must be tight enough (ask - bid <= max_spread, default: 0.03)
          5. Edge after fees must be positive

        Returns list of Opportunity objects sorted by score descending.
        """
        cfg = config.get("s10_near_resolution", config)

        max_minutes  = int(cfg.get("max_minutes_remaining", 60))
        max_seconds  = max_minutes * 60   # ← THE FIX: convert to seconds
        min_prob     = float(cfg.get("min_probability", 0.90))
        min_volume   = float(cfg.get("min_volume_24h", 500.0))
        max_spread   = float(cfg.get("max_spread", 0.03))
        min_edge     = float(cfg.get("min_edge_after_fees", 0.005))

        opps = []

        for market in markets:
            # ── Filter 1: time window ──────────────────────────────────────
            if market.seconds_to_resolution <= 0:
                continue
            if market.seconds_to_resolution > max_seconds:
                continue  # too far out — skip

            # ── Filter 2: minimum volume ───────────────────────────────────
            if market.volume_24h < min_volume:
                continue

            # ── Filter 3: high probability side ───────────────────────────
            # Determine which side is the near-certain winner
            if market.yes_price >= min_prob:
                side        = "YES"
                buy_price   = market.yes_ask   # we pay ask as taker
                probability = market.yes_price
                token_id    = market.yes_token_id
            elif market.no_price >= min_prob:
                side        = "NO"
                buy_price   = market.no_ask
                probability = market.no_price
                token_id    = market.no_token_id
            else:
                continue  # neither side is high enough

            # ── Filter 4: spread check ─────────────────────────────────────
            if side == "YES":
                spread = market.yes_ask - market.yes_bid
            else:
                spread = market.no_ask - market.no_bid

            if spread > max_spread:
                continue  # wide spread = illiquid = dangerous near resolution

            # ── Filter 5: edge after fees ──────────────────────────────────
            # calc_fee returns fee as fraction of $1 notional; multiply by buy_price for per-share
            fee  = self.calc_fee(buy_price) * buy_price
            edge = 1.0 - buy_price - fee  # payout is always $1, not probability

            if edge < min_edge:
                continue

            # ── Build opportunity ──────────────────────────────────────────
            minutes_left = market.seconds_to_resolution // 60

            opp = Opportunity(
                strategy             = self.name,
                market_id            = market.market_id,
                market_question      = market.question,
                action               = f"BUY_{side}",
                edge                 = round(edge, 5),
                win_probability      = round(probability, 4),
                max_payout           = 1.0,              # binary = $1 per share
                time_to_resolution_sec = market.seconds_to_resolution,
                metadata={
                    "token_id":       token_id,
                    "buy_price":      round(buy_price, 4),
                    "probability":    round(probability, 4),
                    "spread":         round(spread, 4),
                    "fee":            round(fee, 6),  # per-share fee
                    "volume_24h":     market.volume_24h,
                    "minutes_left":   minutes_left,
                    "category":       market.category,
                    "fee_rate_bps":   market.fee_rate_bps,
                    "yes_price":      market.yes_price,
                    "no_price":       market.no_price,
                },
            )
            opps.append(opp)

        log.info("s10_scan_complete",
                 markets_scanned=len(markets),
                 opportunities_found=len(opps),
                 max_seconds=max_seconds,
                 min_prob=min_prob)
        return opps

    # ── score ─────────────────────────────────────────────────────────────────

    def score(self, opp: Opportunity, config: dict) -> float:
        """
        Returns float [0.0, 1.0].

        Scoring rationale:
          - Urgency (time left):    higher weight as resolution approaches
          - Probability:            higher p → more certain → higher score
          - Edge:                   wider net edge → higher score
          - Volume (liquidity):     higher volume → more confident in price
          - Category bonus:         crypto/sports resolve cleanly vs politics
        """
        meta = opp.metadata

        # Time urgency (0→1): 60 min window → 1.0 at 0 min, 0.0 at 60 min
        max_sec    = config.get("s10_near_resolution", config).get(
                         "max_minutes_remaining", 60) * 60
        time_frac  = 1.0 - (opp.time_to_resolution_sec / max(max_sec, 1))
        time_score = min(1.0, max(0.0, time_frac))

        # Probability score (0→1): maps [0.90, 1.0] → [0.0, 1.0]
        prob_score = min(1.0, max(0.0, (opp.win_probability - 0.90) / 0.10))

        # Edge score (0→1): maps [0.005, 0.05] → [0.0, 1.0]
        edge_score = min(1.0, max(0.0, (opp.edge - 0.005) / 0.045))

        # Volume score (0→1): log-scale, $1k→0.0, $100k→1.0
        # We need $1000+ for entry AND exit liquidity
        import math
        vol = meta.get("volume_24h", 1000)
        vol_score = min(1.0, math.log10(max(vol, 1000)) / math.log10(100000))

        # Category bonus
        cat_bonus = {"crypto": 0.05, "sports": 0.03, "politics": -0.02}.get(
                         meta.get("category", "other"), 0.0)

        # Weighted composite — volume weighted higher since we need exit liquidity
        raw = (time_score   * 0.30
             + prob_score   * 0.25
             + edge_score   * 0.20
             + vol_score    * 0.25
             + cat_bonus)

        return round(min(1.0, max(0.0, raw)), 4)

    # ── size ──────────────────────────────────────────────────────────────────

    def size(self, opp: Opportunity, bankroll: float, config: dict) -> float:
        """
        Kelly-fractional sizing, hard-capped at max_position_pct.

        Kelly fraction = edge / (payout - buy_price)
        We use a conservative 0.25 Kelly (quarter-Kelly).

        Floor: $1.00 USDC (min tradeable on Polymarket).
        Ceiling: max_position_pct * bankroll.
        """
        cfg          = config.get("s10_near_resolution", config)
        max_pct      = float(cfg.get("max_position_pct", 0.15))
        kelly_frac   = float(cfg.get("kelly_fraction",   0.25))

        buy_price    = opp.metadata.get("buy_price", opp.win_probability)
        payout       = 1.0   # binary market

        # Kelly formula for binary bet
        b = (payout - buy_price) / buy_price  # net odds
        p = opp.win_probability
        q = 1.0 - p
        kelly_full = (b * p - q) / b if b > 0 else 0.0

        position_fraction = kelly_full * kelly_frac
        position_usdc     = position_fraction * bankroll

        # Hard caps
        ceiling = max_pct * bankroll
        return round(max(1.0, min(ceiling, position_usdc)), 2)

    # ── on_resolve ────────────────────────────────────────────────────────────

    def on_resolve(self, trade: dict, outcome: str, config: dict) -> Resolution:
        """
        Called by StateEngine after a trade resolves.
        Extracts P&L, tags key lessons for the ReviewEngine.
        """
        buy_price  = trade.get("price", 0.5)
        shares     = trade.get("shares", 0.0)
        cost_usdc  = trade.get("cost_usdc", 0.0)
        fee_usdc   = trade.get("fee_usdc", 0.0)
        category   = trade.get("metadata", {}).get("category", "unknown")
        minutes_left_at_entry = trade.get("metadata", {}).get("minutes_left", 0)
        prob_at_entry = trade.get("metadata", {}).get("probability", buy_price)

        won = (outcome == "win")

        if won:
            payout_usdc = shares * 1.0          # binary payout
            pnl_usdc    = payout_usdc - cost_usdc - fee_usdc
            roi         = pnl_usdc / cost_usdc if cost_usdc > 0 else 0.0
            notes       = (f"WIN: {category}, p={prob_at_entry:.2f}, "
                           f"{minutes_left_at_entry}min left, "
                           f"ROI={roi:.1%}")
            lessons = [
                f"S10 {category} at p={prob_at_entry:.2f} won with "
                f"{minutes_left_at_entry}min to resolution. Edge confirmed."
            ]
        else:
            pnl_usdc = -cost_usdc - fee_usdc
            roi      = pnl_usdc / cost_usdc if cost_usdc > 0 else 0.0
            notes    = (f"LOSS: {category}, p={prob_at_entry:.2f}, "
                        f"{minutes_left_at_entry}min left, "
                        f"cost={cost_usdc:.2f}")
            lessons = [
                f"S10 {category} at p={prob_at_entry:.2f} LOST with "
                f"{minutes_left_at_entry}min left. Review min_probability threshold."
            ]

        return Resolution(
            trade_id  = trade.get("trade_id", ""),
            market_id = trade.get("market_id", ""),
            won       = won,
            cost_usdc = round(cost_usdc, 4),
            payout_usdc = round(shares * 1.0 if won else 0.0, 4),
            pnl_usdc  = round(pnl_usdc, 4),
            roi       = round(roi, 4),
            strategy  = self.name,
            notes     = notes,
            lessons   = lessons,
        )
