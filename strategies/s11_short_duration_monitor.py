"""
strategies/s11_short_duration_monitor.py
======================================
Same-day / short-duration market monitor.

Watches for sports/political markets that resolve within 24 hours.
Alerts when they appear - no execution since we need to verify liquidity first.

No latency infrastructure needed - these markets resolve in hours,
plenty of time for normal API execution.
"""

from __future__ import annotations

import structlog

from strategies.base import BaseStrategy, Opportunity, Resolution
from engines.data_engine import MarketState

log = structlog.get_logger()


class ShortDurationMonitor(BaseStrategy):
    """
    S11: Same-day / short-duration market monitor.
    
    Finds markets resolving within 24 hours.
    Alerts trader to manually verify liquidity before execution.
    
    This is a MONITOR strategy - it finds opportunities but doesn't execute.
    Execution requires manual verification of order book liquidity.
    """

    name = "s11_short_duration_monitor"

    def scan(
        self,
        markets: list,
        negrisk_groups: dict,
        config: dict,
    ) -> list[Opportunity]:
        max_hours = config.get("max_hours", 24)
        min_volume = config.get("min_volume_24h", 100)
        min_edge = config.get("min_edge_after_fees", 0.02)

        opps: list[Opportunity] = []

        for market in markets:
            hours_to_resolve = market.seconds_to_resolution / 3600
            
            # Skip markets outside our window
            if hours_to_resolve > max_hours or hours_to_resolve < 0:
                continue
            
            # Check volume threshold
            if market.volume_24h < min_volume:
                continue

            # Evaluate YES side
            if market.yes_price >= 0.50:
                opp = self._create_opportunity(market, "buy_yes", market.yes_price, hours_to_resolve)
                if opp:
                    opps.append(opp)
            
            # Evaluate NO side
            if market.no_price >= 0.50:
                opp = self._create_opportunity(market, "buy_no", market.no_price, hours_to_resolve)
                if opp:
                    opps.append(opp)

        log.info("s11.scan_done",
                 markets_checked=len(markets),
                 opportunities=len(opps))
        return opps

    def _create_opportunity(
        self,
        market: MarketState,
        action: str,
        price: float,
        hours_to_resolve: float,
    ) -> Opportunity | None:
        """Create an opportunity if edge is sufficient."""
        fee = self.calc_fee(price)
        
        if action == "buy_yes":
            edge = (1.0 - price) - fee
        else:  # buy_no
            edge = price - fee
        
        if edge < 0.01:  # 1% minimum edge
            return None
        
        return Opportunity(
            strategy=self.name,
            market_id=market.market_id,
            market_question=market.question,
            action=action,
            edge=edge,
            win_probability=price,
            max_payout=1.0 / price if price > 0 else 1.0,
            time_to_resolution_sec=int(hours_to_resolve * 3600),
            metadata={
                "hours_to_resolve": round(hours_to_resolve, 2),
                "category": market.category,
                "volume_24h": market.volume_24h,
            },
        )

    def score(self, opp: Opportunity, config: dict) -> float:
        """
        Score based on:
        - Time urgency (shorter = higher score)
        - Volume (higher = higher score)
        - Edge (higher = higher score)
        """
        hours = opp.metadata.get("hours_to_resolve", 24)
        time_score = max(0, 1.0 - (hours / 24.0))
        
        volume = opp.metadata.get("volume_24h", 0)
        volume_score = min(volume / 1000.0, 1.0)
        
        return opp.edge * 0.5 + time_score * 0.3 + volume_score * 0.2

    def size(self, opp: Opportunity, bankroll: float, config: dict) -> float:
        # Conservative sizing for short-duration trades
        return self.calc_kelly_size(
            win_probability=opp.win_probability,
            payout_ratio=opp.max_payout,
            bankroll=bankroll,
            kelly_fraction=0.25,  # Conservative
            max_position_pct=0.10,  # 10% max
        )

    def on_resolve(self, resolution: Resolution) -> dict:
        lessons: list[str] = []
        if not resolution.won:
            lessons.append(
                f"S11 loss on {resolution.market_id[:40]}. "
                f"ROI={resolution.roi:.2%}. "
                "Short-duration markets can be volatile."
            )
        return {
            "won": resolution.won,
            "roi": resolution.roi,
            "notes": resolution.notes,
            "lessons": lessons,
        }
