# TASK-C: Verify resolution pipeline end-to-end
## Status: READY TO RUN | Priority: HIGH | Time: 20 min

## What was fixed so far
- BUG-4: NegRisk time-based expiry added to is_market_resolved()
- check_resolutions.py: full per-trade debug logging added
- Every resolution attempt now logs: is_negrisk, has_leg_ids, notes_preview

## Step 1: Run Recalculate Balance workflow FIRST
Go to: GitHub Actions -> Recalculate Balance -> Run workflow -> type YES
This corrects balance from $100.00 to ~$94.15 for the 15 pre-fix open trades.

## Step 2: Run Resolution Check manually
Go to: GitHub Actions -> Resolution Check -> Run workflow
Then read the logs. Look for these exact log keys:

  resolution_attempt  -- one per open trade (should see 15)
    is_negrisk: true/false
    has_leg_ids: true/false   <-- CRITICAL: must be true for S1 trades
    notes_preview: "DRY score=... leg_ids=1234,5678..."

  resolution_settled  -- if a trade resolved (outcome + pnl)
  resolution_skipped  -- trade still live (expected for most)
  resolution_error    -- if an exception occurred (check error field)

  resolution_cycle_done  -- summary: checked/resolved/skipped/errors/balance

## Step 3: If has_leg_ids=false for S1 trades
The trade was opened before leg_ids were stored in notes.
These trades CANNOT resolve via API. Two options:
  A) Wait for the market to hard-expire (3+ days past end_date)
  B) Reset the DB: GitHub Actions -> Reset Dry Run Data -> Run workflow

## Step 4: Expected outcome after fixes
- Trades with end_date > 3 days ago: automatically resolved as LOSS
- Trades with any leg yes_price >= 0.99: resolved as WIN
- Balance updates correctly after each resolution

## Files involved
- scripts/check_resolutions.py
- engines/execution_engine.py  (check_and_settle)
- engines/trade_analytics.py   (fetch_market_resolution, is_market_resolved)
- engines/state_engine.py      (mark_resolved, update_balance)
