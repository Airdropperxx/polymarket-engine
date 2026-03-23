"""
engines/data_engine.py — Fetches and parses Polymarket Gamma API markets.

Key fixes vs previous version:
  1. Filters out already-resolved markets (price = 1.0 or 0.0 exactly)
  2. Category detection reads from Gamma's own 'category' field first,
     then falls back to keyword matching on question text
  3. NegRisk group ID correctly parsed from nested events structure
  4. Logs category distribution after every fetch for debugging
"""

from __future__ import annotations

import gzip
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import structlog

log = structlog.get_logger(component="data_engine")

GAMMA_URL  = "https://gamma-api.polymarket.com/markets"
SNAPSHOT   = Path("data/market_snapshot.json.gz")
PAGE_SIZE  = 500
MIN_VOLUME = 10.0

# Keyword sets for fallback category detection
_CRYPTO   = {"btc","eth","bitcoin","ethereum","crypto","sol","solana","xrp",
              "doge","bnb","coinbase","binance","defi","nft","blockchain",
              "token","altcoin","stablecoin"}
_POLITICS = {"election","president","senate","congress","vote","political",
             "governor","minister","parliament","trump","biden","harris",
             "republican","democrat","tariff","fed","federal","white house",
             "supreme court","legislation","policy"}
_SPORTS   = {"nba","nfl","mlb","nhl","soccer","football","basketball","tennis",
             "ufc","match","tournament","championship","league","playoff",
             "world cup","super bowl","finals","mls","ncaa","pga","golf",
             "formula","f1","wimbledon","premier","serie a","bundesliga",
             "champions league","boxing","wrestling","cricket","rugby"}
_FINANCE  = {"stock","nasdaq","s&p","dow","ipo","earnings","gdp","inflation",
             "interest rate","fed rate","cpi","unemployment","recession",
             "market cap","aapl","tsla","nvda","msft","amzn","googl","meta"}
_TECH     = {"ai","artificial intelligence","openai","anthropic","google",
             "microsoft","apple","samsung","iphone","android","chatgpt",
             "spacex","tesla","neuralink"}
_GEO      = {"war","ceasefire","invasion","military","ukraine","russia","china",
             "taiwan","israel","gaza","nato","sanctions","conflict","nuclear"}

_WEATHER  = {"temperature","weather","celsius","fahrenheit","degrees","rainfall",
              "humidity","forecast","°c","°f","highest temp","lowest temp","climate",
              "hottest","coldest","precipitation","snow","wind","storm"}
_ENTERTAIN= {"netflix","survivor","reality tv","peaky blinders","oscar","grammy",
             "celebrity","movie","film","tv show","episode","season","streaming",
             "disney","hbo","amazon prime","box office","chart","number one",
             "spotify","album","singer","actor","actress","series","top.*netflix"}
_SOCIAL   = {"tweets","tweet","posts from","white house post","elon musk post",
             "instagram","tiktok","youtube","views","likes","social media",
             "followers","retweet","post count","number of posts"}
_SHIPPING = {"ships transit","strait of hormuz","suez","maritime","cargo","vessel",
             "shipping","port","trade route","tanker","transit count"}

def _categorise(tags: list, question: str) -> str:
    """
    Determine market category from tags list and question text.
    Priority: tags keyword matching -> question keyword matching -> 'other'
    Covers: crypto, sports, politics, finance, tech, geopolitics,
            weather, entertainment, social_media, shipping, science, other
    """
    gc = " ".join(str(t) for t in tags).lower().strip() if tags else ""
    if gc:
        known = {
            "us politics": "politics", "world politics": "politics",
            "crypto": "crypto", "sports": "sports", "science": "science",
            "entertainment": "entertainment", "business": "finance",
            "economics": "finance", "technology": "tech",
            "weather": "weather", "pop culture": "entertainment",
            "tv": "entertainment", "music": "entertainment",
        }
        for k, v in known.items():
            if k in gc: return v
        if any(k in gc for k in ["crypto","bitcoin","ethereum","blockchain"]): return "crypto"
        if any(k in gc for k in ["sport","nba","nfl","soccer","football","tennis","mls","nhl","mlb"]): return "sports"
        if any(k in gc for k in ["politic","election","government"]): return "politics"
        if any(k in gc for k in ["financ","stock","market","econom"]): return "finance"
        if any(k in gc for k in ["tech","ai","software"]): return "tech"
        if any(k in gc for k in ["world","geo","war","conflict"]): return "geopolitics"
        if any(k in gc for k in ["weather","climate","temperature"]): return "weather"
        if any(k in gc for k in ["entertainment","tv","film","netflix","music"]): return "entertainment"
        if len(gc) < 30: return gc.title()

    q = question.lower()
    words = set(q.split())
    # Priority order: specific first, generic last
    if words & _WEATHER   or any(k in q for k in _WEATHER):   return "weather"
    if words & _CRYPTO    or any(k in q for k in _CRYPTO):    return "crypto"
    if words & _SPORTS    or any(k in q for k in _SPORTS):    return "sports"
    if words & _ENTERTAIN or any(k in q for k in _ENTERTAIN): return "entertainment"
    if words & _SOCIAL    or any(k in q for k in _SOCIAL):    return "social_media"
    if words & _SHIPPING  or any(k in q for k in _SHIPPING):  return "shipping"
    if words & _FINANCE   or any(k in q for k in _FINANCE):   return "finance"
    if words & _TECH      or any(k in q for k in _TECH if len(k) > 4): return "tech"
    if words & _POLITICS  or any(k in q for k in _POLITICS):  return "politics"
    if words & _GEO       or any(k in q for k in _GEO):       return "geopolitics"
    return "other"


def _parse_iso(s: str) -> Optional[int]:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        pass
    try:
        return int(datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return None


def _seconds_until(iso_str: str) -> int:
    """Return seconds from now until the ISO datetime. Returns -1 for empty/invalid."""
    if not iso_str:
        return -1
    ts = _parse_iso(iso_str)
    if ts is None:
        return -1
    return int(ts - time.time())


@dataclass
class MarketState:
    market_id:             str
    question:              str
    yes_token_id:          str
    no_token_id:           str
    yes_price:             float
    no_price:              float
    yes_bid:               float
    yes_ask:               float
    no_bid:                float
    no_ask:                float
    volume_24h:            float
    end_date_iso:          str
    seconds_to_resolution: int
    negrisk_group_id:      Optional[str]
    category:              str
    fee_rate_bps:          int
    fetched_at:            float = field(default_factory=time.time)

    def is_stale(self, max_age_seconds: int = 300) -> bool:
        return (time.time() - self.fetched_at) > max_age_seconds

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MarketState":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


def save_snapshot(markets: list, path: Path = SNAPSHOT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "count": len(markets),
        "markets": [m.to_dict() for m in markets],
    }
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    log.info("snapshot_saved", count=len(markets), bytes=path.stat().st_size)


def load_snapshot(path: Path = SNAPSHOT) -> list:
    if not path.exists():
        return []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
        return [MarketState.from_dict(m) for m in data.get("markets", [])]
    except Exception as e:
        log.warning("snapshot_load_failed", error=str(e))
        return []


class DataEngine:

    def __init__(self, config: dict):
        self.config  = config
        self._cache: list = []
        self._groups: dict = {}
        prev = load_snapshot()
        if prev:
            self._cache  = [m for m in prev if m.seconds_to_resolution > -3600]
            self._groups = self._build_groups(self._cache)
            log.info("warm_cache", count=len(self._cache))

    def fetch_all_markets(self) -> list:
        try:
            raw_markets = self._paginate()
            if not raw_markets:
                log.warning("gamma_returned_empty_using_cache")
                return self._cache

            # Log sample for debugging
            if raw_markets:
                s = raw_markets[0]
                log.info("api_sample",
                         outcomePrices=str(s.get("outcomePrices",""))[:50],
                         clobTokenIds=str(s.get("clobTokenIds",""))[:50],
                         category=str(s.get("category",""))[:30],
                         endDateIso=str(s.get("endDateIso",""))[:30])

            markets     = [m for m in (self._parse(r) for r in raw_markets) if m]
            filtered    = [m for m in markets if m.volume_24h >= MIN_VOLUME]
            if not filtered:
                filtered = markets

            self._cache  = filtered
            self._groups = self._build_groups(filtered)
            save_snapshot(filtered)

            # Log category distribution
            from collections import Counter
            cats = Counter(m.category for m in filtered)
            log.info("fetch_complete",
                     total=len(filtered),
                     negrisk_groups=len(self._groups),
                     categories=dict(cats.most_common(8)))
            return filtered

        except Exception as e:
            log.error("fetch_failed_using_cache", error=str(e))
            return self._cache

    def fetch_negrisk_groups(self) -> dict:
        return self._groups

    def get_cached_markets(self) -> list:
        return self._cache

    def get_single_market(self, token_id: str) -> Optional[MarketState]:
        return next(
            (m for m in self._cache
             if m.yes_token_id == token_id or m.no_token_id == token_id),
            None
        )

    def _paginate(self) -> list:
        results = []
        offset  = 0
        session = requests.Session()
        session.headers.update({"User-Agent": "polymarket-engine/3.1"})

        while True:
            try:
                resp = session.get(
                    GAMMA_URL,
                    params={
                        "active": "true", "closed": "false",
                        "limit": PAGE_SIZE, "offset": offset,
                        "order": "volume24hr", "ascending": "false",
                    },
                    timeout=25,
                )
                resp.raise_for_status()
                page = resp.json()
                if not page:
                    break
                results.extend(page)
                log.info("gamma_page", offset=offset, count=len(page))
                if len(page) < PAGE_SIZE:
                    break
                offset += PAGE_SIZE
                if offset >= 3000:
                    break
                time.sleep(0.25)
            except Exception as e:
                log.warning("gamma_page_error", offset=offset, error=str(e))
                break

        return results

    def _parse(self, raw: dict) -> Optional[MarketState]:
        try:
            question = str(raw.get("question") or raw.get("title") or "").strip()
            if not question:
                return None

            # Parse outcomePrices — always a JSON string like '["0.94","0.06"]'
            op_raw = raw.get("outcomePrices")
            if not op_raw:
                return None
            prices = json.loads(op_raw) if isinstance(op_raw, str) else op_raw
            if len(prices) < 2:
                return None
            yes_price = max(0.001, min(0.999, float(prices[0])))
            no_price  = max(0.001, min(0.999, float(prices[1])))

            # KEY FIX: Skip markets that are already resolved (price = exactly 1 or 0)
            # These are NOT opportunities — they've already paid out
            raw_yes = float(prices[0])
            raw_no  = float(prices[1])
            if raw_yes >= 0.999 or raw_no >= 0.999:
                return None   # already resolved
            if raw_yes <= 0.001 and raw_no <= 0.001:
                return None   # invalid/broken market

            # Parse clobTokenIds — JSON string like '["tokenA","tokenB"]'
            ctids = raw.get("clobTokenIds")
            token_ids  = json.loads(ctids) if isinstance(ctids, str) else (ctids or [])
            cid        = str(raw.get("conditionId") or raw.get("id") or "")
            yes_token_id = str(token_ids[0]) if len(token_ids) > 0 else cid + "_yes"
            no_token_id  = str(token_ids[1]) if len(token_ids) > 1 else cid + "_no"

            # Time to resolution — prefer endDateIso
            end_str     = (raw.get("endDateIso") or raw.get("endDate") or "")
            end_ts      = _parse_iso(end_str)
            now_ts      = int(time.time())
            seconds_left = max(0, (end_ts - now_ts)) if end_ts else 0

            # Skip markets with no end date that resolve in the past
            if seconds_left == 0 and not end_str:
                return None

            # Volume
            vol = float(raw.get("volume24hr") or raw.get("volume24hrClob") or 0.0)

            # Category — use Gamma's field first
            gamma_cat = str(raw.get("category") or "")
            # Also check nested events for category
            if not gamma_cat:
                events = raw.get("events") or []
                if isinstance(events, list) and events:
                    first = events[0] if isinstance(events[0], dict) else {}
                    gamma_cat = str(first.get("category") or "")
            category = _categorise([gamma_cat], question)

            # NegRisk group
            neg_risk_id = raw.get("negRiskGroupId")
            if not neg_risk_id:
                events = raw.get("events") or []
                if isinstance(events, list) and events:
                    first = events[0] if isinstance(events[0], dict) else {}
                    if first.get("negRisk"):
                        neg_risk_id = first.get("negRiskMarketID") or first.get("id")

            # Fees
            fee_bps = int(raw.get("takerBaseFee") or 0)
            if fee_bps == 0:
                fee_bps = int(2.25 * (yes_price * (1 - yes_price)) ** 2 * 10000)

            # Synthetic spread (1%)
            spread = 0.01
            return MarketState(
                market_id             = str(raw.get("id") or cid),
                question              = question,
                yes_token_id          = yes_token_id,
                no_token_id           = no_token_id,
                yes_price             = yes_price,
                no_price              = no_price,
                yes_bid               = max(0.001, yes_price - spread),
                yes_ask               = min(0.999, yes_price + spread),
                no_bid                = max(0.001, no_price  - spread),
                no_ask                = min(0.999, no_price  + spread),
                volume_24h            = vol,
                end_date_iso          = end_str,
                seconds_to_resolution = seconds_left,
                negrisk_group_id      = str(neg_risk_id) if neg_risk_id else None,
                category              = category,
                fee_rate_bps          = fee_bps,
                fetched_at            = float(now_ts),
            )

        except Exception as e:
            log.debug("parse_skip", error=str(e),
                      q=str(raw.get("question", ""))[:50])
            return None

    def _build_groups(self, markets: list) -> dict:
        groups: dict = {}
        for m in markets:
            if m.negrisk_group_id:
                groups.setdefault(m.negrisk_group_id, []).append(m)
        return {gid: ms for gid, ms in groups.items() if len(ms) >= 2}

    @staticmethod
    def _parse_iso_to_ts(s: str) -> Optional[int]:
        return _parse_iso(s)
