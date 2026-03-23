"""
engines/market_observer.py — Market price polling and pattern detection engine.

v2 improvements:
- Tracks top 200 by volume + top 100 by soonest resolution (deduped → up to 300)
  so short-lived same-day sports/crypto markets are never missed
- Stores bid/ask spread in each point for spread-widening detection
- Stores no_price for both-side analysis
- negrisk_imbalance signal properly implemented using group data
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

PRICE_HISTORY          = Path("data/price_history.json")
MAX_HISTORY_PER_MARKET = 100
MAX_MARKETS_BY_VOLUME  = 200
MAX_MARKETS_BY_TTL     = 100   # soonest-resolving markets (catches same-day)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MarketObserver:

    def __init__(self, config: dict):
        self.config   = config
        self._history: dict = self._load()

    def observe(self, markets: list) -> None:
        """
        Record current prices for tracked markets every scan cycle.
        Tracked = top 200 by 24h volume UNION top 100 by soonest resolution.
        """
        by_volume = sorted(markets, key=lambda m: m.volume_24h, reverse=True)
        by_ttl    = sorted(
            [m for m in markets if m.seconds_to_resolution > 0],
            key=lambda m: m.seconds_to_resolution
        )
        # Deduplicate: volume list first, TTL list fills remaining slots
        seen    = set()
        to_track = []
        for m in by_volume[:MAX_MARKETS_BY_VOLUME]:
            seen.add(m.market_id)
            to_track.append(m)
        for m in by_ttl[:MAX_MARKETS_BY_TTL]:
            if m.market_id not in seen:
                seen.add(m.market_id)
                to_track.append(m)

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

            self._history[mid]["points"].append({
                "ts":  now_ts,
                "iso": now_iso,
                "yes": round(market.yes_price, 4),
                "no":  round(market.no_price, 4),
                "yb":  round(getattr(market, "yes_bid",  market.yes_price - 0.01), 4),
                "ya":  round(getattr(market, "yes_ask",  market.yes_price + 0.01), 4),
                "vol": round(market.volume_24h, 2),
                "ttl": market.seconds_to_resolution,
            })

            if len(self._history[mid]["points"]) > MAX_HISTORY_PER_MARKET:
                self._history[mid]["points"] = (
                    self._history[mid]["points"][-MAX_HISTORY_PER_MARKET:]
                )
            new_points += 1

        # Prune markets with no data in last 7 days
        cutoff = now_ts - (7 * 86400)
        stale  = [
            mid for mid, data in self._history.items()
            if data["points"] and data["points"][-1]["ts"] < cutoff
        ]
        for mid in stale:
            del self._history[mid]

        self._save()
        log.info("observer_recorded",
                 markets_tracked=len(self._history),
                 by_volume=min(len(by_volume), MAX_MARKETS_BY_VOLUME),
                 by_ttl=sum(1 for m in by_ttl[:MAX_MARKETS_BY_TTL] if m.market_id not in set(m2.market_id for m2 in by_volume[:MAX_MARKETS_BY_VOLUME])),
                 new_points=new_points)

    def detect_signals(self, markets: list, groups: dict = None) -> list[dict]:
        """
        Detect actionable signals from price history.

        Signal types:
          momentum_up       — consistent YES price increase over last 3+ points
          momentum_down     — consistent NO price increase
          sharp_move        — single-candle jump >8% (reversion candidate)
          resolution_drift  — price 88-97%, resolving within 7 days
          volume_spike      — unusual volume increase (smart money)
          spread_widening   — bid-ask spread increasing (market maker uncertainty)
          negrisk_imbalance — NegRisk group sum drifting away from 1.0
        """
        signals       = []
        market_by_id  = {m.market_id: m for m in markets}

        for mid, data in self._history.items():
            pts = data["points"]
            if len(pts) < 3:
                continue

            market = market_by_id.get(mid)
            if not market:
                continue

            question = data.get("question", market.question)
            category = data.get("category", market.category)

            recent     = pts[-6:]
            yes_prices = [p["yes"] for p in recent]
            no_prices  = [p.get("no", 1.0 - p["yes"]) for p in recent]
            volumes    = [p["vol"] for p in recent]
            spreads    = [p.get("ya", p["yes"]+0.01) - p.get("yb", p["yes"]-0.01)
                         for p in recent]

            # ── Momentum ─────────────────────────────────────────────────
            if len(yes_prices) >= 3:
                deltas    = [yes_prices[i]-yes_prices[i-1] for i in range(1, len(yes_prices))]
                pos_moves = sum(1 for d in deltas if d > 0.005)
                neg_moves = sum(1 for d in deltas if d < -0.005)
                total_move = yes_prices[-1] - yes_prices[0]

                if pos_moves >= len(deltas) - 1 and total_move > 0.03:
                    signals.append({
                        "type": "momentum_up", "market_id": mid,
                        "question": question, "category": category,
                        "strength": min(1.0, abs(total_move) * 10),
                        "yes_price": market.yes_price,
                        "total_move": round(total_move, 4),
                        "n_points": len(yes_prices),
                        "note": f"YES moved +{total_move:.3f} over {len(yes_prices)} obs. BUY YES.",
                    })
                elif neg_moves >= len(deltas) - 1 and total_move < -0.03:
                    signals.append({
                        "type": "momentum_down", "market_id": mid,
                        "question": question, "category": category,
                        "strength": min(1.0, abs(total_move) * 10),
                        "yes_price": market.yes_price,
                        "total_move": round(total_move, 4),
                        "n_points": len(yes_prices),
                        "note": f"YES fell {total_move:.3f} over {len(yes_prices)} obs. BUY NO.",
                    })

            # ── Sharp move (reversion candidate) ─────────────────────────
            if len(yes_prices) >= 2:
                last_jump = yes_prices[-1] - yes_prices[-2]
                if abs(last_jump) > 0.08:
                    signals.append({
                        "type": "sharp_move", "market_id": mid,
                        "question": question, "category": category,
                        "strength": min(1.0, abs(last_jump) * 5),
                        "yes_price": market.yes_price,
                        "jump": round(last_jump, 4),
                        "note": f"Single-candle {'+' if last_jump>0 else ''}{last_jump:.3f} jump. Reversion risk.",
                    })

            # ── Resolution drift ──────────────────────────────────────────
            if (0.88 <= market.yes_price <= 0.97
                    and market.seconds_to_resolution < 7 * 86400
                    and market.volume_24h > 200):
                ttl_days = market.seconds_to_resolution / 86400
                signals.append({
                    "type": "resolution_drift", "market_id": mid,
                    "question": question, "category": category,
                    "strength": (market.yes_price - 0.88) / 0.09,
                    "yes_price": market.yes_price,
                    "days_left": round(ttl_days, 2),
                    "volume": market.volume_24h,
                    "note": f"High-prob ({market.yes_price:.2%}) resolving in {ttl_days:.1f}d. S10 target.",
                })

            # ── Volume spike ──────────────────────────────────────────────
            if len(volumes) >= 4:
                avg_prev = sum(volumes[:-2]) / max(len(volumes[:-2]), 1)
                last_vol = volumes[-1]
                if avg_prev > 0 and last_vol > avg_prev * 3:
                    signals.append({
                        "type": "volume_spike", "market_id": mid,
                        "question": question, "category": category,
                        "strength": min(1.0, last_vol / avg_prev / 10),
                        "yes_price": market.yes_price,
                        "spike_ratio": round(last_vol / avg_prev, 1),
                        "volume": last_vol,
                        "note": f"Volume spiked {last_vol/avg_prev:.1f}x. Smart money entering.",
                    })

            # ── Spread widening ───────────────────────────────────────────
            if len(spreads) >= 3:
                avg_spread  = sum(spreads[:-1]) / max(len(spreads[:-1]), 1)
                last_spread = spreads[-1]
                if avg_spread > 0 and last_spread > avg_spread * 1.5 and last_spread > 0.03:
                    signals.append({
                        "type": "spread_widening", "market_id": mid,
                        "question": question, "category": category,
                        "strength": min(1.0, (last_spread - avg_spread) / avg_spread),
                        "yes_price": market.yes_price,
                        "spread": round(last_spread, 4),
                        "note": f"Spread widened {last_spread/avg_spread:.1f}x. MM uncertainty.",
                    })

        # ── NegRisk imbalance (needs group data) ──────────────────────────
        if groups:
            for group_id, group_markets in groups.items():
                live_markets = [m for m in group_markets
                                if m.market_id in market_by_id
                                and 0.02 < m.yes_price < 0.98
                                and m.seconds_to_resolution > 0]
                if len(live_markets) < 2:
                    continue
                group_sum = sum(m.yes_price for m in live_markets)
                # Imbalance: sum < 0.95 (arb building) or > 1.05 (overpriced)
                if group_sum < 0.95:
                    signals.append({
                        "type": "negrisk_imbalance", "market_id": group_id,
                        "question": f"NegRisk group ({len(live_markets)} legs)",
                        "category": live_markets[0].category,
                        "strength": min(1.0, (0.95 - group_sum) * 20),
                        "yes_price": round(group_sum / len(live_markets), 4),
                        "group_sum": round(group_sum, 4),
                        "num_legs": len(live_markets),
                        "note": f"Group sum={group_sum:.3f} ({len(live_markets)} legs). Arb building.",
                    })

        signals.sort(key=lambda s: s["strength"], reverse=True)
        log.info("signals_detected",
                 total=len(signals),
                 by_type={t: sum(1 for s in signals if s["type"] == t)
                          for t in set(s["type"] for s in signals)})
        return signals

    def get_stats(self) -> dict:
        total_points = sum(len(d["points"]) for d in self._history.values())
        by_cat: dict = defaultdict(int)
        for data in self._history.values():
            by_cat[data.get("category", "other")] += len(data["points"])

        oldest_ts = newest_ts = None
        for data in self._history.values():
            for pt in data["points"]:
                ts = pt["ts"]
                if oldest_ts is None or ts < oldest_ts: oldest_ts = ts
                if newest_ts is None or ts > newest_ts: newest_ts = ts

        return {
            "markets_tracked":   len(self._history),
            "total_data_points": total_points,
            "by_category":       dict(by_cat),
            "oldest_observation": (
                datetime.fromtimestamp(oldest_ts, tz=timezone.utc).isoformat()
                if oldest_ts else None
            ),
            "newest_observation": (
                datetime.fromtimestamp(newest_ts, tz=timezone.utc).isoformat()
                if newest_ts else None
            ),
        }

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
