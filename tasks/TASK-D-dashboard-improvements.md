# TASK-D: Dashboard display improvements
## Status: PENDING | Priority: Medium | Time: 45 min

## Changes needed in index.html

1. Available Cash card: balance - open_exposure. Add to KPI row.
2. Rename Trades Executed -> Total Trades. Keep Pipeline Executed showing scan_log_executed.
3. Scan log table: add Volume, Shares, Cost, Days Left columns. Toggle to hide vol < 1000.
4. Trade table: color open=grey, win=green, loss=red. Add Days Open column.
5. KPI row: add Exposure card showing open_exposure.

## No Python changes needed. Edit index.html JS render functions only:
- rTrades() - trade table
- rKPIs() - stat cards  
- rScanLog() - scan log table
