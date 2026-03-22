"""
engines/data_engine.py

Fetches markets from Polymarket Gamma API using the CORRECT field names:
  outcomePrices  -> JSON string like '["0.94", "0.06"]'
  clobTokenIds   -> JSON string like '["tokenId1", "tokenId2"]'  
  volume24hr     -> float (24h volume)
  endDateIso     -> ISO date string
  takerBaseFee   -> taker fee bps
  
Saves compressed snapshot to data/market_snapshot.json.gz after each fetch.
NEVER places orders. Returns stale cache on any error.
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
MIN_VOLUME = 10.0   # very low floor — let strategies filter

_CRYPTO   = {"btc","eth","bitcoin","ethereum","crypto","sol","solana","xrp","doge",
              "bnb","usdc","defi","nft","blockchain","coinbase","binance"}
_POLITICS = {"election","president","senate","congress","vote","political",
             "governor","minister","parliament","trump","biden","harris",
             "republican","democrat","tariff","tariffs","fed","federal"}
_SPORTS   = {"nba","nfl","mlb","nhl","soccer","football","basketball","tennis",
             "ufc","match","tournament","championship","league","playoff",
             "world cup","super bowl","finals","mls","ncaa","pga","golf",
             "formula","f1","wimbledon","premier league"}


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


def _categorise(question: str, category_field: str = "") -> str:
    text = (question + " " + category_field).lower()
    words = set(text.split())
    if words & _CRYPTO:   return "crypto"
    if words & _POLITICS: return "politics"
    if words & _SPORTS:   return "sports"
    # Also check substrings for compound words
    for kw in _CRYPTO:
        if kw in text: return "crypto"
    for kw in _SPORTS:
        if kw in text: return "sports"
    for kw in _POLITICS:
        if kw in text: return "politics"
    return "other"


def _parse_iso(s: str) -> Optional[int]:
    if not s:
        return None
    # Try multiple formats
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S%z"):
        try:
            if fmt.endswith("%z"):
                dt = datetime.fromisoformat(s)
                return int(dt.timestamp())
            dt = datetime.strptime(s, fmt)
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    # Fallback: parse first 10 chars as date
    try:
        dt = datetime.strptime(s[:10], "%Y-%m-%d")
        return int(dt.replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return None


def save_snapshot(markets: list, path: Path = SNAPSHOT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "count":    len(markets),
        "markets":  [m.to_dict() for m in markets],
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
        markets = [MarketState.from_dict(m) for m in data.get("markets", [])]
        log.info("snapshot_loaded", count=len(markets))
        return markets
    except Exception as e:
        log.warning("snapshot_load_failed", error=str(e))
        return []


class DataEngine:

    def __init__(self, config: dict):
        self.config  = config
        self._cache: list = []
        self._groups: dict = {}
        # Warm up from previous snapshot
        prev = load_snapshot()
        if prev:
            now = time.time()
            # Keep markets that haven't clearly expired (give 1hr buffer)
            self._cache = [m for m in prev if m.seconds_to_resolution > -3600]
            self._groups = self._build_groups(self._cache)
            log.info("warm_cache", count=len(self._cache))

    # ------------------------------------------------------------------ #
    #  Public                                                              #
    # ------------------------------------------------------------------ #

    def fetch_all_markets(self) -> list:
        """Fetch all active markets. Falls back to cache on any error."""
        try:
            raw_markets = self._paginate()
            if not raw_markets:
                log.warning("gamma_returned_empty_using_cache")
                return self._cache

            # Log a sample of raw data for debugging
            if raw_markets:
                sample = raw_markets[0]
                log.info("raw_sample_fields", fields=list(sample.keys())[:15])
                log.info("raw_sample_outcomePrices",
                         val=str(sample.get("outcomePrices",""))[:60])
                log.info("raw_sample_clobTokenIds",
                         val=str(sample.get("clobTokenIds",""))[:60])

            markets = []
            parse_errors = 0
            for raw in raw_markets:
                m = self._parse(raw)
                if m:
                    markets.append(m)
                else:
                    parse_errors += 1

            log.info("parse_results",
                     raw=len(raw_markets),
                     parsed=len(markets),
                     errors=parse_errors)

            if not markets:
                log.warning("zero_markets_parsed_using_cache",
                            hint="Check raw_sample_fields log above")
                return self._cache

            # Apply minimum volume filter
            filtered = [m for m in markets if m.volume_24h >= MIN_VOLUME]
            log.info("volume_filter",
                     before=len(markets),
                     after=len(filtered),
                     min_volume=MIN_VOLUME)

            if not filtered:
                filtered = markets  # use all if volume filter removes everything

            self._cache  = filtered
            self._groups = self._build_groups(filtered)
            save_snapshot(filtered)

            log.info("fetch_complete",
                     total=len(filtered),
                     negrisk_groups=len(self._groups))
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

    # ------------------------------------------------------------------ #
    #  Internal                                                            #
    # ------------------------------------------------------------------ #

    def _paginate(self) -> list:
        """Page through Gamma API. Stops at 3000 markets."""
        results = []
        offset  = 0
        session = requests.Session()
        session.headers.update({"User-Agent": "polymarket-engine/3.1"})

        while True:
            try:
                resp = session.get(
                    GAMMA_URL,
                    params={
                        "active":    "true",
                        "closed":    "false",
                        "limit":     PAGE_SIZE,
                        "offset":    offset,
                        "order":     "volume24hr",
                        "ascending": "false",
                    },
                    timeout=25,
                )
                resp.raise_for_status()
                page = resp.json()

                if not page:
                    break

                results.extend(page)
                log.info("gamma_page_fetched",
                         offset=offset,
                         page_size=len(page),
                         total_so_far=len(results))

                if len(page) < PAGE_SIZE:
                    break   # last page

                offset += PAGE_SIZE
                if offset >= 3000:
                    break   # safety cap

                time.sleep(0.25)

            except Exception as e:
                log.warning("gamma_page_error", offset=offset, error=str(e))
                break

        return results

    def _parse(self, raw: dict) -> Optional[MarketState]:
        """
        Parse a single Gamma API market dict into MarketState.

        Real Gamma API fields (verified against docs):
          outcomePrices  -> string: '["0.94", "0.06"]'
          clobTokenIds   -> string: '["tokenA", "tokenB"]'  (YES=index 0, NO=index 1)
          volume24hr     -> number
          endDateIso     -> string ISO date (PREFERRED - always ISO format)
          endDate        -> string (fallback)
          takerBaseFee   -> number (fee in bps)
          category       -> string (Gamma's own category)
          negRiskGroupId -> string (for NegRisk markets)
        """
        try:
            question = str(raw.get("question") or raw.get("title") or "")
            if not question:
                return None

            # --- Parse outcomePrices (JSON string "["0.6","0.4"]") ---
            op_raw = raw.get("outcomePrices")
            if not op_raw:
                return None

            if isinstance(op_raw, str):
                try:
                    prices = json.loads(op_raw)
                except (json.JSONDecodeError, ValueError):
                    return None
            elif isinstance(op_raw, list):
                prices = op_raw
            else:
                return None

            if len(prices) < 2:
                return None

            try:
                yes_price = float(prices[0])
                no_price  = float(prices[1])
            except (TypeError, ValueError):
                return None

            # Clamp
            yes_price = max(0.001, min(0.999, yes_price))
            no_price  = max(0.001, min(0.999, no_price))

            # --- Parse clobTokenIds (JSON string "["tokenA","tokenB"]") ---
            ctids_raw = raw.get("clobTokenIds")
            yes_token_id = ""
            no_token_id  = ""

            if ctids_raw:
                if isinstance(ctids_raw, str):
                    try:
                        token_ids = json.loads(ctids_raw)
                    except (json.JSONDecodeError, ValueError):
                        token_ids = []
                elif isinstance(ctids_raw, list):
                    token_ids = ctids_raw
                else:
                    token_ids = []

                if len(token_ids) >= 2:
                    yes_token_id = str(token_ids[0])
                    no_token_id  = str(token_ids[1])
                elif len(token_ids) == 1:
                    yes_token_id = str(token_ids[0])
                    no_token_id  = str(token_ids[0]) + "_no"

            # Fallback token IDs from conditionId
            if not yes_token_id:
                cid = str(raw.get("conditionId") or raw.get("id") or "")
                yes_token_id = cid + "_yes"
                no_token_id  = cid + "_no"

            # --- Time to resolution ---
            # Prefer endDateIso (always ISO), fallback to endDate
            end_str = (raw.get("endDateIso") or
                       raw.get("endDate") or
                       raw.get("umaEndDateIso") or "")
            end_ts      = _parse_iso(end_str)
            now_ts      = int(time.time())
            seconds_left = max(0, (end_ts - now_ts)) if end_ts else 0

            # --- Volume ---
            vol = float(raw.get("volume24hr") or
                        raw.get("volume24hrClob") or
                        raw.get("volume_24h") or 0.0)

            # --- Fees ---
            # takerBaseFee is in bps (e.g. 200 = 2%)
            fee_bps = int(raw.get("takerBaseFee") or
                          raw.get("fee_rate_bps") or 0)
            if fee_bps == 0:
                # Estimate from formula if not provided
                fee_bps = int(2.25 * (yes_price * (1 - yes_price)) ** 2 * 10000)

            # --- Category ---
            gamma_cat = str(raw.get("category") or "")
            category  = _categorise(question, gamma_cat)

            # --- NegRisk group ---
            neg_risk_id = (raw.get("negRiskGroupId") or
                           raw.get("negRiskMarketID") or None)
            # Also check nested events for negRisk
            if not neg_risk_id:
                events = raw.get("events") or []
                if isinstance(events, list) and events:
                    first_event = events[0] if isinstance(events[0], dict) else {}
                    if first_event.get("negRisk"):
                        neg_risk_id = first_event.get("negRiskMarketID") or first_event.get("id")

            # --- Synthetic bid/ask (1% spread around mid) ---
            spread   = 0.01
            yes_bid  = max(0.001, yes_price - spread)
            yes_ask  = min(0.999, yes_price + spread)
            no_bid   = max(0.001, no_price  - spread)
            no_ask   = min(0.999, no_price  + spread)

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
                volume_24h            = vol,
                end_date_iso          = end_str,
                seconds_to_resolution = seconds_left,
                negrisk_group_id      = str(neg_risk_id) if neg_risk_id else None,
                category              = category,
                fee_rate_bps          = fee_bps,
                fetched_at            = float(now_ts),
            )

        except Exception as e:
            log.debug("parse_exception",
                      error=str(e),
                      q=str(raw.get("question",""))[:50])
            return None

    def _build_groups(self, markets: list) -> dict:
        groups: dict = {}
        for m in markets:
            if m.negrisk_group_id:
                groups.setdefault(m.negrisk_group_id, []).append(m)
        return {gid: ms for gid, ms in groups.items() if len(ms) >= 2}

    # Kept for test compatibility
    @staticmethod
    def _parse_iso_to_ts(s: str) -> Optional[int]:
        return _parse_iso(s)
