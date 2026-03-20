#!/usr/bin/env python3
"""
scripts/check_resolutions.py
=============================
Checks all open positions against the Polymarket CLOB API for resolution.

For each open trade in the DB:
  - Fetches current market state from CLOB
  - If market is resolved: marks trade won/loss in StateEngine
  - Triggers ReviewEngine for any resolved market

Called by: .github/workflows/resolve_check.yml (every 60 min)

NOTE: SignalEngine._check_resolutions() does a quick heuristic check
(market disappeared from active feed). This script does the authoritative
check using the CLOB API and records the actual payout.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import yaml
import structlog

log = structlog.get_logger()


def main() -> None:
    config = yaml.safe_load(open(os.environ.get("ENGINE_CONFIG", "configs/engine.yaml")))

    from py_clob_client.client import ClobClient
    from engines.state_engine import StateEngine
    from engines.review_engine import ReviewEngine
    from engines.monitor_engine import MonitorEngine

    clob = ClobClient(
        host="https://clob.polymarket.com",
        key=os.environ["POLYMARKET_PRIVATE_KEY"],
        chain_id=137,
    )
    state   = StateEngine(
        db_path=os.environ.get("DATABASE_PATH", "data/trades.db"),
        lessons_path=os.environ.get("LESSONS_PATH", "data/lessons.json"),
    )
    review  = ReviewEngine(state, config)
    monitor = MonitorEngine(config)

    open_positions = state.get_open_positions()
    if not open_positions:
        log.info("check_resolutions.no_open_positions")
        sys.exit(0)

    log.info("check_resolutions.checking", count=len(open_positions))
    resolved_count = 0

    for pos in open_positions:
        market_id = pos["market_id"]
        side      = pos["side"]   # 'YES' or 'NO'
        shares    = pos["shares"]
        cost_usdc = pos["cost_usdc"]

        try:
            # Fetch market from CLOB — token_id needed; use yes_token from position notes
            # or reconstruct by fetching via gamma API
            mkt_data = _fetch_market(clob, market_id)
            if mkt_data is None:
                continue

            resolution = mkt_data.get("resolution")  # 'YES', 'NO', or None
            if resolution is None:
                continue  # Still active

            won    = (resolution == side)
            payout = shares * 1.0 if won else 0.0  # each winning share pays $1.00
            pnl    = payout - cost_usdc

            state.mark_resolved(market_id, "win" if won else "loss", pnl)
            resolved_count += 1

            # Alert via Telegram
            monitor.send(
                "trade_executed",   # reuse template for resolution confirmation
                strategy=pos["strategy"],
                action=f"RESOLVED {'WIN' if won else 'LOSS'}",
                question=pos.get("market_question", market_id)[:40],
                size=abs(pnl),
                price=1.0 if won else 0.0,
            )

            log.info("check_resolutions.resolved",
                     market_id=market_id, won=won, pnl=pnl)

            # Trigger AI reviewer
            review_result = review.run_after_resolution(market_id)
            log.info("check_resolutions.review_done", result=review_result.get("status"))

        except Exception as exc:
            log.error("check_resolutions.error", market_id=market_id, error=str(exc))
            continue   # Never crash — check remaining positions

    log.info("check_resolutions.done", resolved=resolved_count)
    sys.exit(0)


def _fetch_market(clob, market_id: str) -> dict | None:
    """
    Fetch market resolution status from CLOB.
    Returns dict with 'resolution' key ('YES', 'NO', or None for unresolved).
    Returns None on any error.
    """
    import requests
    try:
        # Gamma API has resolution status for markets
        resp = requests.get(
            f"https://gamma-api.polymarket.com/markets/{market_id}",
            timeout=10,
        )
        if not resp.ok:
            return None
        data = resp.json()

        # Gamma API: resolved markets have 'resolved': True and 'resolution': 'YES'/'NO'
        if data.get("resolved"):
            return {"resolution": data.get("resolution")}
        return {"resolution": None}   # still active
    except Exception as exc:
        log.warning("check_resolutions.fetch_error", market_id=market_id, error=str(exc))
        return None


if __name__ == "__main__":
    main()
