"""
engines/data_engine.py

Fetches markets from Polymarket Gamma API.
Saves a compressed snapshot to data/market_snapshot.json.gz after each fetch.
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

GAMMA_URL    = "https://gamma-api.polymarket.com/markets"
SNAPSHOT     = Path("data/market_snapshot.json.gz")
PAGE_SIZE    = 500
MIN_VOLUME   = 50.0      # USDC - low floor so we actually get data

# Category keywords
_CRYPTO   = {"btc","eth","bitcoin","ethereum","crypto","sol","solana","xrp","doge"}
_POLITICS = {"election","president","senate","congress","vote","political",
             "governor","minister","parliament","trump","biden","harris"}
_SPORTS   = {"nba","nfl","mlb","nhl","soccer","football","basketball",
             "tennis","ufc","match","tournament","championship","league",
             "playoff","world cup","super bowl","finals","mls"}


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
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _categorise(tags: list, question: str) -> str:
    text = " ".join(list(tags) + [question]).lower()
    words = set(text.split())
    if words & _CRYPTO:   return "crypto"
    if words & _POLITICS: return "politics"
    if words & _SPORTS:   return "sports"
    return "other"


def _parse_iso(s: str) -> Optional[int]:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:19] if "Z" not in fmt else s, fmt)
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    # last resort: take first 10 chars as date
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
        return [MarketState.from_dict(m) for m in data["markets"]]
    except Exception as e:
        log.warning("snapshot_load_failed", error=str(e))
        return []


class DataEngine:
    def __init__(self, config: dict):
        self.config  = config
        self._cache: list = []
        self._groups: dict = {}
        # warm up from previous snapshot
        prev = load_snapshot()
        if prev:
            self._cache  = [m for m in prev if m.seconds_to_resolution > 0]
            self._groups = self._build_groups(self._cache)
            log.info("warm_cache", count=len(self._cache))

    # ------------------------------------------------------------------ #
    #  Public interface                                                    #
    # ------------------------------------------------------------------ #

    def fetch_all_markets(self) -> list:
        """Fetch active markets from Gamma API. Returns [] only on total failure."""
        try:
            raw = self._paginate()
            if not raw:
                log.warning("gamma_returned_empty")
                return self._cache

            markets = [m for m in (self._parse(r) for r in raw) if m]
            markets = [m for m in markets if m.volume_24h >= MIN_VOLUME]

            if not markets:
                log.warning("all_markets_filtered_out", raw_count=len(raw))
                return self._cache

            self._cache  = markets
            self._groups = self._build_groups(markets)
            save_snapshot(markets)
            log.info("fetch_complete", total=len(markets),
                     groups=len(self._groups))
            return markets

        except Exception as e:
            log.error("fetch_failed", error=str(e))
            return self._cache

    def fetch_negrisk_groups(self) -> dict:
        return self._groups

    def get_cached_markets(self) -> list:
        return self._cache

    def get_single_market(self, token_id: str) -> Optional[MarketState]:
        return next((m for m in self._cache
                     if m.yes_token_id == token_id
                     or m.no_token_id  == token_id), None)

    # ------------------------------------------------------------------ #
    #  Internal                                                            #
    # ------------------------------------------------------------------ #

    def _paginate(self) -> list:
        """Page through Gamma API until we have all active markets."""
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
                    timeout=20,
                )
                resp.raise_for_status()
                page = resp.json()

                if not page:
                    break

                results.extend(page)
                log.info("gamma_page", offset=offset, count=len(page))

                if len(page) < PAGE_SIZE:
                    break   # last page

                offset += PAGE_SIZE
                if offset >= 3000:
                    break   # safety cap

                time.sleep(0.3)

            except Exception as e:
                log.warning("gamma_page_error", offset=offset, error=str(e))
                break

        return results

    def _parse(self, raw: dict) -> Optional[MarketState]:
        """Parse one Gamma API market dict into a MarketState."""
        try:
            # --- token IDs and prices ---
            tokens = raw.get("tokens") or []

            yes_tok = next((t for t in tokens
                            if str(t.get("outcome","")).upper() == "YES"), None)
            no_tok  = next((t for t in tokens
                            if str(t.get("outcome","")).upper() == "NO"),  None)

            # Gamma sometimes returns outcomePrices as a JSON string "[0.6, 0.4]"
            if not yes_tok or not no_tok:
                op = raw.get("outcomePrices")
                if isinstance(op, str):
                    try:
                        prices = json.loads(op)
                        if len(prices) >= 2:
                            yes_price = float(prices[0])
                            no_price  = float(prices[1])
                        else:
                            return None
                    except Exception:
                        return None
                else:
                    return None
                yes_token_id = str(raw.get("conditionId", raw.get("id", ""))) + "_yes"
                no_token_id  = str(raw.get("conditionId", raw.get("id", ""))) + "_no"
            else:
                yes_price    = float(yes_tok.get("price", 0.5))
                no_price     = float(no_tok.get("price",  0.5))
                yes_token_id = str(yes_tok.get("token_id", ""))
                no_token_id  = str(no_tok.get("token_id",  ""))

            # clamp
            yes_price = max(0.001, min(0.999, yes_price))
            no_price  = max(0.001, min(0.999, no_price))

            # --- time to resolution ---
            end_str = raw.get("endDate") or raw.get("end_date_iso") or ""
            end_ts  = _parse_iso(end_str)
            now     = int(time.time())
            seconds_left = (end_ts - now) if end_ts else 0
            if seconds_left < 0:
                seconds_left = 0

            # --- volume ---
            vol = float(raw.get("volume24hr") or raw.get("volume_24h") or 0.0)

            # --- category ---
            tags     = raw.get("tags") or []
            question = str(raw.get("question") or "")
            if isinstance(tags, list):
                tag_names = [t.get("label","") if isinstance(t, dict) else str(t)
                             for t in tags]
            else:
                tag_names = []
            category = _categorise(tag_names, question)

            # --- negrisk group ---
            ng = raw.get("negRiskGroupId") or raw.get("neg_risk_group_id")

            # --- synthetic bid/ask (tight 1% spread around mid) ---
            spread    = 0.01
            yes_bid   = max(0.001, yes_price - spread)
            yes_ask   = min(0.999, yes_price + spread)
            no_bid    = max(0.001, no_price  - spread)
            no_ask    = min(0.999, no_price  + spread)

            # fee_rate_bps: use canonical formula estimate
            fee_bps = int(2.25 * (yes_price * (1 - yes_price)) ** 2 * 10000)

            return MarketState(
                market_id             = str(raw.get("id", "")),
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
                negrisk_group_id      = str(ng) if ng else None,
                category              = category,
                fee_rate_bps          = fee_bps,
                fetched_at            = float(now),
            )

        except Exception as e:
            log.debug("parse_skipped", error=str(e),
                      q=str(raw.get("question",""))[:40])
            return None

    def _build_groups(self, markets: list) -> dict:
        groups: dict = {}
        for m in markets:
            if m.negrisk_group_id:
                groups.setdefault(m.negrisk_group_id, []).append(m)
        return {gid: ms for gid, ms in groups.items() if len(ms) >= 2}

    # kept for test compatibility
    @staticmethod
    def _parse_iso_to_ts(s: str) -> Optional[int]:
        return _parse_iso(s)
