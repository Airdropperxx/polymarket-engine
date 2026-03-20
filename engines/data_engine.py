"""
engines/data_engine.py
======================
Market data fetcher — REST polling, MVP version.

Fetches all active Polymarket markets every poll_interval seconds.
Caches last successful fetch — returns stale cache on error (never raises).

NEVER places orders. Data reads only.
WebSocket upgrade happens in Phase 3 (persistent process on VPS).

KEY FIX (v3.1): fee_rate_bps is now included in MarketState.
ExecutionEngine reads it directly from MarketState instead of making
a separate API call per order. Eliminates the phantom get_fee_rate_bps()
method that doesn't exist in py-clob-client 0.16.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

log = structlog.get_logger()

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"


# ---------------------------------------------------------------------------
# MarketState
# ---------------------------------------------------------------------------

@dataclass
class MarketState:
    """Point-in-time snapshot of a single Polymarket market."""

    market_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_price: float          # best ask for YES  (clamped to 0.001–0.999)
    no_price: float           # best ask for NO   (clamped to 0.001–0.999)
    yes_bid: float            # best bid for YES  (>= 0.0)
    no_bid: float             # best bid for NO   (>= 0.0)
    volume_24h: float         # USDC traded in last 24 h
    end_date_iso: str         # ISO 8601 resolution time
    seconds_to_resolution: int  # negative means already resolved
    negrisk_group_id: Optional[str]
    category: str             # 'crypto' | 'politics' | 'sports' | 'other'
    fee_rate_bps: int = 200   # Taker fee in basis points (200 = 2%).
                              # Fetched from API; ExecutionEngine reads this.
                              # Never hardcode 0 — use 200 as safe overestimate default.
    fetched_at: float = 0.0   # Unix timestamp of this snapshot (for staleness check)

    def __post_init__(self) -> None:
        self.yes_price = max(0.001, min(0.999, float(self.yes_price)))
        self.no_price  = max(0.001, min(0.999, float(self.no_price)))
        self.yes_bid   = max(0.0, float(self.yes_bid))
        self.no_bid    = max(0.0, float(self.no_bid))
        if self.fetched_at == 0.0:
            self.fetched_at = time.time()

    @property
    def is_stale(self, max_age_seconds: int = 300) -> bool:
        """True if this snapshot is older than max_age_seconds (default 5 min)."""
        return (time.time() - self.fetched_at) > max_age_seconds


# ---------------------------------------------------------------------------
# DataEngine
# ---------------------------------------------------------------------------

class DataEngine:
    """
    Polls Polymarket REST API and caches market data.
    All strategies read from the cache — no duplicate API calls per cycle.
    """

    def __init__(self, config: dict) -> None:
        self._config = config
        self._cache: list[MarketState] = []
        self._negrisk_cache: dict[str, list[MarketState]] = {}
        self._last_fetch: Optional[float] = None
        self._headers = {"Content-Type": "application/json"}

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def fetch_all_markets(self) -> list[MarketState]:
        """
        Fetch all active markets from Polymarket REST API.
        Updates internal cache. Returns stale cache on any error (never raises).
        """
        log.info("data_engine.fetch_start")
        try:
            raw_markets = self._fetch_markets_paginated()
            parsed = [self._parse_market(m) for m in raw_markets if self._is_tradeable(m)]
            self._cache = [m for m in parsed if m is not None]
            self._last_fetch = time.time()
            log.info("data_engine.fetch_done", count=len(self._cache))
        except Exception as exc:
            log.error("data_engine.fetch_error", error=str(exc))
            # Return stale cache — better than crashing the cycle
        return self._cache

    def fetch_negrisk_groups(self) -> dict[str, list[MarketState]]:
        """
        Group markets by their NegRisk group ID.
        Only groups with 2+ markets are returned (single-market groups aren't arb opportunities).
        Uses current cache — call fetch_all_markets() first.
        """
        groups: dict[str, list[MarketState]] = {}
        for market in self._cache:
            if market.negrisk_group_id:
                groups.setdefault(market.negrisk_group_id, []).append(market)
        self._negrisk_cache = {k: v for k, v in groups.items() if len(v) >= 2}
        log.info("data_engine.negrisk_groups", count=len(self._negrisk_cache))
        return self._negrisk_cache

    def get_cached_markets(self) -> list[MarketState]:
        """Return last fetched markets without triggering a new API call."""
        return self._cache

    def fetch_market_by_id(self, market_id: str) -> Optional[MarketState]:
        """Check cache first; fetch from CLOB API if not found."""
        for m in self._cache:
            if m.market_id == market_id:
                return m
        try:
            raw = self._get(f"{CLOB_BASE}/markets/{market_id}")
            return self._parse_market(raw)
        except Exception as exc:
            log.error("data_engine.single_fetch_error", market_id=market_id, error=str(exc))
            return None

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _fetch_markets_paginated(self) -> list[dict]:
        """Paginate through all active Polymarket markets."""
        all_markets: list[dict] = []
        next_cursor: Optional[str] = None

        for _page in range(100):  # safety cap
            params: dict = {"limit": 500, "active": "true", "closed": "false"}
            if next_cursor:
                params["next_cursor"] = next_cursor

            data = self._get(f"{GAMMA_BASE}/markets", params=params)

            if isinstance(data, list):
                all_markets.extend(data)
                break
            elif isinstance(data, dict):
                all_markets.extend(data.get("data", []))
                next_cursor = data.get("next_cursor")
                if not next_cursor or next_cursor == "LTE=":
                    break

        return all_markets

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _get(self, url: str, params: Optional[dict] = None) -> dict:
        """HTTP GET with automatic exponential-backoff retry on failure."""
        resp = requests.get(url, headers=self._headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _parse_market(self, raw: dict) -> Optional[MarketState]:
        """Convert a raw API dict into a MarketState. Returns None if data is invalid."""
        try:
            tokens   = raw.get("tokens", [])
            yes_tok  = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), {})
            no_tok   = next((t for t in tokens if t.get("outcome", "").upper() == "NO"),  {})

            end_date = raw.get("endDate") or raw.get("end_date_iso", "")
            secs_left = _seconds_until(end_date)
            if secs_left < 0:
                return None  # already resolved

            # fee_rate_bps: prefer explicit field from API, else compute from formula
            raw_bps = raw.get("feeRateBps") or raw.get("fee_rate_bps")
            if raw_bps is not None:
                fee_bps = int(raw_bps)
            else:
                # Compute from canonical formula as fallback
                p = float(yes_tok.get("price", 0.5))
                fee_fraction = 2.25 * 0.25 * (p * (1.0 - p)) ** 2
                fee_bps = max(1, int(round(fee_fraction * 10000)))

            return MarketState(
                market_id=raw.get("conditionId") or raw.get("id", ""),
                question=raw.get("question", ""),
                yes_token_id=yes_tok.get("token_id", ""),
                no_token_id=no_tok.get("token_id", ""),
                yes_price=float(yes_tok.get("price", 0.5)),
                no_price=float(no_tok.get("price", 0.5)),
                yes_bid=float(yes_tok.get("bid", 0.0)),
                no_bid=float(no_tok.get("bid", 0.0)),
                volume_24h=float(raw.get("volume24hr", 0.0)),
                end_date_iso=end_date,
                seconds_to_resolution=secs_left,
                negrisk_group_id=raw.get("negRiskGroupId") or raw.get("groupItemTitle"),
                category=_categorise(raw.get("tags", []), raw.get("question", "")),
                fee_rate_bps=fee_bps,
                fetched_at=time.time(),
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("data_engine.parse_skip", error=str(exc))
            return None

    @staticmethod
    def _is_tradeable(raw: dict) -> bool:
        """Only process markets that are active, not closed, and have a question."""
        return (
            raw.get("active", False)
            and not raw.get("closed", True)
            and bool(raw.get("question", ""))
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _seconds_until(end_date_iso: str) -> int:
    """Parse ISO date string and return seconds until that time. Returns -1 if unparseable."""
    if not end_date_iso:
        return -1
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(end_date_iso[:19], fmt[:19]).replace(tzinfo=timezone.utc)
            return max(-1, int((dt - datetime.now(timezone.utc)).total_seconds()))
        except ValueError:
            continue
    return -1


def _categorise(tags: list, question: str) -> str:
    """Determine market category from tags list and question text."""
    combined = " ".join(str(t).lower() for t in tags) + " " + question.lower()
    if any(w in combined for w in ("bitcoin", "btc", "eth", "ethereum", "crypto", "sol", "solana")):
        return "crypto"
    if any(w in combined for w in ("election", "president", "senate", "congress", "vote", "political")):
        return "politics"
    if any(w in combined for w in (
        "nba", "nfl", "mlb", "nhl", "soccer", "football", "basketball",
        "baseball", "tennis", "golf", "ufc", "mma", "match", "game",
        "championship", "tournament", "cup",
    )):
        return "sports"
    return "other"
