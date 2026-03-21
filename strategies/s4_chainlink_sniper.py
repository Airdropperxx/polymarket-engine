"""
strategies/s4_chainlink_sniper.py
==================================
Chainlink oracle front-running strategy.

Monitors Chainlink BTC/USD price feed (same oracle Polymarket uses for hourly BTC markets).
When price crosses hourly strike with < 2 min remaining → enter position.

PREREQUISITE: Capital must be > $1,000. Do not build before Checkpoint 2.

NOTE: Poll interval of 30 min is acceptable for hourly markets.
"""

import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from strategies.base import BaseStrategy, Opportunity

log = structlog.get_logger()

CHAINLINK_ABI = """
{
  "inputs": [{"name": "roundId","type":"uint80"}],
  "name": "getRoundData",
  "outputs": [
    {"name":"roundId","type":"uint80"},
    {"name":"answer","type":"int256"},
    {"name":"startedAt","type":"uint256"},
    {"name":"updatedAt","type":"uint256"},
    {"name":"answeredInRound","type":"uint80"}
  ],
  "stateMutability": "view",
  "type": "function"
}
"""

CHAINLINK_BTC_FEED = "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c"


class ChainlinkSniperStrategy(BaseStrategy):
    """
    Chainlink oracle front-running for hourly BTC markets.
    
    Monitors Chainlink BTC/USD price. When the price crosses an hourly strike
    (e.g., $90,000, $90,500, $91,000) with < 2 minutes to the hourly close,
    enter a position expecting the price to hold or continue in that direction.
    """

    name = "s4_chainlink_sniper"

    def __init__(self):
        self._rpc_url = os.environ.get("POLYGON_RPC_URL")
        self._last_fetch = 0
        self._cached_price = None
        self._strike_interval = 500

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def _fetch_chainlink_price(self) -> Optional[float]:
        """Fetch current BTC/USD price from Chainlink oracle via Alchemy RPC."""
        if not self._rpc_url:
            log.warning("s4.no_rpc_url")
            return None

        try:
            resp = requests.post(
                self._rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "method": "eth_call",
                    "params": [
                        {
                            "to": CHAINLINK_BTC_FEED,
                            "data": "0xfeaf968c",  # latestRoundData()
                        },
                        "latest",
                    ],
                    "id": 1,
                },
                timeout=10,
            )

            if not resp.ok:
                log.warning("s4.rpc_error", status=resp.status_code)
                return None

            data = resp.json()
            result = data.get("result", "")

            if len(result) >= 66:
                answer = int(result[2:66], 16)
                btc_price = answer / 1e8
                log.info("s4.price_fetched", price=btc_price)
                return btc_price

        except Exception as e:
            log.error("s4.fetch_error", error=str(e))

        return None

    def get_current_price(self) -> Optional[float]:
        """Get current BTC price with caching (5 min cache)."""
        now = time.time()
        if self._cached_price and (now - self._last_fetch) < 300:
            return self._cached_price

        price = self._fetch_chainlink_price()
        if price:
            self._cached_price = price
            self._last_fetch = now

        return price

    def _get_minutes_to_hour(self) -> float:
        """Get minutes until next hourly mark."""
        now = datetime.now(timezone.utc)
        minutes = now.minute
        seconds = now.second
        return (60 - minutes) - (seconds / 60)

    def _get_strike_level(self, price: float) -> int:
        """Get the current strike level (rounded to nearest interval)."""
        return int(price / self._strike_interval) * self._strike_interval

    def scan(self, markets: list, negrisk_groups: dict, config: dict) -> list[Opportunity]:
        """
        Scan for hourly BTC markets that are about to resolve.
        
        Entry condition:
        - Market resolves within 2 minutes (within hourly close)
        - Chainlink price is near a strike level
        - Current price vs strike suggests direction
        """
        opportunities = []
        min_minutes = config.get("s4_chainlink_sniper", {}).get("max_minutes_remaining", 2)

        minutes_to_hour = self._get_minutes_to_hour()

        if minutes_to_hour > min_minutes:
            return opportunities

        btc_price = self.get_current_price()
        if not btc_price:
            return opportunities

        strike_level = self._get_strike_level(btc_price)
        price_to_strike = abs(btc_price - strike_level)

        if price_to_strike < (self._strike_interval * 0.1):
            for market in markets:
                question = market.question.lower() if hasattr(market, "question") else ""

                if "btc" not in question or "hour" not in question:
                    continue

                if hasattr(market, "seconds_to_resolution"):
                    if market.seconds_to_resolution > 120:
                        continue

                yes_price = getattr(market, "yes_price", 0.5)

                if btc_price >= strike_level:
                    action = "buy_yes"
                    edge = 0.05
                else:
                    action = "buy_no"
                    edge = 0.05

                opp = Opportunity(
                    strategy=self.name,
                    market_id=market.market_id,
                    market_question=getattr(market, "question", ""),
                    action=action,
                    edge=edge,
                    win_probability=yes_price if action == "buy_yes" else (1 - yes_price),
                    max_payout=1.0 / yes_price if action == "buy_yes" else 1.0 / (1 - yes_price),
                    time_to_resolution_sec=getattr(market, "seconds_to_resolution", 0),
                )
                opportunities.append(opp)
                log.info("s4.opportunity_found",
                         market_id=market.market_id,
                         btc_price=btc_price,
                         strike=strike_level,
                         minutes_left=minutes_to_hour)

        return opportunities

    def score(self, opp: Opportunity, config: dict) -> float:
        """Score based on time to resolution and edge."""
        if opp.time_to_resolution_sec > 120:
            return 0.0

        if opp.edge < 0.03:
            return 0.0

        base_score = min(opp.edge * 10, 1.0)

        time_bonus = 1.0 - (opp.time_to_resolution_sec / 120)

        return base_score * 0.7 + time_bonus * 0.3

    def size(self, opp: Opportunity, bankroll: float, config: dict) -> float:
        """Size position using Kelly fraction, capped at max_position_pct."""
        strategy_config = config.get("s4_chainlink_sniper", {})
        max_pct = strategy_config.get("max_position_pct", 0.05)
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
        """Record resolution outcome for learning."""
        won = resolution.get("won", False)
        roi = resolution.get("roi", 0.0)

        return {
            "won": won,
            "roi": roi,
            "notes": f"S4 Chainlink Sniper: {'WIN' if won else 'LOSS'} - {roi:.2%} ROI",
            "lessons": [
                "S4: Only enter when < 2 min to hourly close",
                "S4: Strike proximity matters - enter when price within 10% of strike",
            ] if not won else [],
        }