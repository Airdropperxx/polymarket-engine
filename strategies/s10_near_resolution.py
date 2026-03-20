"""
strategies/s10_near_resolution.py
==================================
Near-resolution outcome harvesting.

Buys near-certain outcomes when a market is within max_minutes_remaining of resolution.
At high probabilities the dynamic fee approaches zero, so edge is nearly pure profit.

Key fix (v3.1): max_minutes_remaining in YAML must be > poll_interval + 15 min buffer.
  With 30-min GitHub Actions poll:  set to 60 in YAML.
  With 60-min poll:                 set to 90.
  With 5-min local cron:            set to 35.

Category-specific thresholds (sports = stricter due to late-reversal risk).
"""

from __future__ import annotations

import structlog

from strategies.base import BaseStrategy, Opportunity, Resolution
from engines.data_engine import MarketState

log = structlog.get_logger()


class NearResolutionStrategy(BaseStrategy):
    """S10: Near-resolution outcome harvesting."""

    name = "s10_near_resolution"

    def scan(
        self,
        markets: list,
        negrisk_groups: dict,
        config: dict,
    ) -> list[Opportunity]:
        default_min_p   = config.get("min_probability", 0.90)
        default_max_min = config.get("max_minutes_remaining", 60)
        min_edge        = config.get("min_edge_after_fees", 0.025)
        cat_rules       = config.get("category_rules", {})

        opps: list[Opportunity] = []

        for market in markets:
            # Get category-specific thresholds (fall back to defaults)
            cat_cfg = cat_rules.get(market.category, {})
            min_p   = cat_cfg.get("min_probability",      default_min_p)
            max_min = cat_cfg.get("max_minutes_remaining", default_max_min)

            # Skip markets outside the resolution window
            if market.seconds_to_resolution > max_min * 60:
                continue

            # Skip already-resolved markets
            if market.seconds_to_resolution < 0:
                continue

            # Check YES side
            opp = self._evaluate_side(
                market=market,
                p=market.yes_price,
                action="buy_yes",
                min_p=min_p,
                min_edge=min_edge,
            )
            if opp:
                opps.append(opp)

            # Check NO side
            opp = self._evaluate_side(
                market=market,
                p=market.no_price,
                action="buy_no",
                min_p=min_p,
                min_edge=min_edge,
            )
            if opp:
                opps.append(opp)

        log.info("s10.scan_done",
                 markets_checked=len(markets),
                 opportunities=len(opps))
        return opps

    def score(self, opp: Opportunity, config: dict) -> float:
        """
        score = edge × 0.4 + time_urgency × 0.3 + volume_score × 0.3

        time_urgency:  1.0 when 0 seconds left, 0.0 at max_minutes boundary
        volume_score:  1.0 at $10K daily volume, scales linearly below
        """
        max_secs      = config.get("max_minutes_remaining", 60) * 60
        time_urgency  = 1.0 - (opp.time_to_resolution_sec / max(max_secs, 1))
        time_urgency  = max(0.0, min(1.0, time_urgency))

        volume_24h    = opp.metadata.get("volume_24h", 0.0)
        volume_score  = min(volume_24h / 10_000.0, 1.0)

        return opp.edge * 0.4 + time_urgency * 0.3 + volume_score * 0.3

    def size(self, opp: Opportunity, bankroll: float, config: dict) -> float:
        return self.calc_kelly_size(
            win_probability=opp.win_probability,
            payout_ratio=opp.max_payout,
            bankroll=bankroll,
            kelly_fraction=config.get("kelly_fraction", 0.25),
            max_position_pct=config.get("max_position_pct", 0.15),
        )

    def on_resolve(self, resolution: Resolution) -> dict:
        lessons: list[str] = []
        if not resolution.won and resolution.roi < -0.05:
            lessons.append(
                f"S10 loss: {resolution.market_id[:40]} | "
                f"ROI={resolution.roi:.2%}. "
                "Review category threshold — consider tightening min_probability."
            )
        return {
            "won":     resolution.won,
            "roi":     resolution.roi,
            "notes":   resolution.notes,
            "lessons": lessons,
        }

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _evaluate_side(
        self,
        market: MarketState,
        p: float,
        action: str,
        min_p: float,
        min_edge: float,
    ) -> "Opportunity | None":
        """Evaluate one side (YES or NO) of a market for a near-resolution opportunity."""
        if p < min_p:
            return None

        fee  = self.calc_fee(p)
        edge = (1.0 - p) - fee      # profit per share if we win, minus fee

        if edge < min_edge:
            return None

        return Opportunity(
            strategy=self.name,
            market_id=market.market_id,
            market_question=market.question,
            action=action,
            edge=edge,
            win_probability=p,
            max_payout=1.0 / p,     # payout ratio: e.g. 1/0.94 ≈ 1.064
            time_to_resolution_sec=market.seconds_to_resolution,
            metadata={
                "category":   market.category,
                "fee_rate":   round(fee, 6),
                "volume_24h": market.volume_24h,
            },
        )
