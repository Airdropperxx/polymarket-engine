# TASK-D: Dashboard display improvements
## Status: PENDING | Priority: Medium | Time: 1 hour

## Background
BUG-1 and BUG-2 are fixed. Balance and trade counts are now correct.
New fields available in engine-data JSON:
  - scan_log_executed: pipeline executions (vs trades_executed = DB total)
  - trade_id: each scan_log entry now has the DB trade_id for reconciliation

## Changes needed in index.html (JS only — no Python changes needed)

### 1. Available Cash KPI card
Formula: balance - open_exposure
Display: "$94.15 available" next to Balance card

### 2. Fix Trades Executed label confusion
Was: trades_executed = scan_log count (cumulative, survives resets)
Now: trades_executed = DB total (correct)
     scan_log_executed = pipeline count (for debugging)
Dashboard label: rename to "DB Trades" for clarity
Add secondary: "Pipeline Executions: {scan_log_executed}"

### 3. Scan log table improvements
Add columns: Volume ($), Shares, Cost ($), Days Left, trade_id (truncated)
Add filter button: hide vol < $1000 entries
Color rows: executed=true -> green bg, executed=false -> grey

### 4. Trade table improvements  
Status badge colors: open=grey, win=green bg, loss=red bg
Add: Days Open column = (now - entry_time) in days
Add: PnL column showing win/loss with color

### 5. Pipeline health section (new)
Show last cycle_summary data if available in scan_log:
  - Last scan time
  - markets_fetched, opportunities_found
  - trades_executed, resolved_this_cycle
  - at_position_cap (warn if true)

## Files to edit
- index.html: JS render functions rTrades(), rKPIs(), rScanLog()
- No Python changes needed
