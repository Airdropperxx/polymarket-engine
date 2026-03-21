#!/usr/bin/env python3
"""
scripts/update_dashboard.py
===========================
Updates dashboard.html with embedded engine state.

Run after each scan cycle to keep dashboard fresh.
"""

import json
import os
import re
import sys
import html
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

import structlog
from dotenv import load_dotenv

load_dotenv()

log = structlog.get_logger()


def escape_html(text):
    """Escape HTML to prevent XSS."""
    if text is None:
        return ""
    return html.escape(str(text))


def get_engine_state():
    """Get current engine state from database."""
    from engines.state_engine import StateEngine

    db_path = os.environ.get("DATABASE_PATH", "data/trades.db")
    lessons_path = os.environ.get("LESSONS_PATH", "data/lessons.json")

    if not os.path.exists(db_path):
        return None

    state = StateEngine(db_path, lessons_path)

    open_positions = state.get_open_positions()
    recent = state.get_recent_resolved_trades(24)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        "balance": float(state.get_current_balance()),
        "daily_pnl": float(state.get_daily_pnl()),
        "open_positions": len(open_positions),
        "resolved_24h": len(recent),
        "dry_run": os.environ.get("DRY_RUN", "false").lower() == "true",
        "recent_trades": [
            {
                "strategy": escape_html(t.get("strategy", "")),
                "market": escape_html(t.get("market_question", ""))[:60],
                "side": escape_html(t.get("side", "")),
                "price": float(t.get("price", 0)),
                "outcome": escape_html(t.get("outcome", "")) if t.get("outcome") else "",
                "pnl": float(t.get("pnl_usdc", 0)) if t.get("pnl_usdc") else 0,
            }
            for t in recent[:10]
        ],
    }


def update_dashboard_html(data: dict) -> bool:
    """Update dashboard.html with embedded data."""
    dashboard_path = "dashboard.html"

    if not os.path.exists(dashboard_path):
        log.warning("dashboard.file_not_found")
        return False

    with open(dashboard_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    json_str = json.dumps(data)

    pattern = r'var STATE_DATA = \{[^}]*\};'
    if re.search(pattern, html_content):
        html_content = re.sub(pattern, f'var STATE_DATA = {json_str};', html_content)
    else:
        html_content = html_content.replace("var STATE_DATA = null;", f"var STATE_DATA = {json_str};")

    with open(dashboard_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    log.info("dashboard.updated", path=dashboard_path)
    return True


def main():
    log.info("dashboard.update_start")

    data = get_engine_state()
    if not data:
        print("No database found - skipping dashboard update")
        return

    print(f"Balance: ${data['balance']:.2f}")
    print(f"Daily P&L: ${data['daily_pnl']:+.2f}")
    print(f"Open Positions: {data['open_positions']}")
    print(f"Resolved (24h): {data['resolved_24h']}")
    print(f"Dry Run: {data['dry_run']}")

    success = update_dashboard_html(data)
    if success:
        print("Dashboard updated: dashboard.html")


if __name__ == "__main__":
    main()