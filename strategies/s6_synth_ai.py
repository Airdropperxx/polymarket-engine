"""
strategies/s6_synth_ai.py
=========================
Bittensor SN50 (Synth) signal integration strategy.

PREREQUISITE: Capital > $1,000. Synth API costs ~$200/month.

Queries Monte Carlo BTC/ETH/SOL price forecasts from Synth.
When Synth probability diverges >10% from Polymarket price:
enter maker order (zero fees).

Documented return: $2K → $4.2K in 4 weeks (110% ROI, 0 fees paid).
"""

import os
import time
from typing import Optional

import requests
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from strategies.base import BaseStrategy, Opportunity

log = structlog.get_logger()

SYNTH_API_BASE = "https://api.synth.bittensor.com"


class SynthAIStrategy(BaseStrategy):
    """
    Bittensor SN50 (Synth) signal integration.
    
    Fetches Monte Carlo price forecasts and compares to Polymarket prices.
    When divergence > 10%, enter maker order (GTD, not FOK) for zero fees.
    """

    name = "s6_synth_ai"

    def __init__(self):
        self._api_key = os.environ.get("SYNTH_API_KEY")
        self._cache = {}
        self._cache_ttl = 300

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def _fetch_synth_forecast(self, asset: str) -> Optional[dict]:
        """Fetch Synth forecast for given asset (BTC, ETH, SOL)."""
        if not self._api_key:
            log.warning("s6.no_api_key")
            return None

        cache_key = f"{asset}_{int(time.time() // self._cache_ttl)}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            resp = requests.get(
                f"{SYNTH_API_BASE}/forecast/{asset}",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=10,
            )

            if not resp.ok:
                log.warning("s6.api_error", status=resp.status_code, asset=asset)
                return None

            data = resp.json()
            self._cache[cache_key] = data
            log.info("s6.forecast_fetched", asset=asset, forecast=data)
            return data

        except Exception as e:
            log.error("s6.fetch_error", error=str(e), asset=asset)
            return None

    def _get_synth_probability(self, asset: str, direction: str) -> Optional[float]:
        """Get probability from Synth for asset going up or down."""
        forecast = self._fetch_synth_forecast(asset)
        if not forecast:
            return None

        try:
            prob_up = float(forecast.get("prob_up", 0.5))
            return prob_up if direction == "up" else (1 - prob_up)
        except (ValueError, TypeError):
            return None

    def scan(self, markets: list, negrisk_groups: dict, config: dict) -> list[Opportunity]:
        """
        Scan for markets where Synth diverges from Polymarket by > 10%.
        """
        opportunities = []
        divergence_threshold = config.get("s6_synth_ai", {}).get("min_divergence", 0.10)

        asset_map = {
            "btc": ["bitcoin", "btc"],
            "eth": ["ethereum", "eth"],
            "sol": ["solana", "sol"],
        }

        for market in markets:
            question = getattr(market, "question", "").lower()
            yes_price = getattr(market, "yes_price", 0.5)

            asset = None
            for key, keywords in asset_map.items():
                if any(kw in question for kw in keywords):
                    asset = key
                    break

            if not asset:
                continue

            synth_prob = self._get_synth_probability(asset, "up")
            if synth_prob is None:
                continue

            divergence = abs(synth_prob - yes_price)

            if divergence < divergence_threshold:
                continue

            if synth_prob > yes_price:
                action = "buy_yes"
                win_prob = synth_prob
            else:
                action = "buy_no"
                win_prob = 1 - synth_prob

            edge = divergence

            opp = Opportunity(
                strategy=self.name,
                market_id=market.market_id,
                market_question=getattr(market, "question", ""),
                action=action,
                edge=edge,
                win_probability=win_prob,
                max_payout=1.0 / yes_price if action == "buy_yes" else 1.0 / (1 - yes_price),
                time_to_resolution_sec=getattr(market, "seconds_to_resolution", 86400),
            )
            opportunities.append(opp)
            log.info("s6.opportunity_found",
                     market_id=market.market_id,
                     asset=asset,
                     polymarket_price=yes_price,
                     synth_prob=synth_prob,
                     divergence=divergence)

        return opportunities

    def score(self, opp: Opportunity, config: dict) -> float:
        """Score based on divergence magnitude."""
        if opp.edge < 0.10:
            return 0.0

        time_factor = 1.0
        if opp.time_to_resolution_sec < 3600:
            time_factor = 0.8
        elif opp.time_to_resolution_sec > 86400:
            time_factor = 1.2

        return min(opp.edge * time_factor, 1.0)

    def size(self, opp: Opportunity, bankroll: float, config: dict) -> float:
        """Size position using Kelly, capped at max_position_pct."""
        strategy_config = config.get("s6_synth_ai", {})
        max_pct = strategy_config.get("max_position_pct", 0.10)
        kelly = strategy_config.get("kelly_fraction", 0.25)

        max_size = bankroll * max_pct

        b = opp.edge
        p = opp.win_probability
        q = 1 - p

        if b * p - q <= 0:
            return 1.0

        kelly_size = bankroll * kelly * (b * p - q) / b

        return min(kelly_size, max_size)

    def on_resolve(self, resolution: dict) -> dict:
        """Record resolution outcome."""
        won = resolution.get("won", False)
        roi = resolution.get("roi", 0.0)

        return {
            "won": won,
            "roi": roi,
            "notes": f"S6 Synth AI: {'WIN' if won else 'LOSS'} - {roi:.2%} ROI",
            "lessons": [
                "S6: Only use MAKER orders (GTD) for zero fees",
                "S6: Divergence > 10% required for entry",
                "S6: Monitor Synth API costs vs returns",
            ] if not won else [],
        }