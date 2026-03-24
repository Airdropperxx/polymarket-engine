"""
strategies/s1_negrisk_arb.py

NegRisk multi-outcome arbitrage. CRITICAL FIXES:
  1. Phantom arb filter: skip legs with price < 0.03 or > 0.97 (already resolved)
  2. total_ask sanity: must be 0.50-0.99 (real unresolved group)
  3. Skip 10% fee markets (takerBaseFee >= 1000)
"""

from __future__ import annotations
import math
import structlog
from typing import Optional
from strategies.base import BaseStrategy, Opportunity, Resolution
from engines.data_engine import MarketState

log = structlog.get_logger(component="s1_negrisk_arb")


class S1NegRiskArb(BaseStrategy):
    name = "s1_negrisk_arb"

    def scan(self, markets: list, groups: dict, config: dict) -> list[Opportunity]:
        cfg           = config.get("s1_negrisk_arb", {})
        min_edge      = float(cfg.get("min_edge_after_fees",  0.005))
        min_volume    = float(cfg.get("min_leg_volume_24h",   1000.0))  # $1k minimum
        min_bid       = float(cfg.get("min_leg_bid",          0.01))
        max_legs      = int(cfg.get("max_group_legs",         20))
        min_leg_price = float(cfg.get("min_leg_price",        0.03))
        max_leg_price = float(cfg.get("max_leg_price",        0.97))

        opps = []
        for group_id, group_markets in groups.items():
            opp = self._evaluate_group(
                group_id, group_markets, min_edge, min_volume,
                min_bid, max_legs, min_leg_price, max_leg_price)
            if opp:
                opps.append(opp)

        log.info("s1_scan_complete", groups_evaluated=len(groups), opportunities_found=len(opps))
        return opps

    def _evaluate_group(self, group_id, markets, min_edge, min_volume,
                        min_bid, max_legs, min_leg_price, max_leg_price):
        if len(markets) > max_legs:
            return None

        valid_legs = []
        for m in markets:
            if m.volume_24h < min_volume: continue
            if m.yes_bid < min_bid: continue
            if m.seconds_to_resolution <= 0: continue
            if m.yes_price < min_leg_price or m.yes_price > max_leg_price: continue
            if m.fee_rate_bps >= 1000: continue
            valid_legs.append(m)

        if len(valid_legs) < 2:
            return None

        total_ask  = sum(m.yes_ask for m in valid_legs)
        total_fees = sum(self.calc_fee(m.yes_ask) * m.yes_ask for m in valid_legs)
        edge       = 1.0 - total_ask - total_fees

        if edge < min_edge:
            return None
        if total_ask <= 0.50 or total_ask >= 1.0:
            return None

        min_ttl      = min(m.seconds_to_resolution for m in valid_legs)
        cat_counts   = {}
        for m in valid_legs:
            c = m.category or "other"
            cat_counts[c] = cat_counts.get(c, 0) + 1
        dominant_cat = max(cat_counts, key=cat_counts.get) if cat_counts else "other"
        avg_vol      = round(sum(m.volume_24h for m in valid_legs) / len(valid_legs), 2)

        return Opportunity(
            strategy             = "s1_negrisk_arb",
            market_id            = group_id,
            market_question      = "NegRisk: " + valid_legs[0].question[:70],
            action               = "BUY_ALL_YES",
            edge                 = round(edge, 5),
            win_probability      = 1.0,
            max_payout           = 1.0,
            time_to_resolution_sec = min_ttl,
            metadata = {
                "group_id":     group_id,
                "num_legs":     len(valid_legs),
                "total_ask":    round(total_ask, 4),
                "total_fees":   round(total_fees, 6),
                "category":     dominant_cat,
                "volume_24h":   avg_vol,
                "buy_price":    round(total_ask / len(valid_legs), 4),
                "fee":          round(total_fees, 6),
                "spread":       0.01,
                "fee_rate_bps": valid_legs[0].fee_rate_bps,
                "avg_leg_volume": avg_vol,
                "legs": [
                    {"market_id": m.market_id, "question": m.question[:80],
                     "yes_price": round(m.yes_price, 4), "yes_ask": round(m.yes_ask, 4),
                     "yes_token_id": m.yes_token_id, "volume_24h": m.volume_24h,
                     "category": m.category}
                    for m in valid_legs
                ],
            },
        )

    def score(self, opp: Opportunity, config: dict) -> float:
        meta      = opp.metadata
        total_ask = meta.get("total_ask", 0.9)
        avg_vol   = meta.get("avg_leg_volume", 50.0)
        num_legs  = meta.get("num_legs", 2)
        ttl_sec   = opp.time_to_resolution_sec

        ask_score  = max(0.0, 1.0 - abs(total_ask - 0.88) / 0.12)
        edge_score = min(1.0, max(0.0, (opp.edge - 0.005) / 0.10))
        leg_score  = max(0.1, 1.0 - (num_legs - 2) * 0.08)
        urgency    = 1.0 - min(1.0, ttl_sec / (7 * 86400))
        vol_score  = min(1.0, math.log10(max(avg_vol, 50)) / math.log10(500000))

        raw = edge_score*0.35 + ask_score*0.25 + leg_score*0.20 + urgency*0.10 + vol_score*0.10
        return round(min(1.0, max(0.0, raw)), 4)

    def size(self, opp: Opportunity, bankroll: float, config: dict) -> float:
        cfg       = config.get("s1_negrisk_arb", {})
        max_pct   = float(cfg.get("max_position_pct", 0.40))
        total_ask = opp.metadata.get("total_ask", 0.90)
        fraction  = min(max_pct, opp.edge / max(total_ask, 0.1) * 0.5)
        return round(max(1.0, min(max_pct * bankroll, fraction * bankroll)), 2)

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
            notes=("WIN" if won else "LOSS"),
            lessons=["S1 " + ("WIN" if won else "LOSS") + " category=" + meta.get("category","?") + " legs=" + str(meta.get("num_legs",0))],
        )
