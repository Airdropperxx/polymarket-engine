#!/usr/bin/env python3
"""
scripts/backtest.py
===================
Historical backtester for Polymarket strategies.

Usage:
    python scripts/backtest.py --strategy s10_near_resolution --days 90
    python scripts/backtest.py --strategy s1_negrisk_arb --days 90

Data source: gamma-api.polymarket.com (free, public)
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import structlog

log = structlog.get_logger()

GAMMA_BASE = "https://gamma-api.polymarket.com"


def fetch_resolved_markets(days: int = 90) -> list[dict]:
    """Fetch all resolved markets from the last N days."""
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=days)

    url = f"{GAMMA_BASE}/markets"
    params = {
        "closed": "true",
        "limit": 1000,
        "endDate": end_date.isoformat(),
        "startDate": start_date.isoformat(),
    }

    all_markets = []
    offset = 0

    while True:
        params["offset"] = offset
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        markets = resp.json()

        if not markets:
            break

        all_markets.extend(markets)
        offset += len(markets)
        log.info("backtest.fetched_markets", count=len(all_markets), offset=offset)

        if len(markets) < 1000:
            break

    return all_markets


def backtest_s10(markets: list[dict], config: dict) -> dict:
    """Backtest S10 Near Resolution strategy."""
    min_probability = config.get("min_probability", 0.93)
    max_minutes_remaining = config.get("max_minutes_remaining", 60)
    min_edge = config.get("min_edge_after_fees", 0.025)

    trades = []
    wins = 0
    losses = 0
    total_pnl = 0.0
    capital = 100.0

    for market in markets:
        if market.get("outcomeType") != "BINARY":
            continue

        end_date = market.get("endDate")
        if not end_date:
            continue

        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue

        seconds_remaining = (end_dt - datetime.now(timezone.utc)).total_seconds()
        minutes_remaining = seconds_remaining / 60

        if minutes_remaining > max_minutes_remaining or minutes_remaining < 0:
            continue

        tokens = market.get("tokens", [])
        yes_token = next((t for t in tokens if t.get("outcome") == "YES"), None)
        no_token = next((t for t in tokens if t.get("outcome") == "NO"), None)

        if not yes_token or not no_token:
            continue

        try:
            yes_price = float(yes_token.get("price", 0))
            no_price = float(no_token.get("price", 0))
        except (ValueError, TypeError):
            continue

        if yes_price < min_probability:
            continue

        fee = 2.25 * 0.25 * (yes_price * (1 - yes_price)) ** 2
        net_payout = (1 / yes_price) - 1 - fee

        if net_payout < min_edge:
            continue

        outcome = market.get("outcome")
        won = (outcome == "YES")

        trade_pnl = (net_payout * 10) if won else -10
        capital += trade_pnl

        trades.append({
            "market_id": market.get("conditionId"),
            "question": market.get("question", "")[:60],
            "yes_price": yes_price,
            "outcome": outcome,
            "won": won,
            "pnl": trade_pnl,
            "capital_after": capital,
        })

        if won:
            wins += 1
        else:
            losses += 1
        total_pnl += trade_pnl

    total_trades = wins + losses
    win_rate = wins / total_trades if total_trades > 0 else 0
    total_roi = (total_pnl / (total_trades * 10)) * 100 if total_trades > 0 else 0

    return {
        "strategy": "s10_near_resolution",
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "total_roi": total_roi,
        "final_capital": capital,
        "trades": trades,
    }


def backtest_s1(markets: list[dict], config: dict) -> dict:
    """Backtest S1 NegRisk Arb strategy."""
    min_spread = config.get("min_spread_after_fees", 0.02)

    neg_risk_groups = {}
    for market in markets:
        group_id = market.get("negRiskGroupId")
        if not group_id:
            continue
        if group_id not in neg_risk_groups:
            neg_risk_groups[group_id] = []
        neg_risk_groups[group_id].append(market)

    trades = []
    wins = 0
    losses = 0
    total_pnl = 0.0

    for group_id, group_markets in neg_risk_groups.items():
        if len(group_markets) < 2:
            continue

        total_yes_price = 0.0
        valid = True

        for market in group_markets:
            tokens = market.get("tokens", [])
            yes_token = next((t for t in tokens if t.get("outcome") == "YES"), None)

            if not yes_token:
                valid = False
                break

            try:
                total_yes_price += float(yes_token.get("price", 0))
            except (ValueError, TypeError):
                valid = False
                break

        if not valid:
            continue

        spread = 1.0 - total_yes_price

        if spread < min_spread:
            continue

        outcome = group_markets[0].get("outcome")
        won = True

        trade_pnl = spread * 10
        total_pnl += trade_pnl

        trades.append({
            "group_id": group_id,
            "markets": len(group_markets),
            "total_yes_price": total_yes_price,
            "spread": spread,
            "won": won,
            "pnl": trade_pnl,
        })

        wins += 1

    total_trades = wins + losses
    win_rate = wins / total_trades if total_trades > 0 else 0
    total_roi = (total_pnl / (total_trades * 10)) * 100 if total_trades > 0 else 0

    return {
        "strategy": "s1_negrisk_arb",
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "total_roi": total_roi,
        "trades": trades,
    }


def calculate_max_drawdown(trades: list[dict]) -> float:
    """Calculate max drawdown from trade history."""
    capital = 100.0
    peak = 100.0
    max_dd = 0.0

    for trade in trades:
        capital += trade.get("pnl", 0)
        if capital > peak:
            peak = capital
        dd = (peak - capital) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    return max_dd * 100


def main():
    parser = argparse.ArgumentParser(description="Backtest Polymarket strategies")
    parser.add_argument(
        "--strategy",
        choices=["s10_near_resolution", "s1_negrisk_arb"],
        required=True,
        help="Strategy to backtest",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Number of days to backtest (default: 90)",
    )
    parser.add_argument(
        "--output",
        default="data/backtest_results.json",
        help="Output file path",
    )

    args = parser.parse_args()

    log.info("backtest.starting", strategy=args.strategy, days=args.days)

    print(f"Fetching markets from last {args.days} days...")
    markets = fetch_resolved_markets(args.days)
    print(f"Found {len(markets)} resolved markets")

    if args.strategy == "s10_near_resolution":
        config = {"min_probability": 0.93, "max_minutes_remaining": 60, "min_edge_after_fees": 0.025}
        result = backtest_s10(markets, config)
    else:
        config = {"min_spread_after_fees": 0.02}
        result = backtest_s1(markets, config)

    result["max_drawdown"] = calculate_max_drawdown(result.get("trades", []))
    result["days_backtested"] = args.days
    result["markets_analyzed"] = len(markets)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\n{'='*50}")
    print(f"BACKTEST RESULTS: {args.strategy}")
    print(f"{'='*50}")
    print(f"Total Trades:    {result['total_trades']}")
    print(f"Wins:            {result['wins']}")
    print(f"Losses:          {result['losses']}")
    print(f"Win Rate:        {result['win_rate']*100:.1f}%")
    print(f"Total P&L:       ${result['total_pnl']:.2f}")
    print(f"Total ROI:       {result['total_roi']:.2f}%")
    print(f"Max Drawdown:    {result['max_drawdown']:.2f}%")
    print(f"{'='*50}")
    print(f"\nResults saved to: {args.output}")


if __name__ == "__main__":
    main()