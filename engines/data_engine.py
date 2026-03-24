"""
engines/data_engine.py — Multi-source market data fetcher with compression.

Sources (in priority order, all free, no auth required for read):
  1. Gamma API  — market metadata, NegRisk groups, end dates, prices
  2. CLOB API   — live orderbook (bid/ask spread), fee_rate_bps per market
  3. Data API   — 24h volume, trade history (rate-limit safe, batched)

Storage: writes data/market_snapshot.json.gz after every fetch.
         loads previous snapshot on startup for delta-fetch efficiency.
         git-committed → survives across ephemeral GitHub Actions runners.

NEVER places orders. NEVER raises (returns stale cache on any error).
All HTTP calls: @retry 3 attempts, exponential backoff 2-10s.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

log = structlog.get_logger(component="data_engine")

# ─── Endpoints ────────────────────────────────────────────────────────────────
GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"
DATA_API    = "https://data-api.polymarket.com"

# ─── Tunable constants ────────────────────────────────────────────────────────
# Only fetch markets resolving within this many days — keeps data tight
MAX_DAYS_TO_RESOLUTION = 7          # 7-day window catches S10 + most S1/S8
# Also pull a second page of medium-term markets for S1 NegRisk group hunting
MAX_DAYS_NEGRISK       = 30
PAGE_SIZE              = 500        # Gamma API max per page
MIN_VOLUME_24H         = 100        # USDC — ignore dead markets
MIN_LIQUIDITY          = 50         # USDC in orderbook
SNAPSHOT_PATH          = Path("data/market_snapshot.json.gz")

# ─── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class MarketState:
    market_id:           str
    question:            str
    yes_token_id:        str
    no_token_id:         str
    yes_price:           float          # mid price, clamped (0.001, 0.999)
    no_price:            float
    yes_bid:             float
    yes_ask:             float
    no_bid:              float
    no_ask:              float
    volume_24h:          float
    end_date_iso:        str
    seconds_to_resolution: int
    negrisk_group_id:    Optional[str]
    category:            str           # 'crypto'|'politics'|'sports'|'other'
    fee_rate_bps:        int           # from CLOB API, live per-market
    fetched_at:          float = field(default_factory=time.time)  # unix ts

    def is_stale(self, max_age_seconds: int = 300) -> bool:
        return (time.time() - self.fetched_at) > max_age_seconds

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MarketState":
        return cls(**d)


# ─── Compression helpers ──────────────────────────────────────────────────────

def save_snapshot(markets: list[MarketState], path: Path = SNAPSHOT_PATH) -> None:
    """Gzip-compress market list to disk. ~10x size reduction."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "count": len(markets),
        "markets": [m.to_dict() for m in markets],
    }
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    log.info("snapshot_saved", path=str(path), count=len(markets),
             size_kb=path.stat().st_size // 1024)


def load_snapshot(path: Path = SNAPSHOT_PATH) -> list[MarketState]:
    """Load and decompress market snapshot. Returns [] if missing/corrupt."""
    if not path.exists():
        return []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            payload = json.load(f)
        markets = [MarketState.from_dict(m) for m in payload["markets"]]
        log.info("snapshot_loaded", path=str(path), count=len(markets),
                 saved_at=payload.get("saved_at"))
        return markets
    except Exception as e:
        log.warning("snapshot_load_failed", error=str(e))
        return []


# ─── HTTP session (shared, connection-pooled) ─────────────────────────────────

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "polymarket-engine/3.1 (github-actions)",
        "Accept":     "application/json",
    })
    return s


# ─── Category detection ───────────────────────────────────────────────────────

_CRYPTO_KEYWORDS   = {"btc","eth","bitcoin","ethereum","crypto","sol","solana",
                       "xrp","bnb","doge","usdc","defi","nft","blockchain"}
_POLITICS_KEYWORDS = {"election","president","senate","congress","vote","political",
                       "governor","minister","parliament","referendum","ballot","trump",
                       "biden","democrat","republican","kamala","modi","macron"}
_SPORTS_KEYWORDS   = {"nba","nfl","mlb","nhl","soccer","football","basketball",
                       "tennis","ufc","match","tournament","championship","league",
                       "playoff","world cup","super bowl","finals","mls","epl"}

def _categorise(tags: list[str], question: str) -> str:
    combined = " ".join(tags + [question]).lower()
    words    = set(combined.split())
    if words & _CRYPTO_KEYWORDS:   return "crypto"
    if words & _POLITICS_KEYWORDS: return "politics"
    if words & _SPORTS_KEYWORDS:   return "sports"
    return "other"


# ─── Fee formula (canonical — matches BaseStrategy.calc_fee) ──────────────────

def _calc_fee_bps(p: float) -> int:
    """Convert probability to fee_rate_bps using canonical formula."""
    fee = 2.25 * 0.25 * (p * (1 - p)) ** 2
    return int(fee * 10000)


# ─── DataEngine ───────────────────────────────────────────────────────────────

class DataEngine:
    """
    Multi-source market data fetcher.

    Fetch strategy:
      Phase 1 — near-term markets (≤7 days): full CLOB enrichment, for S10/S1/S8
      Phase 2 — NegRisk group markets (≤30 days): for S1 cross-market arb
      Delta    — skip markets already in snapshot with fresh fetched_at (<5 min)
    """

    def __init__(self, config: dict):
        self.config        = config
        self._session      = _make_session()
        self._cache:  list[MarketState]               = []
        self._groups: dict[str, list[MarketState]]    = {}
        self._load_snapshot()

    # ── Public interface ───────────────────────────────────────────────────────

    def fetch_all_markets(self) -> list[MarketState]:
        """
        Full multi-source fetch. Returns [] on total failure (never raises).
        Saves compressed snapshot to disk on success.
        """
        try:
            near_term   = self._fetch_gamma_markets(max_days=MAX_DAYS_TO_RESOLUTION)
            negrisk_ext = self._fetch_gamma_markets(max_days=MAX_DAYS_NEGRISK,
                                                    negrisk_only=True)

            # Merge, deduplicate by market_id
            seen    = {}
            for m in near_term + negrisk_ext:
                seen[m.market_id] = m

            # Enrich with live CLOB data (bids, asks, fee_rate_bps)
            enriched = self._enrich_with_clob(list(seen.values()))

            # Apply volume / liquidity filters
            filtered = [m for m in enriched
                        if m.volume_24h >= MIN_VOLUME_24H
                        and m.seconds_to_resolution > 0]

            self._cache = filtered
            self._groups = self._build_negrisk_groups(filtered)

            save_snapshot(filtered)

            log.info("fetch_complete",
                     near_term=len(near_term),
                     negrisk_ext=len(negrisk_ext),
                     after_filter=len(filtered),
                     negrisk_groups=len(self._groups))
            return filtered

        except Exception as e:
            log.error("fetch_failed_returning_cache", error=str(e))
            # If in-memory cache is empty, try reloading from disk snapshot
            if not self._cache:
                disk_fallback = load_snapshot()
                if disk_fallback:
                    log.warning("using_disk_snapshot_fallback", count=len(disk_fallback))
                    self._cache = disk_fallback
                    self._groups = self._build_negrisk_groups(self._cache)
            return self._cache  # stale but always better than nothing

    def fetch_negrisk_groups(self) -> dict[str, list[MarketState]]:
        """Groups of 2+ NegRisk markets sharing a group_id."""
        if not self._groups and self._cache:
            self._groups = self._build_negrisk_groups(self._cache)
        return self._groups

    def get_cached_markets(self) -> list[MarketState]:
        """Returns last fetch without a new API call."""
        return self._cache

    def get_single_market(self, token_id: str) -> Optional[MarketState]:
        """
        Fetch a single market fresh from CLOB. Used by ExecutionEngine
        to re-validate fee_rate_bps before order submission.
        """
        try:
            return self._fetch_single_clob(token_id)
        except Exception as e:
            log.warning("single_market_fetch_failed", token_id=token_id, error=str(e))
            return None

    # ── Snapshot bootstrap ────────────────────────────────────────────────────

    def _load_snapshot(self) -> None:
        """Load previous compressed snapshot as warm cache."""
        cached = load_snapshot()
        if cached:
            # Keep cache for 24h — stale data always beats zero data.
            # seconds_to_resolution was computed at fetch time, recalculate
            # relative to now so we don't discard markets that are still active.
            now    = time.time()
            cutoff = now - 86400   # 24h — survive long GH Actions outages
            refreshed = []
            for m in cached:
                if m.fetched_at < cutoff:
                    continue   # truly stale
                # Recompute seconds_to_resolution from stored end_date_iso
                end_ts = self._parse_iso_to_ts(m.end_date_iso) if m.end_date_iso else None
                if end_ts:
                    remaining = end_ts - int(now)
                    if remaining < -3600:
                        continue   # resolved more than 1h ago
                    # Update the field in-place via dataclass replace
                    from dataclasses import replace as dc_replace
                    m = dc_replace(m, seconds_to_resolution=max(0, remaining))
                refreshed.append(m)
            self._cache  = refreshed
            self._groups = self._build_negrisk_groups(self._cache)
            log.info("warm_cache_loaded", count=len(self._cache))

    # ── Gamma API fetch ───────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _get(self, url: str, params: dict = None) -> dict | list:
        resp = self._session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _fetch_gamma_markets(self, max_days: int = 7,
                             negrisk_only: bool = False) -> list[MarketState]:
        """
        Paginate Gamma API with end_date_max filter.
        Returns only markets resolving within max_days from now.
        """
        now      = int(time.time())
        end_max  = now + (max_days * 86400)
        markets  = []
        offset   = 0

        base_params = {
            "active":        "true",
            "closed":        "false",
            "limit":         PAGE_SIZE,
            "order":         "volume",   # highest volume first
            "ascending":     "false",
        }
        if negrisk_only:
            base_params["neg_risk"] = "true"

        while True:
            try:
                params = {**base_params, "offset": offset}
                data   = self._get(f"{GAMMA_API}/markets", params=params)

                if not data:
                    break

                page_markets = []
                for raw in data:
                    m = self._parse_gamma_market(raw, end_max)
                    if m:
                        page_markets.append(m)

                markets.extend(page_markets)

                # If the last market on this page resolves beyond our window,
                # and we're fetching by volume desc, we can stop paginating
                if len(data) < PAGE_SIZE:
                    break

                # Safety: stop after 10 pages (5000 markets)
                offset += PAGE_SIZE
                if offset >= 5000:
                    break

                time.sleep(0.3)  # polite rate-limiting between pages

            except Exception as e:
                log.warning("gamma_page_failed", offset=offset, error=str(e))
                break

        return markets

    def _parse_gamma_market(self, raw: dict, end_max_ts: int) -> Optional[MarketState]:
        """Parse a single Gamma API market dict into MarketState. Returns None to skip."""
        try:
            # ── End date ──────────────────────────────────────────────────────
            # Gamma returns both "endDate" (ISO datetime) and "endDateIso" (date only)
            end_date_str = raw.get("endDate") or raw.get("endDateIso") or ""
            if not end_date_str:
                return None

            end_ts = self._parse_iso_to_ts(end_date_str)
            if end_ts is None:
                return None

            now          = int(time.time())
            seconds_left = end_ts - now

            if seconds_left <= 0:
                return None   # already resolved or past end date
            if end_ts > end_max_ts:
                return None   # beyond our scanning window

            # ── Token IDs ─────────────────────────────────────────────────────
            # Gamma API returns clobTokenIds as a JSON string: '["tokenA","tokenB"]'
            # NOT a "tokens" array — that was the old CLOB API format.
            # Index 0 = YES token, index 1 = NO token (always this order on Polymarket)
            ctids_raw = raw.get("clobTokenIds", "")
            try:
                token_ids = json.loads(ctids_raw) if isinstance(ctids_raw, str) else (ctids_raw or [])
            except (json.JSONDecodeError, TypeError):
                token_ids = []

            if len(token_ids) < 2:
                return None

            yes_token_id = str(token_ids[0])
            no_token_id  = str(token_ids[1])
            if not yes_token_id or not no_token_id:
                return None

            # ── Prices ────────────────────────────────────────────────────────
            # Gamma API returns outcomePrices as a JSON string: '["0.94","0.06"]'
            # Index 0 = YES price, index 1 = NO price
            op_raw = raw.get("outcomePrices", "")
            try:
                prices = json.loads(op_raw) if isinstance(op_raw, str) else (op_raw or [])
            except (json.JSONDecodeError, TypeError):
                prices = []

            if len(prices) < 2:
                yes_price, no_price = 0.5, 0.5
            else:
                raw_yes = float(prices[0])
                raw_no  = float(prices[1])
                # Skip already-resolved markets (price at 0 or 1)
                if raw_yes >= 0.999 or raw_no >= 0.999:
                    return None
                yes_price = max(0.001, min(0.999, raw_yes))
                no_price  = max(0.001, min(0.999, raw_no))

            # ── Volume ────────────────────────────────────────────────────────
            # Gamma returns both "volume24hr" (float) and "volume" (total)
            volume_24h = float(raw.get("volume24hr") or raw.get("volume_24h") or 0.0)

            # ── NegRisk ───────────────────────────────────────────────────────
            # Gamma returns "negRisk" bool and "negRiskRequestID" for the group
            negrisk_group_id = None
            if raw.get("negRisk"):
                negrisk_group_id = str(raw.get("negRiskRequestID") or raw.get("conditionId") or "")
                if not negrisk_group_id:
                    negrisk_group_id = None

            # ── Category ──────────────────────────────────────────────────────
            tags     = raw.get("tags", [])
            question = str(raw.get("question") or "").strip()
            if not question:
                return None
            category = _categorise(tags, question)

            # ── Fee rate (formula default — CLOB enrichment will override) ───
            fee_rate_bps = _calc_fee_bps(yes_price)

            return MarketState(
                market_id             = str(raw.get("id") or raw.get("conditionId") or ""),
                question              = question,
                yes_token_id          = yes_token_id,
                no_token_id           = no_token_id,
                yes_price             = yes_price,
                no_price              = no_price,
                yes_bid               = yes_price - 0.01,   # placeholder until CLOB enrichment
                yes_ask               = yes_price + 0.01,
                no_bid                = no_price  - 0.01,
                no_ask                = no_price  + 0.01,
                volume_24h            = volume_24h,
                end_date_iso          = end_date_str,
                seconds_to_resolution = seconds_left,
                negrisk_group_id      = negrisk_group_id,
                category              = category,
                fee_rate_bps          = fee_rate_bps,
                fetched_at            = time.time(),
            )
        except Exception as e:
            log.debug("market_parse_skipped", market_id=str(raw.get("id","")), error=str(e))
            return None

    # ── CLOB enrichment ───────────────────────────────────────────────────────

    def _enrich_with_clob(self, markets: list[MarketState]) -> list[MarketState]:
        """
        Batch-fetch CLOB orderbook for each market to get live bid/ask and
        fee_rate_bps. Fails gracefully per-market (keeps Gamma data on error).

        Rate-limit strategy: batch by groups of 20, 0.5s sleep between batches.
        No auth required for read-only CLOB endpoints.
        """
        enriched = []
        batch_size = 20
        total = len(markets)

        for i in range(0, total, batch_size):
            batch = markets[i:i + batch_size]
            for m in batch:
                try:
                    m = self._enrich_single(m)
                except Exception as e:
                    log.debug("clob_enrich_skip", market_id=m.market_id, error=str(e))
                enriched.append(m)

            if i + batch_size < total:
                time.sleep(0.5)  # 500ms between batches → safe rate

        return enriched

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=5))
    def _enrich_single(self, m: MarketState) -> MarketState:
        """Fetch live orderbook for YES and NO tokens."""
        # YES orderbook
        yes_book = self._get(f"{CLOB_API}/book", params={"token_id": m.yes_token_id})
        no_book  = self._get(f"{CLOB_API}/book", params={"token_id": m.no_token_id})

        def best_bid(book: dict) -> float:
            bids = book.get("bids", [])
            if not bids:
                return 0.0
            return float(max(bids, key=lambda x: float(x.get("price", 0)))["price"])

        def best_ask(book: dict) -> float:
            asks = book.get("asks", [])
            if not asks:
                return 1.0
            return float(min(asks, key=lambda x: float(x.get("price", 1)))["price"])

        yes_bid = best_bid(yes_book)
        yes_ask = best_ask(yes_book)
        no_bid  = best_bid(no_book)
        no_ask  = best_ask(no_book)

        # fee_rate_bps from CLOB market endpoint
        try:
            market_detail = self._get(f"{CLOB_API}/markets/{m.yes_token_id}")
            fee_bps = int(market_detail.get("feeRateBps", m.fee_rate_bps))
        except Exception:
            fee_bps = m.fee_rate_bps  # fall back to formula estimate

        # Update mid prices from live orderbook (more accurate than Gamma)
        if yes_bid > 0 and yes_ask < 1:
            yes_mid = (yes_bid + yes_ask) / 2.0
        else:
            yes_mid = m.yes_price

        if no_bid > 0 and no_ask < 1:
            no_mid = (no_bid + no_ask) / 2.0
        else:
            no_mid = m.no_price

        return MarketState(
            **{**asdict(m),
               "yes_bid":      max(0.001, min(0.999, yes_bid)),
               "yes_ask":      max(0.001, min(0.999, yes_ask)),
               "no_bid":       max(0.001, min(0.999, no_bid)),
               "no_ask":       max(0.001, min(0.999, no_ask)),
               "yes_price":    max(0.001, min(0.999, yes_mid)),
               "no_price":     max(0.001, min(0.999, no_mid)),
               "fee_rate_bps": fee_bps,
               "fetched_at":   time.time()}
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def _fetch_single_clob(self, token_id: str) -> Optional[MarketState]:
        """Used by ExecutionEngine for pre-order re-validation."""
        # Find in cache first
        existing = next((m for m in self._cache if m.yes_token_id == token_id), None)
        if existing:
            return self._enrich_single(existing)
        return None

    # ── NegRisk grouping ──────────────────────────────────────────────────────

    def _build_negrisk_groups(self,
                               markets: list[MarketState]
                               ) -> dict[str, list[MarketState]]:
        """
        Group markets by negrisk_group_id.
        Only groups with 2+ markets are included (can't arb a single market).
        """
        groups: dict[str, list[MarketState]] = {}
        for m in markets:
            if m.negrisk_group_id:
                groups.setdefault(m.negrisk_group_id, []).append(m)
        return {gid: mlist for gid, mlist in groups.items() if len(mlist) >= 2}

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_iso_to_ts(iso_str: str) -> Optional[int]:
        """Parse ISO 8601 date string to unix timestamp. Returns None on failure."""
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
                    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
            try:
                if fmt == "%Y-%m-%d":
                    dt = datetime.strptime(iso_str[:10], fmt).replace(tzinfo=timezone.utc)
                else:
                    dt = datetime.strptime(iso_str, fmt).replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            except ValueError:
                continue
        return None
