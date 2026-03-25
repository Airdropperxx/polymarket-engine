# TASK-C: Verify end-to-end resolution pipeline
## Status: PENDING | Priority: HIGH | Time: 30 min

## Background
BUG-4 was fixed (time-based expiry for NegRisk LOSS). Need to verify trades actually resolve.

## Steps

1. Go to GitHub Actions -> Resolution Check -> Run workflow (manual trigger)
2. Read the logs. Look for:
   - checking_resolutions open_positions=10
   - negrisk_legs_checked legs=N best_yes=X
   - settled trade_id=... outcome=win/loss pnl=...
3. If still not resolving check for these in logs:
   - hex_market_id_no_leg_ids: old trades without leg_ids. Cannot resolve. Reset DB.
   - negrisk_legs_checked best_yes=0.5: market still live. Wait.
   - Gamma 429/500: rate limited. 3-day hard expiry will catch it.

## Verify
After first resolution dashboard should show:
- resolved_trades > 0
- win_rate_pct = real number
- total_pnl = non-zero
- balance = old_balance + pnl_usdc
