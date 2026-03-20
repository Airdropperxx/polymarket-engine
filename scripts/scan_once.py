#!/usr/bin/env python3
"""
scripts/scan_once.py
====================
Manual scanner. Fetches live markets, runs all strategies, prints opportunities.
NEVER submits any order.

Usage:
    python scripts/scan_once.py                          # live scan, all strategies
    python scripts/scan_once.py --strategy s10           # single strategy
    python scripts/scan_once.py --min-edge 0.05          # filter by edge
    python scripts/scan_once.py --dry-run                # use fixture data, no API calls

Output format:
    === SCAN RESULTS — 2026-03-20 14:22:00 UTC ===
    Strategies: 3 | Markets scanned: 1247 | Opportunities: 5

    #1 [SCORE: 0.87] s10_near_resolution
       Market: "Will BTC close above $85K at 14:30 UTC?"
       Action: buy_yes @ $0.94 | Edge: 3.2% net | Time: 8 min remaining
       Size (if live, $100 bankroll): $14.82 USDC
    ...
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import yaml


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Engine — Manual Scanner")
    parser.add_argument("--strategy",  default=None, help="Only run this strategy (s1/s8/s10)")
    parser.add_argument("--min-edge",  type=float, default=0.0, help="Minimum edge filter")
    parser.add_argument("--dry-run",   action="store_true", help="Use fixture data, no real API")
    args = parser.parse_args()

    config = yaml.safe_load(open(os.environ.get("ENGINE_CONFIG", "configs/engine.yaml")))

    from engines.data_engine import DataEngine
    from strategies.s10_near_resolution import NearResolutionStrategy
    from strategies.s1_negrisk_arb import NegRiskArbStrategy
    from strategies.s8_logical_arb import LogicalArbStrategy

    data = DataEngine(config)

    if args.dry_run:
        # Use fixture data — no real API calls
        import json
        fixtures_path = Path("tests/fixtures/sample_markets.json")
        if fixtures_path.exists():
            raw = json.loads(fixtures_path.read_text())
            print(f"[DRY-RUN] Using fixture data: {len(raw)} markets")
            markets = []
            for r in raw:
                from engines.data_engine import _seconds_until, _categorise
                from engines.data_engine import MarketState
                import time
                tokens = r.get("tokens", [])
                yes_tok = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), {})
                no_tok  = next((t for t in tokens if t.get("outcome", "").upper() == "NO"),  {})
                markets.append(MarketState(
                    market_id=r.get("conditionId", ""),
                    question=r.get("question", ""),
                    yes_token_id=yes_tok.get("token_id", ""),
                    no_token_id=no_tok.get("token_id", ""),
                    yes_price=float(yes_tok.get("price", 0.5)),
                    no_price=float(no_tok.get("price", 0.5)),
                    yes_bid=float(yes_tok.get("bid", 0.0)),
                    no_bid=float(no_tok.get("bid", 0.0)),
                    volume_24h=float(r.get("volume24hr", 0.0)),
                    end_date_iso=r.get("endDate", ""),
                    seconds_to_resolution=_seconds_until(r.get("endDate", "")),
                    negrisk_group_id=r.get("negRiskGroupId"),
                    category=_categorise(r.get("tags", []), r.get("question", "")),
                    fee_rate_bps=r.get("feeRateBps", 200),
                    fetched_at=time.time(),
                ))
        else:
            print("[DRY-RUN] No fixture file found. Run against live API instead.")
            sys.exit(1)
        groups = {}
        for m in markets:
            if m.negrisk_group_id:
                groups.setdefault(m.negrisk_group_id, []).append(m)
        groups = {k: v for k, v in groups.items() if len(v) >= 2}
    else:
        markets = data.fetch_all_markets()
        groups  = data.fetch_negrisk_groups()

    # Build strategy list
    all_strategies = [
        (NearResolutionStrategy(), yaml.safe_load(open("configs/s10_near_resolution.yaml")), "s10"),
        (NegRiskArbStrategy(),     yaml.safe_load(open("configs/s1_negrisk.yaml")),          "s1"),
        (LogicalArbStrategy(),     yaml.safe_load(open("configs/s8_logical.yaml")),          "s8"),
    ]
    if args.strategy:
        all_strategies = [(s, c, k) for (s, c, k) in all_strategies if k == args.strategy]

    # Run scanners
    all_opps = []
    for strategy, s_cfg, key in all_strategies:
        try:
            opps = strategy.scan(markets, groups, s_cfg)
            for opp in opps:
                opp.score = strategy.score(opp, s_cfg)
            all_opps.extend((opp, strategy, s_cfg) for opp in opps)
        except Exception as exc:
            print(f"[ERROR] {strategy.name}: {exc}")

    # Filter and sort
    if args.min_edge:
        all_opps = [(o, s, c) for (o, s, c) in all_opps if o.edge >= args.min_edge]
    all_opps.sort(key=lambda x: x[0].score, reverse=True)

    # Print results
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{'='*60}")
    print(f"SCAN RESULTS — {now}")
    print(f"Strategies: {len(all_strategies)} | Markets scanned: {len(markets)} | "
          f"NegRisk groups: {len(groups)} | Opportunities: {len(all_opps)}")
    print("=" * 60)

    if not all_opps:
        print("No opportunities found above threshold.\n")
        sys.exit(0)

    for i, (opp, strategy, s_cfg) in enumerate(all_opps[:20], 1):
        mins = opp.time_to_resolution_sec // 60
        bankroll = config.get("engine", {}).get("capital_usdc", 100.0)
        allocated = bankroll * config["allocations"].get(opp.strategy, 0.0)
        size = strategy.size(opp, allocated, s_cfg)

        print(f"\n#{i} [SCORE: {opp.score:.3f}] {opp.strategy}")
        print(f"   Market: \"{opp.market_question[:70]}\"")
        print(f"   Action: {opp.action} @ ${opp.win_probability:.3f} | "
              f"Edge: {opp.edge*100:.1f}% net | Time: {mins} min remaining")
        print(f"   Size (if live, ${bankroll:.0f} bankroll): ${size:.2f} USDC")
        if opp.metadata:
            extras = {k: v for k, v in opp.metadata.items()
                     if k in ("total_cost", "group_id", "violation", "fee_rate")}
            if extras:
                print(f"   Meta: {extras}")

    print(f"\n{'='*60}")
    print("NOTE: This is a read-only scan. No orders were submitted.\n")


if __name__ == "__main__":
    main()
