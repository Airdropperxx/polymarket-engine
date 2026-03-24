# Polymarket Engine — P&L Fix

## How to apply

```bash
cd /path/to/polymarket-engine

# Copy the fixed files over your existing ones
cp fix/engines/trade_analytics.py   engines/
cp fix/engines/execution_engine.py  engines/
cp fix/engines/state_engine.py      engines/
cp fix/engines/review_engine.py     engines/
cp fix/engines/monitor_engine.py    engines/
cp fix/scripts/check_resolutions.py scripts/
cp fix/scripts/update_dashboard.py  scripts/

# Commit and push
git add engines/ scripts/
git commit -m "fix: complete dry-run P&L pipeline — resolution, math, analytics, review"
git push
```

## What was fixed

| File | Bug | Fix |
|---|---|---|
| `engines/trade_analytics.py` | **NEW** — no central math module existed | Created: Kelly sizing, Gamma resolution, portfolio stats, ROI |
| `engines/execution_engine.py` | `check_and_settle` required CLOB token_id → dry trades **never resolved** | Now uses Gamma API by market_id |
| `engines/execution_engine.py` | Fee math double-multiplied (`calc_fee * price * cost`) | Fixed: `cost_usdc * fee_bps / 10000` |
| `engines/state_engine.py` | `get_open_positions()` had no `get_all_trades()` method | Added full trade query methods |
| `engines/review_engine.py` | Prompt had no P&L math — AI couldn't learn | Now feeds win_rate, ROI, edge accuracy, EV accuracy, per-strategy stats |
| `engines/monitor_engine.py` | Telegram alert had no trade stats | Now includes win rate, ROI, best/worst trade |
| `scripts/check_resolutions.py` | Never called ReviewEngine after settling | Now triggers learning loop after each resolution |
| `scripts/update_dashboard.py` | Hardcoded zeros for all stats | Now reads from scan_log.json for real stats |
