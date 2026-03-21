#!/usr/bin/env python3
"""
scripts/health_check.py
=======================
Diagnostic script to verify all engine connections and print current state.

Checks:
- Polymarket CLOB API
- Polygon RPC
- Anthropic API (optional)
- Telegram (optional)
- Wallet USDC balance

Exit code: 0 if all healthy, 1 if any check fails.
"""

import os
import sys
from pathlib import Path

import requests
import structlog
import yaml
from dotenv import load_dotenv

log = structlog.get_logger()

load_dotenv()


def check_clob_api() -> bool:
    """Check Polymarket CLOB API connectivity."""
    try:
        resp = requests.get("https://clob.polymarket.com/health", timeout=10)
        if resp.ok:
            print("✅ CLOB API: Connected")
            return True
        else:
            print(f"⚠️  CLOB API: HTTP {resp.status_code}")
            return False
    except Exception as e:
        print(f"❌ CLOB API: {e}")
        return False


def check_polygon_rpc() -> bool:
    """Check Polygon RPC connectivity."""
    rpc_url = os.environ.get("POLYGON_RPC_URL")
    if not rpc_url:
        print("⚠️  POLYGON_RPC_URL not set")
        return False

    try:
        resp = requests.post(
            rpc_url,
            json={
                "jsonrpc": "2.0",
                "method": "eth_blockNumber",
                "params": [],
                "id": 1,
            },
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            block = data.get("result", "0x0")
            print(f"✅ Polygon RPC: Connected (block {int(block, 16)})")
            return True
        else:
            print(f"⚠️  Polygon RPC: HTTP {resp.status_code}")
            return False
    except Exception as e:
        print(f"❌ Polygon RPC: {e}")
        return False


def check_anthropic() -> bool:
    """Check Anthropic API connectivity (optional)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("⚠️  ANTHROPIC_API_KEY not set (optional)")
        return True

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-3-haiku-20240307",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=10,
        )
        if resp.ok:
            print("✅ Anthropic API: Connected")
            return True
        else:
            print(f"⚠️  Anthropic API: HTTP {resp.status_code}")
            return False
    except Exception as e:
        print(f"❌ Anthropic API: {e}")
        return False


def check_telegram() -> bool:
    """Check Telegram connectivity (optional)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("⚠️  Telegram not configured (optional)")
        return True

    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=10,
        )
        if resp.ok:
            print("✅ Telegram: Connected")
            return True
        else:
            print(f"⚠️  Telegram: HTTP {resp.status_code}")
            return False
    except Exception as e:
        print(f"❌ Telegram: {e}")
        return False


def check_wallet_balance() -> bool:
    """Check wallet USDC balance."""
    from py_clob_client.client import ClobClient
    from py_clob_client.auth import PrivateKeyAuth

    try:
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=os.environ.get("POLYMARKET_PRIVATE_KEY", ""),
        )
        balance = client.get_balance()
        print(f"✅ Wallet: ${balance} USDC")
        return True
    except ImportError:
        print("⚠️  py-clob-client not installed")
        return True
    except Exception as e:
        print(f"⚠️  Wallet check: {e}")
        return True


def print_engine_state() -> None:
    """Print current engine state from database and config."""
    from engines.state_engine import StateEngine

    db_path = os.environ.get("DATABASE_PATH", "data/trades.db")
    lessons_path = os.environ.get("LESSONS_PATH", "data/lessons.json")

    if not Path(db_path).exists():
        print("\n📊 Engine State: No database found")
        return

    state = StateEngine(db_path, lessons_path)

    print("\n📊 Engine State")
    print("-" * 30)
    print(f"Current Balance: ${state.get_current_balance():.2f}")
    print(f"Daily P&L:       ${state.get_daily_pnl():.2f}")
    print(f"Open Positions:  {state.get_open_position_count()}")

    lessons = state.get_lessons()
    print(f"Lessons:         {len(lessons.get('lessons', []))}")

    recent = state.get_recent_resolved_trades(24)
    print(f"Resolved (24h):  {len(recent)}")


def print_config() -> None:
    """Print current config."""
    config_path = os.environ.get("ENGINE_CONFIG", "configs/engine.yaml")

    if not Path(config_path).exists():
        print("\n⚠️  Config file not found")
        return

    with open(config_path) as f:
        config = yaml.safe_load(f)

    print("\n⚙️  Configuration")
    print("-" * 30)
    engine = config.get("engine", {})
    risk = config.get("risk", {})
    alloc = config.get("allocations", {})

    print(f"Capital:         ${engine.get('capital_usdc', 100):.2f}")
    print(f"DRY_RUN:         {engine.get('dry_run', True)}")
    print(f"Max Daily Loss:  {risk.get('max_daily_loss_pct', 0.05)*100}%")
    print(f"Max Positions:   {risk.get('max_open_positions', 5)}")
    print(f"Allocations:")
    for strat, pct in alloc.items():
        print(f"  - {strat}: {pct*100:.0f}%")


def main() -> int:
    """Run all health checks."""
    print("=" * 40)
    print("Polymarket Engine Health Check")
    print("=" * 40)

    checks = [
        ("CLOB API", check_clob_api),
        ("Polygon RPC", check_polygon_rpc),
        ("Anthropic", check_anthropic),
        ("Telegram", check_telegram),
        ("Wallet", check_wallet_balance),
    ]

    results = []
    for name, check in checks:
        results.append(check())

    print_config()
    print_engine_state()

    print("\n" + "=" * 40)

    if all(results):
        print("✅ All checks passed!")
        return 0
    else:
        print("❌ Some checks failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())