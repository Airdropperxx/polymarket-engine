#!/usr/bin/env python3
"""
scripts/clob_enrich.py
======================
Runs AFTER the scan cycle. Reads the top unexecuted opportunities from
scan_log.json, fetches live CLOB bid/ask orderbook for each, and writes
data/enriched_opportunities.json.

This decouples expensive CLOB calls from the main scan cycle:
  - Scan cycle: fast Gamma-only fetch (~250ms for 2000 markets)
  - CLOB enrichment: separate job, enriches only the top 10 candidates
  - Execution engine: reads enriched data when deciding to trade

Architecture:
  scan_log.json          <- scan cycle writes opportunities
  clob_enrich.py         <- this script enriches top ones with live orderbook
  enriched_opps.json     <- execution engine reads this for final trade decision
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import structlog

log = structlog.get_logger(component="clob_enrich")

CLOB_API   = "https://clob.polymarket.com"
GAMMA_API  = "https://gamma-api.polymarket.com"
SCAN_LOG   = Path("data/scan_log.json")
OUTPUT     = Path("data/enriched_opportunities.json")
TOP_N      = 15   # enrich top N candidates per run
MAX_AGE_S  = 600  # only enrich opportunities seen in last 10 minutes


def fetch_clob_book(token_id: str, session: requests.Session, timeout: int = 5) -> dict:
    """Fetch live orderbook from CLOB for a single token."""
    try:
        r = session.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.debug("clob_book_failed", token_id=token_id[:20], error=str(e))
    return {}


def best_bid(book: dict) -> float:
    bids = book.get("bids", [])
    if not bids:
        return 0.0
    return max(float(b["price"]) for b in bids)


def best_ask(book: dict) -> float:
    asks = book.get("asks", [])
    if not asks:
        return 1.0
    return min(float(a["price"]) for a in asks)


def fetch_gamma_market(market_id: str, session: requests.Session) -> dict:
    """Fetch single market from Gamma for token IDs and metadata."""
    try:
        r = session.get(f"{GAMMA_API}/markets/{market_id}", timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.debug("gamma_market_failed", market_id=market_id[:20], error=str(e))
    return {}


def main():
    start = time.time()

    # ── Read scan log ─────────────────────────────────────────────────────────
    if not SCAN_LOG.exists():
        log.info("no_scan_log_yet")
        sys.exit(0)

    try:
        entries = json.loads(SCAN_LOG.read_text())
    except Exception as e:
        log.error("scan_log_read_failed", error=str(e))
        sys.exit(0)

    # ── Filter to recent, non-executed, real strategy opportunities ──────────
    now_iso = datetime.now(timezone.utc).isoformat()
    cutoff  = time.time() - MAX_AGE_S

    candidates = []
    for e in reversed(entries):  # newest first
        try:
            ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if ts < cutoff:
            break  # older entries, stop
        # Skip observer signals — only real strategy opportunities
        strat = e.get("strategy", "")
        if strat.startswith("observer_"):
            continue
        if not e.get("market_id"):
            continue
        candidates.append(e)

    if not candidates:
        log.info("no_recent_candidates", cutoff_age_s=MAX_AGE_S)
        # Write empty enriched file so dashboard knows enrichment ran
        OUTPUT.parent.mkdir(exist_ok=True)
        OUTPUT.write_text(json.dumps({
            "enriched_at": now_iso,
            "opportunities": [],
            "message": "No recent strategy opportunities to enrich",
        }, indent=2))
        sys.exit(0)

    # Sort by score descending, deduplicate by market_id
    seen_markets = set()
    top = []
    for c in sorted(candidates, key=lambda x: x.get("score", 0), reverse=True):
        mid = c.get("market_id", "")
        if mid not in seen_markets:
            seen_markets.add(mid)
            top.append(c)
        if len(top) >= TOP_N:
            break

    log.info("enriching_candidates", count=len(top))

    # ── Fetch CLOB data for each ──────────────────────────────────────────────
    session = requests.Session()
    session.headers.update({"User-Agent": "polymarket-engine/4.0", "Accept": "application/json"})

    enriched = []
    for opp in top:
        market_id = opp.get("market_id", "")
        entry_price = opp.get("buy_price", opp.get("win_probability", 0.5))

        # Get token IDs from Gamma
        gamma_data = fetch_gamma_market(market_id, session)
        if not gamma_data:
            enriched.append({**opp, "clob_enriched": False, "error": "gamma_fetch_failed"})
            continue

        try:
            token_ids = json.loads(gamma_data.get("clobTokenIds", "[]") or "[]")
        except Exception:
            token_ids = []

        if len(token_ids) < 2:
            enriched.append({**opp, "clob_enriched": False, "error": "no_token_ids"})
            continue

        yes_token = token_ids[0]
        no_token  = token_ids[1]

        # Fetch live orderbooks
        yes_book = fetch_clob_book(yes_token, session)
        no_book  = fetch_clob_book(no_token,  session)

        clob_yes_bid = best_bid(yes_book)
        clob_yes_ask = best_ask(yes_book)
        clob_no_bid  = best_bid(no_book)
        clob_no_ask  = best_ask(no_book)

        # Compute live spread and effective entry
        live_spread  = round(clob_yes_ask - clob_yes_bid, 4) if clob_yes_bid > 0 else None
        effective_ask = clob_yes_ask if clob_yes_ask < 1.0 else entry_price

        # Re-check edge against live ask (not stale mid-price)
        win_prob = opp.get("win_probability", entry_price)
        live_edge = round(win_prob - effective_ask, 4)

        # Order book depth
        yes_bid_depth = sum(float(b.get("size", 0)) for b in yes_book.get("bids", [])[:5])
        yes_ask_depth = sum(float(a.get("size", 0)) for a in yes_book.get("asks", [])[:5])

        enriched.append({
            **opp,
            "clob_enriched":   True,
            "yes_token_id":    yes_token,
            "no_token_id":     no_token,
            "clob_yes_bid":    round(clob_yes_bid, 4),
            "clob_yes_ask":    round(clob_yes_ask, 4),
            "clob_no_bid":     round(clob_no_bid,  4),
            "clob_no_ask":     round(clob_no_ask,  4),
            "clob_spread":     live_spread,
            "live_edge":       live_edge,
            "bid_depth_5":     round(yes_bid_depth, 2),
            "ask_depth_5":     round(yes_ask_depth, 2),
            "enriched_at":     datetime.now(timezone.utc).isoformat(),
            # Trade quality signals
            "tradeable":       (live_edge is not None and live_edge > 0.01
                                and live_spread is not None and live_spread < 0.05
                                and yes_ask_depth > 5.0),
        })
        log.info("enriched", strategy=opp.get("strategy"), market=opp.get("question","")[:50],
                 live_edge=live_edge, spread=live_spread, ask_depth=round(yes_ask_depth,1))
        time.sleep(0.1)  # polite rate limiting

    # ── Save output ───────────────────────────────────────────────────────────
    output = {
        "enriched_at": now_iso,
        "elapsed_sec": round(time.time() - start, 2),
        "count": len(enriched),
        "tradeable_count": sum(1 for e in enriched if e.get("tradeable")),
        "opportunities": enriched,
    }
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(output, indent=2))

    log.info("enrichment_complete",
             count=len(enriched),
             tradeable=output["tradeable_count"],
             elapsed=output["elapsed_sec"])

    # Print summary for GH Actions log
    for e in enriched:
        status = "✅ TRADEABLE" if e.get("tradeable") else "⚠  not tradeable"
        print(f"{status} | {e.get('strategy')} | {e.get('question','')[:50]}")
        print(f"         live_edge={e.get('live_edge')} spread={e.get('clob_spread')} ask_depth={e.get('ask_depth_5')}")


if __name__ == "__main__":
    main()
