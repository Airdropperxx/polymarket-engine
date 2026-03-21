#!/usr/bin/env python3
"""
scripts/update_dashboard.py
===========================
Updates GitHub Gist with current engine state.

Run after each scan cycle to keep dashboard fresh.
Uses GitHub Gist for storage - accessible at gist.github.com
"""

import json
import os
import sys
from datetime import datetime

import requests
import structlog
from dotenv import load_dotenv
import yaml

log = structlog.get_logger()

load_dotenv()

GIST_ID = os.environ.get("DASHBOARD_GIST_ID", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")


def get_engine_state():
    """Get current engine state from database."""
    from engines.state_engine import StateEngine
    from engines.data_engine import DataEngine

    db_path = os.environ.get("DATABASE_PATH", "data/trades.db")
    lessons_path = os.environ.get("LESSONS_PATH", "data/lessons.json")

    if not os.path.exists(db_path):
        return None

    state = StateEngine(db_path, lessons_path)

    open_positions = state.get_open_positions()
    recent = state.get_recent_resolved_trades(24)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        "balance": state.get_current_balance(),
        "daily_pnl": state.get_daily_pnl(),
        "open_positions": len(open_positions),
        "resolved_24h": len(recent),
        "recent_trades": [
            {
                "strategy": t["strategy"],
                "market": t["market_question"][:40] if t.get("market_question") else "N/A",
                "side": t["side"],
                "price": t["price"],
                "outcome": t.get("outcome"),
                "pnl": t.get("pnl_usdc"),
            }
            for t in recent[:10]
        ],
    }


def get_config():
    """Get engine config."""
    config_path = os.environ.get("ENGINE_CONFIG", "configs/engine.yaml")
    with open(config_path) as f:
        return yaml.safe_load(f)


def generate_dashboard_html(data: dict, config: dict) -> str:
    """Generate dashboard HTML."""
    balance = data.get("balance", 0)
    daily_pnl = data.get("daily_pnl", 0)
    open_pos = data.get("open_positions", 0)
    resolved = data.get("resolved_24h", 0)
    dry_run = config.get("engine", {}).get("dry_run", True)
    timestamp = data.get("timestamp", "N/A")

    trades_html = ""
    for t in data.get("recent_trades", []):
        pnl_str = f"${t['pnl']:.2f}" if t.get("pnl") else "pending"
        pnl_class = "positive" if t.get("pnl", 0) > 0 else "negative" if t.get("pnl", 0) < 0 else ""
        trades_html += f"""
        <tr>
            <td>{t['strategy']}</td>
            <td>{t['market']}</td>
            <td>{t['side']}</td>
            <td>${t['price']:.2f}</td>
            <td class="{pnl_class}">{pnl_str}</td>
            <td>{t.get('outcome', 'open')}</td>
        </tr>"""

    alloc_html = ""
    for strat, pct in config.get("allocations", {}).items():
        alloc_html += f"""
        <div class="alloc-row">
            <span>{strat}</span>
            <span>{pct*100:.0f}%</span>
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Polymarket Engine Dashboard</title>
    <meta http-equiv="refresh" content="60">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }}
        .container {{ max-width: 1000px; margin: 0 auto; }}
        h1 {{ color: #58a6ff; margin-bottom: 8px; }}
        .timestamp {{ color: #8b949e; font-size: 12px; margin-bottom: 20px; }}
        .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }}
        .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 20px; }}
        .card h3 {{ color: #8b949e; font-size: 11px; text-transform: uppercase; margin-bottom: 8px; }}
        .card .value {{ font-size: 28px; font-weight: bold; }}
        .card .value.positive {{ color: #3fb950; }}
        .card .value.negative {{ color: #f85149; }}
        .alloc-row {{ display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #30363d; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ text-align: left; padding: 10px; border-bottom: 1px solid #30363d; }}
        th {{ color: #8b949e; font-weight: normal; font-size: 11px; text-transform: uppercase; }}
        td.positive {{ color: #3fb950; }}
        td.negative {{ color: #f85149; }}
        .status {{ padding: 2px 6px; border-radius: 4px; font-size: 10px; text-transform: uppercase; }}
        .status.win {{ background: #3fb95022; color: #3fb950; }}
        .status.loss {{ background: #f8514922; color: #f85149; }}
        .status.open {{ background: #1f6feb22; color: #58a6ff; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Polymarket Engine</h1>
        <div class="timestamp">Last updated: {timestamp} | DRY RUN: {dry_run}</div>

        <div class="grid">
            <div class="card">
                <h3>Balance</h3>
                <div class="value">${balance:.2f}</div>
            </div>
            <div class="card">
                <h3>Daily P&L</h3>
                <div class="value {"positive" if daily_pnl >= 0 else "negative"}>${daily_pnl:+.2f}</div>
            </div>
            <div class="card">
                <h3>Open Positions</h3>
                <div class="value">{open_pos}</div>
            </div>
            <div class="card">
                <h3>Resolved (24h)</h3>
                <div class="value">{resolved}</div>
            </div>
        </div>

        <div class="grid">
            <div class="card">
                <h3>Allocations</h3>
                {alloc_html}
            </div>
            <div class="card" style="grid-column: span 3;">
                <h3>Risk Limits</h3>
                <div class="alloc-row"><span>Max Daily Loss</span><span>{config.get('risk', {}).get('max_daily_loss_pct', 0)*100:.0f}%</span></div>
                <div class="alloc-row"><span>Max Positions</span><span>{config.get('risk', {}).get('max_open_positions', 5)}</span></div>
                <div class="alloc-row"><span>Max Position %</span><span>{config.get('risk', {}).get('max_position_pct', 0.15)*100:.0f}%</span></div>
                <div class="alloc-row"><span>Min Edge</span><span>{config.get('risk', {}).get('min_edge_after_fees', 0.025)*100:.1f}%</span></div>
            </div>
        </div>

        <div class="card">
            <h3>Recent Trades</h3>
            <table>
                <thead>
                    <tr><th>Strategy</th><th>Market</th><th>Side</th><th>Price</th><th>P&L</th><th>Outcome</th></tr>
                </thead>
                <tbody>
                    {trades_html or '<tr><td colspan="6" style="text-align:center;color:#8b949e;">No recent trades</td></tr>'}
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>"""


def update_gist(data: dict, config: dict) -> bool:
    """Update GitHub Gist with dashboard data."""
    if not GITHUB_TOKEN or not GIST_ID:
        log.warning("dashboard.gist_not_configured")
        return False

    html = generate_dashboard_html(data, config)

    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    files = {
        "dashboard.html": {"content": html},
        "state.json": {"content": json.dumps(data, indent=2)},
    }

    try:
        resp = requests.patch(url, headers=headers, json={"files": files}, timeout=30)
        if resp.ok:
            log.info("dashboard.gist_updated", gist_id=GIST_ID)
            return True
        else:
            log.error("dashboard.gist_error", status=resp.status_code, error=resp.text[:200])
            return False
    except Exception as e:
        log.error("dashboard.gist_exception", error=str(e))
        return False


from datetime import timezone


def main():
    log.info("dashboard.update_start")

    data = get_engine_state()
    if not data:
        log.warning("dashboard.no_data")
        print("No database found - skipping dashboard update")
        return

    config = get_config()

    print(f"Balance: ${data['balance']:.2f}")
    print(f"Daily P&L: ${data['daily_pnl']:+.2f}")
    print(f"Open Positions: {data['open_positions']}")
    print(f"Resolved (24h): {data['resolved_24h']}")

    if GIST_ID and GITHUB_TOKEN:
        success = update_gist(data, config)
        if success:
            print(f"Dashboard updated: https://gist.github.com/{GITHUB_TOKEN[:4]}.../{GIST_ID}")
    else:
        print("DASHBOARD_GIST_ID or GITHUB_TOKEN not set - skipping Gist update")


if __name__ == "__main__":
    main()