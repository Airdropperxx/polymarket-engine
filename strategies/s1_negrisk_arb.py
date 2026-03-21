"""
strategies/s1_negrisk_arb.py
=============================
NegRisk multi-outcome arbitrage.

In a multi-outcome market (e.g. "Who wins the 2026 Vermont Governor race?"),
EXACTLY ONE candidate must win. If the sum of YES ask prices across all candidates
is less than $1.00, buying YES on every candidate guarantees a $1.00 payout for
less than $1.00 cost. This is pure arbitrage — zero directional risk.

win_probability = 1.0 always.

Two arb types:
  LONG  (buy_all_yes):  sum(YES ask prices) < 1.00  → buy all YES legs
  SHORT (sell_all_yes): sum(YES bid prices) > 1.00  → sell all YES legs (or buy all NO)
"""

from __future__ import annotations

import structlog

from strategies.base import BaseStrategy, Opportunity, Resolution
from engines.data_engine import MarketState

log = structlog.get_logger()


class NegRiskArbStrategy(BaseStrategy):
    """S1: NegRisk multi-outcome arbitrage. win_probability always 1.0."""

    name = "s1_negrisk_arb"

    def scan(
        self,
        markets: list,
        negrisk_groups: dict,
        config: dict,
    ) -> list[Opportunity]:
        min_spread = config.get("min_spread_after_fees", 0.03)
        opps: list[Opportunity] = []

        for group_id, group_markets in negrisk_groups.items():
            if len(group_markets) < 2:
                continue

            # ── LONG ARB: sum of YES ask prices < $1.00 ──────────────────────
            yes_asks = [m.yes_price for m in group_markets]
            total_cost = sum(yes_asks)

            if total_cost < 1.0:
                gross_spread = 1.0 - total_cost
                # Fee only applies to the winning leg. Use avg price as the fee estimate;
                # the actual winning leg price is unknown in advance.
                avg_p = total_cost / len(group_markets)
                fee   = self.calc_fee(avg_p)
                net   = gross_spread - fee

                if net >= min_spread:
                    opps.append(Opportunity(
                        strategy=self.name,
                        market_id=group_markets[0].market_id,  # Use first market's ID
                        market_question=(
                            f"NegRisk LONG: {group_markets[0].question[:60]}…"
                        ),
                        action="buy_all_yes",
                        edge=net,
                        win_probability=1.0,         # guaranteed — one MUST win
                        max_payout=1.0 / total_cost,
                        time_to_resolution_sec=min(
                            m.seconds_to_resolution for m in group_markets
                        ),
                        metadata={
                            "group_id":     group_id,
                            "markets":      [m.market_id for m in group_markets],
                            "yes_prices":   yes_asks,
                            "total_cost":   round(total_cost, 6),
                            "gross_spread": round(gross_spread, 6),
                            "fee_estimate": round(fee, 6),
                        },
                    ))

            # ── SHORT ARB: sum of YES bid prices > $1.00 ─────────────────────
            yes_bids      = [m.yes_bid for m in group_markets]
            total_revenue = sum(yes_bids)

            if total_revenue > 1.0 + min_spread:
                gross_spread = total_revenue - 1.0
                # Short arb: sell YES on all (or equivalently mint + sell).
                # Fee on each sell leg — use avg bid as estimate.
                avg_bid = total_revenue / len(group_markets)
                fee     = self.calc_fee(avg_bid) * len(group_markets)
                net     = gross_spread - fee

                if net >= min_spread:
                    opps.append(Opportunity(
                        strategy=self.name,
                        market_id=group_markets[0].market_id,  # Use first market's ID
                        market_question=(
                            f"NegRisk SHORT: {group_markets[0].question[:60]}…"
                        ),
                        action="sell_all_yes",
                        edge=net,
                        win_probability=1.0,
                        max_payout=total_revenue,
                        time_to_resolution_sec=min(
                            m.seconds_to_resolution for m in group_markets
                        ),
                        metadata={
                            "group_id":       group_id,
                            "markets":        [m.market_id for m in group_markets],
                            "yes_bids":       yes_bids,
                            "total_revenue":  round(total_revenue, 6),
                            "gross_spread":   round(gross_spread, 6),
                            "fee_estimate":   round(fee, 6),
                        },
                    ))

        log.info("s1.scan_done",
                 groups_scanned=len(negrisk_groups),
                 opportunities=len(opps))
        return opps

    def score(self, opp: Opportunity, config: dict) -> float:
        # NegRisk arb is always high priority: score = min(edge × 2, 1.0)
        # A 50% spread → score 1.0; a 3% minimum → score 0.06 (above threshold)
        return min(opp.edge * 2.0, 1.0)

    def size(self, opp: Opportunity, bankroll: float, config: dict) -> float:
        # win_probability = 1.0 → Kelly → bet everything. Use half-Kelly as safety.
        return self.calc_kelly_size(
            win_probability=1.0,
            payout_ratio=opp.max_payout,
            bankroll=bankroll,
            kelly_fraction=config.get("kelly_fraction", 0.50),
            max_position_pct=config.get("max_position_pct", 0.20),
        )

    def on_resolve(self, resolution: Resolution) -> dict:
        lessons: list[str] = []
        if not resolution.won:
            # NegRisk arb should NEVER lose (win_prob = 1.0).
            # If it did, execution failed (partial fill, slippage, or API error).
            lessons.append(
                f"S1 UNEXPECTED LOSS on {resolution.market_id[:40]}. "
                f"ROI={resolution.roi:.2%}. "
                "Check: was buy_all_yes fully filled across all legs? "
                "Was the group still active when all orders landed?"
            )
        return {
            "won":     resolution.won,
            "roi":     resolution.roi,
            "notes":   resolution.notes,
            "lessons": lessons,
        }
