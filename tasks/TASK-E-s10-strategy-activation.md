# TASK-E: Diagnose why S10 executes 0 trades
## Status: PENDING | Priority: High | Time: 20 min

## Background
S10 finds 275 opportunities per scan but 0 are being executed. All trades are S1.

## Investigation
1. Check data/scan_log.json — filter for strategy=s10_near_resolution
2. Look at reason_skipped for S10 entries. Common causes:
   - already_open: Gate 2b is blocking? (S10 has numeric market_ids, shouldnt clash with S1 hex)
   - max_positions: at the cap?
   - daily_loss_limit: shouldnt trigger at $100
3. Check select_top_trades() in scripts/run_scan_cycle.py
   s1_slots parameter may be consuming all 5 max_per_cycle slots
4. Check configs/engine.yaml: max_per_cycle, s1_slots values

## Likely fix
In run_scan_cycle.py, ensure S10 gets at least 1 slot per cycle even when S1 slots fill up.
Or raise max_per_cycle from 5 to 8 in engine.yaml.
