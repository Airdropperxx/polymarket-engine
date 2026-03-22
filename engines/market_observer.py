"""
engines/market_observer.py — Market price polling and pattern detection engine.

What this does:
  Polls selected markets repeatedly (every scan cycle) and records how their
  prices MOVE over time. This is different from data_engine which just fetches
  the current snapshot.

  The core insight: Polymarket prices are driven by:
    1. New information (news, results, on-chain data)
    2. Whales moving markets (large orders pushing price)
    3. Liquidity seeking (market makers adjusting spreads)
    4. Resolution approach (prices converge to 0/1 as end date nears)

What patterns we can detect:
  - Price momentum: markets moving consistently in one direction
    -> If YES moves from 0.70 to 0.80 to 0.87 over 3 scans, something is happening
    -> Momentum trades: buy in the direction of movement before full resolution
  
  - Mean reversion: markets that overshoot and snap back
    -> A sudden jump from 0.60 to 0.90 with no news often reverses
    -> Fade the move: bet against the jump
  
  - Liquidity drain: volume dropping near resolution
    -> Low volume + high price = thin book = dangerous to buy
    -> High volume + high price = confident market = safer
  
  - Spread widening: bid-ask spread increases near resolution
    -> Wider spread = market makers uncertain = higher risk
  
  - NegRisk imbalance: sum of YES prices in a group drifting away from 1.0
    -> Group sum > 1.05 = arb opportunity building
    -> Alerts S1 strategy before full scan detects it

Storage: data/price_history.json — last 100 data points per tracked market
Committed to git after every scan cycle so history persists across GHA runs.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(component="market_observer")

PRICE_HISTORY = Path("data/price_history.json")
MAX_HISTORY_PER_MARKET = 100   # data points per market
MAX_MARKETS_TO_TRACK   = 200   # top N by volume


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MarketObserver:
    """
    Tracks price history and detects patterns in how markets move.
    
    Call observe(markets) every scan cycle.
    Call detect_signals(markets) to get actionable signals.
    """

    def __init__(self, config: dict):
        self.config  = config
        self._history: dict = self._load()

    # ------------------------------------------------------------------ #
    #  Public interface                                                    #
    # ------------------------------------------------------------------ #

    def observe(self, markets: list) -> None:
        """
        Record current prices for top markets by volume.
        Called every scan cycle — builds the time series.
        """
        # Track top markets by volume
        sorted_markets = sorted(markets, key=lambda m: m.volume_24h, reverse=True)
        to_track = sorted_markets[:MAX_MARKETS_TO_TRACK]

        now_ts  = int(time.time())
        now_iso = _now_iso()
        new_points = 0

        for market in to_track:
            mid = market.market_id
            if mid not in self._history:
                self._history[mid] = {
                    "question": market.question,
                    "category": market.category,
                    "points":   [],
                }

            point = {
                "ts":       now_ts,
                "iso":      now_iso,
                "yes":      round(market.yes_price, 4),
                "no":       round(market.no_price, 4),
                "vol":      round(market.volume_24h, 2),
                "ttl_sec":  market.seconds_to_resolution,
                "spread":   round(market.yes_ask - market.yes_bid, 4),
            }
            self._history[mid]["points"].append(point)

            # Keep last N points
            if len(self._history[mid]["points"]) > MAX_HISTORY_PER_MARKET:
                self._history[mid]["points"] = (
                    self._history[mid]["points"][-MAX_HISTORY_PER_MARKET:]
                )
            new_points += 1

        # Prune markets with no recent data (>7 days old)
        cutoff = now_ts - (7 * 86400)
        to_delete = [
            mid for mid, data in self._history.items()
            if data["points"] and data["points"][-1]["ts"] < cutoff
        ]
        for mid in to_delete:
            del self._history[mid]

        self._save()
        log.info("observer_recorded",
                 markets_tracked=len(self._history),
                 new_points=new_points)

    def detect_signals(self, markets: list) -> list[dict]:
        """
        Detect actionable signals from price history.
        Returns list of signal dicts, sorted by strength.

        Signal types:
          momentum_up    — consistent YES price increase over last 3+ points
          momentum_down  — consistent NO price increase
          reversion_risk — sharp recent jump, likely to reverse
          resolution_drift — price approaching 1.0, still tradeable
          negrisk_imbalance — group sum diverging from 1.0
          volume_spike   — unusual volume increase (smart money moving in)
        """
        signals = []
        market_by_id = {m.market_id: m for m in markets}

        for mid, data in self._history.items():
            pts = data["points"]
            if len(pts) < 3:
                continue

            market = market_by_id.get(mid)
            if not market:
                continue

            question = data.get("question", market.question)
            category = data.get("category", market.category)

            # Extract recent YES prices
            recent = pts[-6:]   # last 6 observations
            yes_prices = [p["yes"] for p in recent]
            volumes    = [p["vol"] for p in recent]

            # ── Signal 1: Momentum ────────────────────────────────────────
            if len(yes_prices) >= 3:
                deltas = [yes_prices[i] - yes_prices[i-1]
                          for i in range(1, len(yes_prices))]
                pos_moves = sum(1 for d in deltas if d > 0.005)
                neg_moves = sum(1 for d in deltas if d < -0.005)
                total_move = yes_prices[-1] - yes_prices[0]

                if pos_moves >= len(deltas) - 1 and total_move > 0.03:
                    signals.append({
                        "type":     "momentum_up",
                        "market_id": mid,
                        "question": question,
                        "category": category,
                        "strength": min(1.0, abs(total_move) * 10),
                        "yes_price": market.yes_price,
                        "total_move": round(total_move, 4),
                        "n_points":  len(yes_prices),
                        "note": (f"YES price moved +{total_move:.3f} "
                                 f"over {len(yes_prices)} observations. "
                                 "Consistent upward trend — consider BUY YES."),
                    })

                elif neg_moves >= len(deltas) - 1 and total_move < -0.03:
                    signals.append({
                        "type":     "momentum_down",
                        "market_id": mid,
                        "question": question,
                        "category": category,
                        "strength": min(1.0, abs(total_move) * 10),
                        "yes_price": market.yes_price,
                        "total_move": round(total_move, 4),
                        "n_points":  len(yes_prices),
                        "note": (f"YES price fell {total_move:.3f} "
                                 f"over {len(yes_prices)} observations. "
                                 "Consider BUY NO."),
                    })

            # ── Signal 2: Sharp reversion risk ───────────────────────────
            if len(yes_prices) >= 2:
                last_jump = yes_prices[-1] - yes_prices[-2]
                if abs(last_jump) > 0.08:
                    signals.append({
                        "type":     "sharp_move",
                        "market_id": mid,
                        "question": question,
                        "category": category,
                        "strength": min(1.0, abs(last_jump) * 5),
                        "yes_price": market.yes_price,
                        "jump":     round(last_jump, 4),
                        "note": (f"Sharp move of {last_jump:+.3f} in last scan. "
                                 "May revert — watch next cycle."),
                    })

            # ── Signal 3: Resolution drift ────────────────────────────────
            if (0.88 <= market.yes_price <= 0.97
                    and market.seconds_to_resolution < 7 * 86400
                    and market.volume_24h > 200):
                ttl_days = market.seconds_to_resolution / 86400
                signals.append({
                    "type":     "resolution_drift",
                    "market_id": mid,
                    "question": question,
                    "category": category,
                    "strength": (market.yes_price - 0.88) / 0.09,
                    "yes_price": market.yes_price,
                    "days_left": round(ttl_days, 2),
                    "volume":   market.volume_24h,
                    "note": (f"High-prob ({market.yes_price:.2%}) market resolving "
                             f"in {ttl_days:.1f}d with ${market.volume_24h:,.0f} volume. "
                             "Classic S10 target."),
                })

            # ── Signal 4: Volume spike ─────────────────────────────────────
            if len(volumes) >= 4:
                avg_prev = sum(volumes[:-2]) / len(volumes[:-2])
                last_vol = volumes[-1]
                if avg_prev > 0 and last_vol > avg_prev * 3:
                    signals.append({
                        "type":     "volume_spike",
                        "market_id": mid,
                        "question": question,
                        "category": category,
                        "strength": min(1.0, last_vol / avg_prev / 10),
                        "yes_price": market.yes_price,
                        "spike_ratio": round(last_vol / avg_prev, 1),
                        "note": (f"Volume spiked {last_vol/avg_prev:.1f}x vs average. "
                                 "Smart money entering — follow direction."),
                    })

        # Sort by strength
        signals.sort(key=lambda s: s["strength"], reverse=True)
        log.info("signals_detected",
                 total=len(signals),
                 by_type={t: sum(1 for s in signals if s["type"] == t)
                          for t in set(s["type"] for s in signals)})
        return signals

    def get_stats(self) -> dict:
        """Return summary statistics about collected history."""
        total_points = sum(len(d["points"]) for d in self._history.values())
        by_cat: dict = defaultdict(int)
        for data in self._history.values():
            by_cat[data.get("category", "other")] += len(data["points"])

        oldest_ts = None
        newest_ts = None
        for data in self._history.values():
            for pt in data["points"]:
                ts = pt["ts"]
                if oldest_ts is None or ts < oldest_ts:
                    oldest_ts = ts
                if newest_ts is None or ts > newest_ts:
                    newest_ts = ts

        return {
            "markets_tracked": len(self._history),
            "total_data_points": total_points,
            "by_category": dict(by_cat),
            "oldest_observation": (
                datetime.fromtimestamp(oldest_ts, tz=timezone.utc).isoformat()
                if oldest_ts else None
            ),
            "newest_observation": (
                datetime.fromtimestamp(newest_ts, tz=timezone.utc).isoformat()
                if newest_ts else None
            ),
            "avg_points_per_market": (
                round(total_points / len(self._history), 1)
                if self._history else 0
            ),
        }

    def get_price_history(self, market_id: str) -> Optional[dict]:
        """Return full price history for a specific market."""
        return self._history.get(market_id)

    # ------------------------------------------------------------------ #
    #  Persistence                                                         #
    # ------------------------------------------------------------------ #

    def _load(self) -> dict:
        if PRICE_HISTORY.exists():
            try:
                return json.loads(PRICE_HISTORY.read_text())
            except Exception as e:
                log.warning("price_history_load_failed", error=str(e))
        return {}

    def _save(self) -> None:
        PRICE_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        try:
            PRICE_HISTORY.write_text(json.dumps(self._history, separators=(",", ":")))
        except Exception as e:
            log.warning("price_history_save_failed", error=str(e))
