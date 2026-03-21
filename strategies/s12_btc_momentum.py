"""
strategies/s12_btc_momentum.py
==============================
BTC/USD momentum trading strategy.

Monitors BTC price movements and trades Polymarket BTC prediction markets.
- Uses Binance API for real-time BTC prices
- Trades "Will BTC be above X?" type markets
- Momentum-based: buy YES when BTC trending up, buy NO when trending down

KEY INSIGHT: BTC moves in predictable trends. Even 0.5-1% movements
can create profitable opportunities if the market hasn't priced in the move.
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
    
    The edge comes from the difference between:
    - Market's implied probability (from YES price)
    - Our estimate based on current momentum and distance to target
    """

    name = "s12_btc_momentum"

    def __init__(self) -> None:
        self._btc_cache = {
            "price": None,
            "price_1h_ago": None,
            "price_24h_ago": None,
            "timestamp": 0,
        }
        self._min_momentum_pct = 0.3  # Trade on 0.3%+ movements

    def scan(
        self,
        markets: list,
        negrisk_groups: dict,
        config: dict,
    ) -> list[Opportunity]:
        btc_data = self._fetch_btc_price()
        if not btc_data:
            log.warning("s12.btc_api_unavailable")
            return []

        current_price = btc_data["price"]
        change_1h = btc_data.get("change_1h", 0)
        change_24h = btc_data.get("change_24h", 0)
        change_1h_pct = btc_data.get("change_1h_pct", 0)
        change_24h_pct = btc_data.get("change_24h_pct", 0)

        # Look for BTC-related markets
        btc_markets = []
        for market in markets:
            q = market.question.lower()
            if "btc" in q or "bitcoin" in q:
                btc_markets.append(market)

        if not btc_markets:
            log.info("s12.no_btc_markets", current_price=current_price)
            return []

        opps: list[Opportunity] = []

        for market in btc_markets:
            opp = self._evaluate_market(
                market, 
                current_price, 
                change_1h_pct, 
                change_24h_pct,
                btc_data
            )
            if opp:
                opps.append(opp)

        log.info("s12.scan_done",
                 markets_checked=len(btc_markets),
                 opportunities=len(opps),
                 btc_price=current_price,
                 change_1h=round(change_1h_pct, 3),
                 change_24h=round(change_24h_pct, 3))
        return opps

    def _fetch_btc_price(self) -> dict | None:
        """Fetch BTC price from Binance API with hourly and 24h history."""
        now = time.time()
        
        # Cache for 1 minute
        if self._btc_cache["price"] and (now - self._btc_cache["timestamp"]) < 60:
            return self._btc_cache

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
            change_24h_pct = 0
            open_price = current_price
            if resp24.status_code == 200:
                data24 = resp24.json()
                open_price = float(data24.get("openPrice", current_price))
                change_24h_pct = float(data24.get("priceChangePercent", 0))
            
            # Get 1h candles for hourly change
            change_1h_pct = 0
            try:
                resp_klines = requests.get(
                    f"{_BINANCE_API}/klines",
                    params={"symbol": "BTCUSDT", "interval": "1h", "limit": 2},
                    timeout=10,
                )
                if resp_klines.status_code == 200:
                    klines = resp_klines.json()
                    if len(klines) >= 2:
                        open_1h = float(klines[-2][1])
                        close_1h = float(klines[-1][4])
                        change_1h_pct = ((close_1h - open_1h) / open_1h) * 100
            except:
                pass

            self._btc_cache = {
                "price": current_price,
                "open_price": open_price,
                "change_1h_pct": change_1h_pct,
                "change_24h_pct": change_24h_pct,
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
        btc_data: dict,
    ) -> Opportunity | None:
        """Evaluate a BTC market for momentum-based trading."""
        q = market.question.lower()
        
        # Parse target price from question
        target_price = self._extract_target_price(q)
        if not target_price:
            return None
        
        yes_price = market.yes_price
        
        # Determine action based on momentum
        action = None
        edge = 0
        
        # Calculate our momentum-based probability estimate
        # Use both 1h and 24h momentum
        momentum_score = change_1h * 0.7 + change_24h * 0.3
        
        # Skip if no meaningful momentum
        if abs(momentum_score) < self._min_momentum_pct:
            return None
        
        # Calculate expected probability based on momentum and distance to target
        price_ratio = current_price / target_price
        
        if "above" in q or "higher" in q or "exceed" in q:
            # YES = BTC will be above target
            # If BTC trending up and below target, YES is more likely
            if momentum_score > 0 and current_price < target_price:
                # Calculate expected probability
                # If 10% away from target and 1% momentum, might take 10h to reach
                hours_to_target = 10 if price_ratio > 0.9 else (1 - price_ratio) * 100
                
                # Simple model: momentum increases probability
                implied_prob = yes_price
                expected_prob = min(0.95, 0.50 + momentum_score * 2)
                
                # Edge if expected > implied
                if expected_prob > implied_prob + 0.02:
                    action = "buy_yes"
                    edge = expected_prob - implied_prob - self.calc_fee(yes_price)
        
        if "below" in q or "lower" in q or "under" in q:
            # YES = BTC will be below target (for some markets)
            # If BTC trending down and above target, below is more likely
            if momentum_score < 0 and current_price > target_price:
                implied_prob = yes_price
                expected_prob = min(0.95, 0.50 + abs(momentum_score) * 2)
                
                if expected_prob > implied_prob + 0.02:
                    action = "buy_yes"
                    edge = expected_prob - implied_prob - self.calc_fee(yes_price)
        
        if not action or edge < 0.01:
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
                "change_1h": round(change_1h, 3),
                "change_24h": round(change_24h, 3),
                "momentum_score": round(momentum_score, 3),
                "category": "crypto",
            },
        )

    def _extract_target_price(self, question: str) -> float | None:
        """Extract target price from question text."""
        import re
        matches = re.findall(r'\$?([0-9,]+(?:\.[0-9]+)?)', question)
        for match in matches:
            price_str = match.replace(",", "")
            try:
                price = float(price_str)
                # BTC range check
                if 10000 < price < 500000:
                    return price
            except ValueError:
                continue
        return None

    def score(self, opp: Opportunity, config: dict) -> float:
        """Score based on edge and momentum strength."""
        momentum = abs(opp.metadata.get("momentum_score", 0))
        momentum_score = min(momentum / 2.0, 1.0)
        return opp.edge * 0.5 + momentum_score * 0.5

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
                f"S12 BTC loss ROI={resolution.roi:.2%}. "
                "Check if momentum thesis was correct."
            )
        return {
            "won": resolution.won,
            "roi": resolution.roi,
            "notes": resolution.notes,
            "lessons": lessons,
        }
