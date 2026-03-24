"""
engines/data_engine.py — Market data fetcher.

Source: Gamma API only (https://gamma-api.polymarket.com)
  - Gives us: question, endDate, outcomePrices, clobTokenIds, bestBid, bestAsk,
    lastTradePrice, spread, volume24hr, makerBaseFee, takerBaseFee, negRisk, etc.
  - Everything needed for strategy signals is already in Gamma.
  - CLOB enrichment removed — it was serial, slow, and caused job timeouts.
    CLOB is only called for the 1-2 markets we actually execute a live trade on.

Architecture (decoupled):
  DataEngine fetches and stores data independently.
  Strategies read from DataEngine. Engines read from DataEngine.
  A DataEngine failure never affects StateEngine or resolution checking.

Storage: data/market_snapshot.json.gz — survives across GH Actions runners.
Cache: 24h warm cache so a Gamma outage does not stop the cycle.

NEVER places orders. NEVER raises (returns cache on any error).
"""
from __future__ import annotations

import gzip, json, time
from dataclasses import asdict, dataclass, field, replace as dc_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests, structlog
from tenacity import retry, stop_after_attempt, wait_exponential

log = structlog.get_logger(component="data_engine")

# ── Constants ──────────────────────────────────────────────────────────────────
GAMMA_API              = "https://gamma-api.polymarket.com"
CLOB_API               = "https://clob.polymarket.com"
SNAPSHOT_PATH          = Path("data/market_snapshot.json.gz")
PAGE_SIZE              = 500       # Gamma API max per page
MAX_DAYS_TO_RESOLUTION = 7         # near-term window for S10/S8
MAX_DAYS_NEGRISK       = 30        # wider window for S1 NegRisk groups
MIN_VOLUME_24H         = 50        # USDC — ignore completely dead markets
CACHE_MAX_AGE_SECS     = 86400     # 24h — survive long GH Actions outages

# ── Snapshot helpers ───────────────────────────────────────────────────────────

@dataclass
class MarketState:
    market_id:             str
    question:              str
    yes_token_id:          str
    no_token_id:           str
    yes_price:             float
    no_price:              float
    yes_bid:               float  = 0.0
    yes_ask:               float  = 0.0
    no_bid:                float  = 0.0
    no_ask:                float  = 0.0
    spread:                float  = 0.0
    volume_24h:            float  = 0.0
    end_date_iso:          str    = ""
    seconds_to_resolution: int    = 0
    negrisk_group_id:      Optional[str] = None
    category:              str    = "other"
    fee_rate_bps:          int    = 200
    fetched_at:            float  = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MarketState":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)

    def is_stale(self, max_age_sec: int = 300) -> bool:
        return (time.time() - self.fetched_at) > max_age_sec


def save_snapshot(markets: list[MarketState], path: Path = SNAPSHOT_PATH) -> None:
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
    if not path.exists():
        return []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            payload = json.load(f)
        markets = [MarketState.from_dict(m) for m in payload.get("markets", [])]
        log.info("snapshot_loaded", path=str(path), count=len(markets),
                 saved_at=payload.get("saved_at"))
        return markets
    except Exception as e:
        log.warning("snapshot_load_failed", error=str(e))
        return []


# ── HTTP session ────────────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "polymarket-engine/4.0 (github-actions)",
        "Accept": "application/json",
    })
    return s


# ── Category detection ──────────────────────────────────────────────────────────

_SPORTS_KW  = {"nfl","nba","mlb","nhl","fifa","soccer","football","basketball",
               "baseball","tennis","mma","ufc","cricket","f1","nascar","esports",
               "league","match","game","tournament","championship","cup","série",
               "score","win","beat","champion","team","player","season"}
_CRYPTO_KW  = {"bitcoin","btc","eth","ethereum","solana","sol","crypto","token",
               "blockchain","defi","nft","polygon","matic","usdc","price","ath",
               "market cap","altcoin","binance","coinbase"}
_POLITICS_KW= {"president","election","vote","senate","congress","democrat","republican",
               "trump","biden","harris","minister","parliament","governor","mayor",
               "policy","law","bill","court","supreme","fed","rate"}

def _categorise(tags: list, question: str) -> str:
    q = question.lower()
    tag_str = " ".join(str(t).lower() for t in tags)
    combined = q + " " + tag_str
    if any(kw in combined for kw in _CRYPTO_KW):   return "crypto"
    if any(kw in combined for kw in _POLITICS_KW): return "politics"
    if any(kw in combined for kw in _SPORTS_KW):   return "sports"
    return "other"


# ── Fee formula ────────────────────────────────────────────────────────────────

def _calc_fee_bps(yes_price: float) -> int:
    """Estimate fee bps from price using Polymarket canonical formula."""
    p = max(0.001, min(0.999, yes_price))
    rate = 2.25 * (p * (1.0 - p)) ** 2   # matches BaseStrategy.calc_fee
    return max(1, min(500, int(rate * 10_000)))


# ── DataEngine ──────────────────────────────────────────────────────────────────

class DataEngine:
    """
    Fetches market data from Gamma API only — no CLOB batch calls.
    Gamma already provides bestBid, bestAsk, lastTradePrice, spread, fee data.
    CLOB is only used for single-market pre-execution re-validation.

    Completely decoupled from engines: a fetch failure never propagates.
    """

    def __init__(self, config: dict):
        self.config   = config
        self._session = _make_session()
        self._cache:  list[MarketState]            = []
        self._groups: dict[str, list[MarketState]] = {}
        self._load_snapshot()

    # ── Public interface ──────────────────────────────────────────────────────

    def fetch_all_markets(self) -> list[MarketState]:
        """
        Fetch from Gamma API. Returns cache on any failure.
        Fast: single paginated Gamma call, no per-market CLOB calls.
        """
        try:
            near_term   = self._fetch_gamma(max_days=MAX_DAYS_TO_RESOLUTION)
            negrisk_ext = self._fetch_gamma(max_days=MAX_DAYS_NEGRISK, negrisk_only=True)

            seen = {}
            for m in near_term + negrisk_ext:
                if m.market_id:
                    seen[m.market_id] = m

            filtered = [
                m for m in seen.values()
                if m.volume_24h >= MIN_VOLUME_24H
                and m.seconds_to_resolution > 0
            ]

            self._cache  = filtered
            self._groups = self._build_groups(filtered)
            save_snapshot(filtered)

            log.info("fetch_complete",
                     near_term=len(near_term),
                     negrisk_ext=len(negrisk_ext),
                     after_filter=len(filtered),
                     negrisk_groups=len(self._groups))
            return filtered

        except Exception as e:
            log.error("fetch_failed_returning_cache", error=str(e))
            if not self._cache:
                # last-resort: reload from disk
                disk = load_snapshot()
                if disk:
                    self._cache  = disk
                    self._groups = self._build_groups(disk)
                    log.warning("using_disk_fallback", count=len(disk))
            return self._cache

    def fetch_negrisk_groups(self) -> dict[str, list[MarketState]]:
        if not self._groups and self._cache:
            self._groups = self._build_groups(self._cache)
        return self._groups

    def get_cached_markets(self) -> list[MarketState]:
        return self._cache

    def get_single_market(self, token_id: str) -> Optional[MarketState]:
        """
        Return a market by token_id. Used by ExecutionEngine before a live order.
        First checks cache; optionally re-enriches from CLOB for fresh bid/ask.
        """
        try:
            existing = next((m for m in self._cache if m.yes_token_id == token_id), None)
            if existing and not existing.is_stale(300):
                return existing
            # For live trading, re-enrich from CLOB
            if existing:
                return self._enrich_single_clob(existing)
            return None
        except Exception as e:
            log.warning("get_single_market_failed", token_id=token_id, error=str(e))
            return None

    # ── Snapshot bootstrap ────────────────────────────────────────────────────

    def _load_snapshot(self) -> None:
        cached = load_snapshot()
        if not cached:
            return
        now    = time.time()
        cutoff = now - CACHE_MAX_AGE_SECS
        refreshed = []
        for m in cached:
            if m.fetched_at < cutoff:
                continue
            # Recompute seconds_to_resolution relative to now
            end_ts = _parse_iso_to_ts(m.end_date_iso) if m.end_date_iso else None
            if end_ts:
                remaining = end_ts - int(now)
                if remaining < -3600:
                    continue  # resolved more than 1h ago
                m = dc_replace(m, seconds_to_resolution=max(0, remaining))
            refreshed.append(m)
        self._cache  = refreshed
        self._groups = self._build_groups(refreshed)
        log.info("warm_cache_loaded", count=len(refreshed))

    # ── Gamma API ─────────────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8))
    def _get(self, url: str, params: dict = None) -> dict | list:
        resp = self._session.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _fetch_gamma(self, max_days: int = 7,
                     negrisk_only: bool = False) -> list[MarketState]:
        """Paginate Gamma API. All market data we need is in Gamma responses."""
        now     = int(time.time())
        end_max = now + (max_days * 86400)
        markets = []
        offset  = 0

        base_params = {
            "active":    "true",
            "closed":    "false",
            "limit":     PAGE_SIZE,
            "order":     "volume",
            "ascending": "false",
        }
        if negrisk_only:
            base_params["neg_risk"] = "true"

        while True:
            try:
                params = {**base_params, "offset": offset}
                data   = self._get(f"{GAMMA_API}/markets", params=params)
                if not data:
                    break

                page_ok = 0
                for raw in data:
                    m = self._parse(raw, end_max)
                    if m:
                        markets.append(m)
                        page_ok += 1

                log.debug("gamma_page", offset=offset, raw=len(data), parsed=page_ok)

                if len(data) < PAGE_SIZE:
                    break
                offset += PAGE_SIZE
                if offset >= 5000:
                    break
                time.sleep(0.2)

            except Exception as e:
                log.warning("gamma_page_failed", offset=offset, error=str(e))
                break

        log.info("gamma_fetch_done", max_days=max_days, negrisk=negrisk_only, count=len(markets))
        return markets

    def _parse(self, raw: dict, end_max_ts: int) -> Optional[MarketState]:
        """Parse a Gamma API market dict. Returns None to skip."""
        try:
            # ── End date ────────────────────────────────────────────────────
            end_date_str = raw.get("endDate") or raw.get("endDateIso") or ""
            if not end_date_str:
                return None
            end_ts = _parse_iso_to_ts(end_date_str)
            if end_ts is None:
                return None
            now          = int(time.time())
            seconds_left = end_ts - now
            if seconds_left <= 0:
                return None  # already resolved
            if end_ts > end_max_ts:
                return None  # beyond window

            # ── Token IDs (clobTokenIds is a JSON string) ───────────────────
            ctids_raw = raw.get("clobTokenIds", "") or ""
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

            # ── Prices (outcomePrices is a JSON string) ─────────────────────
            op_raw = raw.get("outcomePrices", "") or ""
            try:
                prices = json.loads(op_raw) if isinstance(op_raw, str) else (op_raw or [])
            except (json.JSONDecodeError, TypeError):
                prices = []
            if len(prices) < 2:
                yes_price, no_price = 0.5, 0.5
            else:
                raw_yes = float(prices[0])
                raw_no  = float(prices[1])
                if raw_yes >= 0.999 or raw_no >= 0.999:
                    return None  # already resolved
                yes_price = max(0.001, min(0.999, raw_yes))
                no_price  = max(0.001, min(0.999, raw_no))

            # ── Bid/Ask from Gamma directly (no CLOB call needed) ───────────
            # Gamma provides bestBid, bestAsk, lastTradePrice, spread
            best_bid   = float(raw.get("bestBid")   or yes_price - 0.01)
            best_ask   = float(raw.get("bestAsk")   or yes_price + 0.01)
            last_price = float(raw.get("lastTradePrice") or yes_price)
            spread_val = float(raw.get("spread")    or abs(best_ask - best_bid))

            # Clamp bid/ask to valid range
            yes_bid = max(0.001, min(0.998, best_bid))
            yes_ask = max(0.002, min(0.999, best_ask))
            no_bid  = max(0.001, min(0.998, 1.0 - best_ask))
            no_ask  = max(0.002, min(0.999, 1.0 - best_bid))

            # ── Volume ──────────────────────────────────────────────────────
            volume_24h = float(raw.get("volume24hr") or raw.get("volume24hrClob") or 0.0)

            # ── Fee ─────────────────────────────────────────────────────────
            # Gamma provides makerBaseFee and takerBaseFee in basis points
            taker_bps = raw.get("takerBaseFee")
            if taker_bps is not None:
                try:
                    fee_rate_bps = int(float(taker_bps))
                except (ValueError, TypeError):
                    fee_rate_bps = _calc_fee_bps(yes_price)
            else:
                fee_rate_bps = _calc_fee_bps(yes_price)

            # ── NegRisk ─────────────────────────────────────────────────────
            negrisk_group_id = None
            if raw.get("negRisk"):
                ng = raw.get("negRiskRequestID") or raw.get("conditionId") or ""
                negrisk_group_id = str(ng) if ng else None

            # ── Category ────────────────────────────────────────────────────
            tags     = raw.get("tags", []) or []
            question = str(raw.get("question") or "").strip()
            if not question:
                return None
            category = _categorise(tags, question)

            return MarketState(
                market_id             = str(raw.get("id") or raw.get("conditionId") or ""),
                question              = question,
                yes_token_id          = yes_token_id,
                no_token_id           = no_token_id,
                yes_price             = yes_price,
                no_price              = no_price,
                yes_bid               = yes_bid,
                yes_ask               = yes_ask,
                no_bid                = no_bid,
                no_ask                = no_ask,
                spread                = spread_val,
                volume_24h            = volume_24h,
                end_date_iso          = end_date_str,
                seconds_to_resolution = seconds_left,
                negrisk_group_id      = negrisk_group_id,
                category              = category,
                fee_rate_bps          = fee_rate_bps,
                fetched_at            = time.time(),
            )

        except Exception as e:
            log.debug("parse_skipped", market_id=str(raw.get("id","")), error=str(e))
            return None

    # ── CLOB enrichment (single market, only for live order pre-validation) ──

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    def _enrich_single_clob(self, m: MarketState) -> MarketState:
        """Re-fetch live bid/ask from CLOB for a single market before execution."""
        try:
            yes_book = self._session.get(
                f"{CLOB_API}/book", params={"token_id": m.yes_token_id}, timeout=5
            ).json()
            no_book  = self._session.get(
                f"{CLOB_API}/book", params={"token_id": m.no_token_id},  timeout=5
            ).json()

            def best_bid(bk): return max((float(b["price"]) for b in bk.get("bids",[])), default=0.0)
            def best_ask(bk): return min((float(a["price"]) for a in bk.get("asks",[])), default=1.0)

            yb = max(0.001, min(0.998, best_bid(yes_book)))
            ya = max(0.002, min(0.999, best_ask(yes_book)))
            nb = max(0.001, min(0.998, best_bid(no_book)))
            na = max(0.002, min(0.999, best_ask(no_book)))

            return dc_replace(m, yes_bid=yb, yes_ask=ya, no_bid=nb, no_ask=na,
                              fetched_at=time.time())
        except Exception as e:
            log.debug("clob_enrich_skip", market_id=m.market_id, error=str(e))
            return m

    # ── NegRisk grouping ──────────────────────────────────────────────────────

    def _build_groups(self, markets: list[MarketState]) -> dict[str, list[MarketState]]:
        groups: dict[str, list[MarketState]] = {}
        for m in markets:
            if m.negrisk_group_id:
                groups.setdefault(m.negrisk_group_id, []).append(m)
        return {gid: ms for gid, ms in groups.items() if len(ms) >= 2}


# ── Standalone helpers ─────────────────────────────────────────────────────────

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