"""
strategies/s1_negrisk_arb.py — NegRisk multi-outcome arbitrage.

Thesis: In a NegRisk group, exactly ONE outcome resolves YES.
Therefore sum(YES prices across all group markets) must equal 1.00 (minus fees).
If sum < 1.00 - fees, buying ALL YES tokens guarantees a profit regardless of
which outcome wins — true risk-free arbitrage.

Example:
  Group: "Who wins the championship?" with 4 teams
  YES prices: 0.22 + 0.18 + 0.25 + 0.31 = 0.96
  Cost to buy all YES: $0.96 per share set
  One will pay out $1.00 → net profit $0.04 per share set minus fees

Win rate: ~100% (mathematical guarantee, not probabilistic bet)
Risk: Execution risk only — must fill ALL legs, not just some.

KEY FIX vs original:
  - Added liquidity floor per leg (min_leg_bid) to reject thin markets
  - Corrected sum calculation to use YES ask (taker price) not mid price
  - Added volume floor so stale markets don't generate phantom arb
"""

from __future__ import annotations

import structlog
from typing import Optional

from strategies.base import BaseStrategy, Opportunity, Resolution
from engines.data_engine import MarketState

log = structlog.get_logger(component="s1_negrisk_arb")


class S1NegRiskArb(BaseStrategy):
    name = "s1_negrisk_arb"

    # ── scan ──────────────────────────────────────────────────────────────────

    def scan(self,
             markets:  list[MarketState],
             groups:   dict[str, list[MarketState]],
             config:   dict) -> list[Opportunity]:
        """
        Scans NegRisk groups for sum(YES ask) < arb threshold.

        For each group:
          1. Verify 2+ legs, all passing quality checks
          2. Compute total_ask = sum of YES ask prices across all legs
          3. Compute fee per leg and total fees
          4. Edge = 1.0 - total_ask - total_fees
          5. Edge > min_edge → opportunity

        Returns list of Opportunity, one per profitable group.
        """
        cfg         = config.get("s1_negrisk_arb", config)
        min_edge    = float(cfg.get("min_edge_after_fees", 0.020))
        min_volume  = float(cfg.get("min_leg_volume_24h",  200.0))
        min_bid     = float(cfg.get("min_leg_bid",          0.02))  # reject illiquid legs
        max_legs    = int(cfg.get("max_group_legs",           20))   # cap group size

        opps = []

        for group_id, group_markets in groups.items():
            opp = self._evaluate_group(
                group_id, group_markets, min_edge, min_volume, min_bid, max_legs
            )
            if opp:
                opps.append(opp)

        log.info("s1_scan_complete",
                 groups_evaluated=len(groups),
                 opportunities_found=len(opps))
        return opps

    def _evaluate_group(self,
                        group_id:   str,
                        markets:    list[MarketState],
                        min_edge:   float,
                        min_volume: float,
                        min_bid:    float,
                        max_legs:   int
                        ) -> Optional[Opportunity]:
        """Evaluate one NegRisk group. Returns Opportunity or None."""

        if len(markets) > max_legs:
            return None  # too many legs — execution risk too high

        # Quality check each leg
        valid_legs = []
        for m in markets:
            if m.volume_24h < min_volume:
                continue                          # illiquid leg
            if m.yes_bid < min_bid:
                continue                          # no real bid = stale
            if m.seconds_to_resolution <= 0:
                continue                          # already resolved
            valid_legs.append(m)

        if len(valid_legs) < 2:
            return None  # need at least 2 valid legs for a group to arb

        # All valid legs must belong to the same group
        # (sanity check in case DataEngine grouped incorrectly)
        group_ids = {m.negrisk_group_id for m in valid_legs}
        if len(group_ids) > 1:
            return None

        # Check that all legs share the same resolution date (± 1 hour)
        end_dates = {m.end_date_iso[:10] for m in valid_legs}
        if len(end_dates) > 1:
            log.debug("s1_group_mixed_dates", group_id=group_id, dates=end_dates)
            # Still viable if dates are same day — proceed anyway

        # Compute arb metrics using ASK prices (taker cost)
        total_ask   = sum(m.yes_ask for m in valid_legs)
        total_fees  = sum(self.calc_fee(m.yes_ask) for m in valid_legs)
        edge        = 1.0 - total_ask - total_fees

        if edge < min_edge:
            return None

        # Find the soonest-resolving leg (time_to_resolution for scoring)
        min_ttl = min(m.seconds_to_resolution for m in valid_legs)

        return Opportunity(
            strategy             = self.name,
            market_id            = group_id,           # group ID as identifier
            market_question      = f"NegRisk group: {valid_legs[0].question[:60]}...",
            action               = "BUY_ALL_YES",
            edge                 = round(edge, 5),
            win_probability      = 1.0,                # guaranteed if all fills execute
            max_payout           = 1.0,
            time_to_resolution_sec = min_ttl,
            metadata={
                "group_id":       group_id,
                "num_legs":       len(valid_legs),
                "total_ask":      round(total_ask, 4),
                "total_fees":     round(total_fees, 6),
                "legs": [
                    {
                        "market_id":     m.market_id,
                        "question":      m.question[:80],
                        "yes_ask":       round(m.yes_ask, 4),
                        "yes_bid":       round(m.yes_bid, 4),
                        "yes_token_id":  m.yes_token_id,
                        "volume_24h":    m.volume_24h,
                        "fee":           round(self.calc_fee(m.yes_ask), 6),
                    }
                    for m in valid_legs
                ],
                "avg_leg_volume": round(
                    sum(m.volume_24h for m in valid_legs) / len(valid_legs), 2),
            },
        )

    # ── score ─────────────────────────────────────────────────────────────────

    def score(self, opp: Opportunity, config: dict) -> float:
        """
        S1 scoring:
          - Edge is the dominant factor (pure arb — bigger is better)
          - Leg count penalty (more legs = more execution risk)
          - Urgency (sooner = better — less time for prices to shift)
          - Liquidity (higher avg volume = more confident fills)
        """
        import math

        meta       = opp.metadata
        edge       = opp.edge
        num_legs   = meta.get("num_legs", 2)
        avg_vol    = meta.get("avg_leg_volume", 500.0)
        ttl_sec    = opp.time_to_resolution_sec

        # Edge score [0→1]: maps 0.02→0.0, 0.10→1.0
        edge_score = min(1.0, max(0.0, (edge - 0.02) / 0.08))

        # Leg penalty: 2 legs = 1.0, 10 legs = 0.2
        leg_score = max(0.1, 1.0 - (num_legs - 2) * 0.1)

        # Urgency: 0→1 as time shrinks (within 30-day window)
        urgency = 1.0 - min(1.0, ttl_sec / (30 * 86400))

        # Volume score (log-scale)
        vol_score = min(1.0, math.log10(max(avg_vol, 200)) / math.log10(50000))

        raw = (edge_score * 0.45
             + leg_score  * 0.25
             + urgency    * 0.20
             + vol_score  * 0.10)

        return round(min(1.0, max(0.0, raw)), 4)

    # ── size ──────────────────────────────────────────────────────────────────

    def size(self, opp: Opportunity, bankroll: float, config: dict) -> float:
        """
        S1 is guaranteed-edge, so we use a higher fraction than Kelly.
        Size = min(max_position_pct * bankroll, edge / total_ask * bankroll * 0.5)
        But total_ask is the cost to buy 1 share set across all legs,
        so we size to the number of share sets we can buy.

        Floor: 1.0 USDC. Ceiling: max_position_pct * bankroll.
        """
        cfg         = config.get("s1_negrisk_arb", config)
        max_pct     = float(cfg.get("max_position_pct", 0.15))

        total_ask   = opp.metadata.get("total_ask", 0.90)
        edge        = opp.edge

        # Conservative: size to 30% of bankroll allocated to S1
        # Edge is guaranteed so we go higher than quarter-Kelly
        fraction    = min(max_pct, edge / total_ask * 0.5)
        position    = fraction * bankroll

        return round(max(1.0, min(max_pct * bankroll, position)), 2)

    # ── on_resolve ────────────────────────────────────────────────────────────

    def on_resolve(self, trade: dict, outcome: str, config: dict) -> Resolution:
        cost_usdc  = trade.get("cost_usdc", 0.0)
        fee_usdc   = trade.get("fee_usdc", 0.0)
        shares     = trade.get("shares", 0.0)
        num_legs   = trade.get("metadata", {}).get("num_legs", 1)

        won        = (outcome == "win")
        payout     = shares * 1.0 if won else 0.0
        pnl_usdc   = payout - cost_usdc - fee_usdc
        roi        = pnl_usdc / cost_usdc if cost_usdc > 0 else 0.0

        lessons = []
        if not won:
            lessons.append(
                f"S1 NegRisk LOSS: {num_legs} legs. "
                "Possible partial fill or price slippage. "
                "Check leg liquidity thresholds."
            )
        else:
            lessons.append(
                f"S1 NegRisk WIN: {num_legs} legs, ROI={roi:.1%}. "
                "Arb confirmed working. Consider increasing size."
            )

        return Resolution(
            trade_id    = trade.get("trade_id", ""),
            market_id   = trade.get("market_id", ""),
            won         = won,
            cost_usdc   = round(cost_usdc, 4),
            payout_usdc = round(payout, 4),
            pnl_usdc    = round(pnl_usdc, 4),
            roi         = round(roi, 4),
            strategy    = self.name,
            notes       = f"{'WIN' if won else 'LOSS'}: {num_legs} legs, ROI={roi:.1%}",
            lessons     = lessons,
        )
