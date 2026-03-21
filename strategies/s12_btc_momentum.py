"""
strategies/s12_btc_momentum.py
==============================
BTC/USD momentum trading strategy.

Monitors BTC price movements and trades Polymarket BTC prediction markets.
- Uses Binance API for real-time BTC prices (no latency infrastructure needed)
- Trades "Will BTC be above X at time Y?" type markets
- Momentum-based entry: buy YES when BTC trending up, buy NO when trending down

No latency infrastructure required - markets resolve in hours/days,
plenty of time for normal API execution.
"""

from __future__ import annotations

import time
import structlog
import requests

from strategies.base import BaseStrategy, Opportunity, Resolution
from engines.data_engine import MarketState

log = structlog.get_logger()

_BINANCE_API = "https://api.binance.com/api/v3"


class BTCMomentumStrategy(BaseStrategy):
    """
    S12: BTC/USD momentum trading on Polymarket.
    
    Fetches real-time BTC price from Binance.
    Finds Polymarket BTC prediction markets and trades based on momentum.
    
    Example: If BTC at $95K and rising, buy YES on "BTC above $100K by Friday"
    """

    name = "s12_btc_momentum"

    def __init__(self) -> None:
        self._btc_cache = {
            "price": None,
            "price_1h_ago": None,
            "timestamp": 0,
        }

    def scan(
        self,
        markets: list,
        negrisk_groups: dict,
        config: dict,
    ) -> list[Opportunity]:
        # Fetch BTC price
        btc_data = self._fetch_btc_price()
        if not btc_data:
            log.warning("s12.btc_api_unavailable")
            return []

        current_price = btc_data["price"]
        change_1h = btc_data["change_1h"]
        change_24h = btc_data["change_24h"]

        # Look for BTC-related markets
        btc_markets = []
        for market in markets:
            q = market.question.lower()
            if "btc" in q or "bitcoin" in q or "bitcoin" in q:
                btc_markets.append(market)

        if not btc_markets:
            log.info("s12.no_btc_markets", current_price=current_price)
            return []

        opps: list[Opportunity] = []

        for market in btc_markets:
            opp = self._evaluate_market(market, current_price, change_1h, change_24h)
            if opp:
                opps.append(opp)

        log.info("s12.scan_done",
                 markets_checked=len(btc_markets),
                 opportunities=len(opps),
                 btc_price=current_price,
                 change_1h=round(change_1h, 2),
                 change_24h=round(change_24h, 2))
        return opps

    def _fetch_btc_price(self) -> dict | None:
        """Fetch BTC price from Binance API."""
        now = time.time()
        
        # Cache for 5 minutes
        if self._btc_cache["price"] and (now - self._btc_cache["timestamp"]) < 300:
            return {
                "price": self._btc_cache["price"],
                "price_1h_ago": self._btc_cache["price_1h_ago"],
                "change_1h": self._btc_cache.get("change_1h", 0),
                "change_24h": self._btc_cache.get("change_24h", 0),
            }

        try:
            # Current price
            resp = requests.get(
                f"{_BINANCE_API}/ticker/price",
                params={"symbol": "BTCUSDT"},
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            
            current_price = float(resp.json()["price"])
            
            # 24h ticker for change
            resp24 = requests.get(
                f"{_BINANCE_API}/ticker/24hr",
                params={"symbol": "BTCUSDT"},
                timeout=10,
            )
            change_24h = 0
            if resp24.status_code == 200:
                change_24h = float(resp24.json()["priceChangePercent"])
            
            # Estimate 1h change (simplified - would need historical data for accuracy)
            change_1h = change_24h / 24  # Rough estimate

            self._btc_cache = {
                "price": current_price,
                "price_1h_ago": current_price * (1 - change_1h / 100),
                "change_1h": change_1h,
                "change_24h": change_24h,
                "timestamp": now,
            }

            return self._btc_cache
        except Exception as exc:
            log.warning("s12.btc_fetch_error", error=str(exc))
            return None

    def _evaluate_market(
        self,
        market: MarketState,
        current_price: float,
        change_1h: float,
        change_24h: float,
    ) -> Opportunity | None:
        """Evaluate a BTC market for momentum-based trading."""
        q = market.question.lower()
        
        # Parse target price from question
        # Example: "Will BTC be above $100,000 by Dec 31?"
        target_price = self._extract_target_price(q)
        if not target_price:
            return None
        
        # Calculate probability implied by market
        yes_price = market.yes_price
        no_price = market.no_price
        
        # Determine action based on momentum
        action = None
        edge = 0
        
        if "above" in q or "higher" in q:
            if change_24h > 2:  # BTC up >2% in 24h
                # Momentum suggests going higher
                implied_prob = yes_price
                expected_prob = min(0.95, 0.50 + change_24h / 100)
                if expected_prob > implied_prob + 0.05:  # 5% edge
                    action = "buy_yes"
                    edge = expected_prob - implied_prob - self.calc_fee(yes_price)
        
        if "below" in q or "lower" in q:
            if change_24h < -2:  # BTC down >2% in 24h
                implied_prob = yes_price  # YES = below target
                expected_prob = min(0.95, 0.50 + abs(change_24h) / 100)
                if expected_prob > implied_prob + 0.05:
                    action = "buy_yes"
                    edge = expected_prob - implied_prob - self.calc_fee(yes_price)
        
        if not action or edge < 0.02:
            return None
        
        return Opportunity(
            strategy=self.name,
            market_id=market.market_id,
            market_question=market.question,
            action=action,
            edge=edge,
            win_probability=yes_price,
            max_payout=1.0 / yes_price if yes_price > 0 else 1.0,
            time_to_resolution_sec=market.seconds_to_resolution,
            metadata={
                "btc_price": current_price,
                "target_price": target_price,
                "change_24h": change_24h,
                "category": "crypto",
            },
        )

    def _extract_target_price(self, question: str) -> float | None:
        """Extract target price from question text."""
        import re
        # Look for dollar amounts
        matches = re.findall(r'\$?([0-9,]+(?:\.[0-9]+)?)', question)
        for match in matches:
            price_str = match.replace(",", "")
            try:
                price = float(price_str)
                if 1000 < price < 1000000:  # Reasonable BTC range
                    return price
            except ValueError:
                continue
        return None

    def score(self, opp: Opportunity, config: dict) -> float:
        """Score based on edge and momentum strength."""
        btc_change = abs(opp.metadata.get("change_24h", 0))
        momentum_score = min(btc_change / 5.0, 1.0)  # 5% change = max score
        return opp.edge * 0.6 + momentum_score * 0.4

    def size(self, opp: Opportunity, bankroll: float, config: dict) -> float:
        return self.calc_kelly_size(
            win_probability=opp.win_probability,
            payout_ratio=opp.max_payout,
            bankroll=bankroll,
            kelly_fraction=config.get("kelly_fraction", 0.25),
            max_position_pct=config.get("max_position_pct", 0.10),
        )

    def on_resolve(self, resolution: Resolution) -> dict:
        lessons: list[str] = []
        if not resolution.won:
            lessons.append(
                f"S12 BTC momentum loss on {resolution.market_id[:40]}. "
                f"ROI={resolution.roi:.2%}. "
                "Check if momentum thesis was correct."
            )
        return {
            "won": resolution.won,
            "roi": resolution.roi,
            "notes": resolution.notes,
            "lessons": lessons,
        }
