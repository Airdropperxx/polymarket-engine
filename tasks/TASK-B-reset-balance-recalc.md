# TASK-B: One-time balance recalculation for pre-fix trades
## Status: PENDING | Priority: HIGH — run once after BUG-1 fix deploys
## Time: 10 min

## Background
BUG-1 fix (balance deduction on open) was deployed but the 10 existing open trades
were logged BEFORE the fix. Their cost was never deducted.
Current balance: 100.00 (wrong). Should be ~95.70.

## Script to create and run: scripts/recalc_balance.py
Create this file:

  import sys
  from pathlib import Path
  sys.path.insert(0, str(Path(__file__).parent.parent))
  from engines.state_engine import StateEngine
  state = StateEngine("data/trades.db", "data/lessons.json")
  open_pos = state.get_open_positions()
  total_spent = sum(
      float(p.get("cost_usdc",0) or 0) + float(p.get("fee_usdc",0) or 0)
      for p in open_pos
  )
  current = state.get_current_balance()
  new_bal = current - total_spent
  print(f"Open: {len(open_pos)}, Spent: {total_spent:.4f}, Old bal: {current:.4f}, New: {new_bal:.4f}")
  state.update_balance(new_bal)
  print("Done.")

Then add .github/workflows/recalc_balance.yml with workflow_dispatch trigger.
Run it once from GitHub Actions UI.

## Verify
Dashboard balance should drop from ~100 to ~95.70 after running.
