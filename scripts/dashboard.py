#!/usr/bin/env python3
"""
scripts/dashboard.py
====================
Simple Flask dashboard to view engine state.

Usage:
    python scripts/dashboard.py

Then open http://localhost:5000
"""

import json
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string

load_dotenv()

app = Flask(__name__)


def get_db_state():
    """Get current database state."""
    from engines.state_engine import StateEngine
    db_path = os.environ.get("DATABASE_PATH", "data/trades.db")
    lessons_path = os.environ.get("LESSONS_PATH", "data/lessons.json")

    if not Path(db_path).exists():
        return {"error": "No database found"}

    state = StateEngine(db_path, lessons_path)
    return {
        "balance": state.get_current_balance(),
        "daily_pnl": state.get_daily_pnl(),
        "open_positions": state.get_open_position_count(),
    }


def get_config():
    """Get engine config."""
    config_path = os.environ.get("ENGINE_CONFIG", "configs/engine.yaml")
    with open(config_path) as f:
        return yaml.safe_load(f)


def get_recent_trades():
    """Get recent resolved trades."""
    from engines.state_engine import StateEngine
    db_path = os.environ.get("DATABASE_PATH", "data/trades.db")
    state = StateEngine(db_path, "data/lessons.json")
    return state.get_recent_resolved_trades(24)


HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Polymarket Engine Dashboard</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 { color: #58a6ff; margin-bottom: 20px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 20px; }
        .card h3 { color: #8b949e; font-size: 12px; text-transform: uppercase; margin-bottom: 8px; }
        .card .value { font-size: 32px; font-weight: bold; }
        .card .value.positive { color: #3fb950; }
        .card .value.negative { color: #f85149; }
        table { width: 100%; border-collapse: collapse; }
        th, td { text-align: left; padding: 12px; border-bottom: 1px solid #30363d; }
        th { color: #8b949e; font-weight: normal; font-size: 12px; }
        .status { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; }
        .status.open { background: #1f6feb22; color: #58a6ff; }
        .status.resolved { background: #23863622; color: #3fb950; }
        .status.win { background: #3fb95022; color: #3fb950; }
        .status.loss { background: #f8514922; color: #f85149; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Polymarket Engine Dashboard</h1>

        <div class="grid">
            <div class="card">
                <h3>Balance</h3>
                <div class="value">${{ "%.2f"|format(state.balance) }}</div>
            </div>
            <div class="card">
                <h3>Daily P&L</h3>
                <div class="value {{ 'positive' if state.daily_pnl >= 0 else 'negative' }}">
                    ${{ "%.2f"|format(state.daily_pnl) }}
                </div>
            </div>
            <div class="card">
                <h3>Open Positions</h3>
                <div class="value">{{ state.open_positions }}</div>
            </div>
            <div class="card">
                <h3>DRY RUN</h3>
                <div class="value" style="font-size: 20px;">{{ config.engine.dry_run }}</div>
            </div>
        </div>

        <div class="card">
            <h3 style="margin-bottom: 16px;">Allocations</h3>
            {% for strat, pct in config.allocations.items() %}
            <div style="margin-bottom: 8px;">
                <span>{{ strat }}</span>
                <span style="float: right;">{{ "%.0f"|format(pct * 100) }}%</span>
            </div>
            {% endfor %}
        </div>

        {% if trades %}
        <div class="card" style="margin-top: 16px;">
            <h3 style="margin-bottom: 16px;">Recent Trades (24h)</h3>
            <table>
                <thead>
                    <tr>
                        <th>Strategy</th>
                        <th>Market</th>
                        <th>Side</th>
                        <th>Price</th>
                        <th>P&L</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>
                    {% for trade in trades %}
                    <tr>
                        <td>{{ trade.strategy }}</td>
                        <td>{{ trade.market_question[:30] }}...</td>
                        <td>{{ trade.side }}</td>
                        <td>${{ "%.2f"|format(trade.price) }}</td>
                        <td class="{{ 'positive' if trade.pnl_usdc and trade.pnl_usdc >= 0 else 'negative' }}">
                            ${{ "%.2f"|format(trade.pnl_usdc or 0) }}
                        </td>
                        <td>
                            <span class="status {{ trade.status }}">{{ trade.status }}</span>
                            {% if trade.outcome %}
                            <span class="status {{ trade.outcome }}">{{ trade.outcome }}</span>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% endif %}
    </div>
</body>
</html>
"""


@app.route("/")
def index():
    state = get_db_state()
    config = get_config()
    trades = get_recent_trades()
    return render_template_string(HTML, state=state, config=config, trades=trades)


@app.route("/api/state")
def api_state():
    return jsonify(get_db_state())


@app.route("/api/trades")
def api_trades():
    return jsonify(get_recent_trades())


if __name__ == "__main__":
    print("Starting dashboard at http://localhost:5000")
    app.run(debug=True, port=5000)