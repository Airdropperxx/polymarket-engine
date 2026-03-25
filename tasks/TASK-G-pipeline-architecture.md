# TASK-G: Pipeline Architecture Reference
## Status: REFERENCE ONLY — read this before editing any engine file

## System Relationships

DataEngine
  -> fetch_all_markets() -> List[MarketState]
  -> fetch_negrisk_groups() -> Dict[group_id, List[MarketState]]
  -> get_cached_markets() -> fallback on API failure

SignalEngine
  -> register(strategy) -> stores strategy instances
  -> run_one_cycle(markets, groups) -> Dict with "opportunities": List[Opportunity]
  -> sorts all opps by score DESC before returning

ExecutionEngine
  -> execute_opportunity(opp, strategy, market_state, bankroll) -> trade_id or None
     Gate 1: daily_loss_limit  (skips: daily_loss_limit)
     Gate 2: max_open_positions (skips: max_positions)
     Gate 2b: duplicate market  (skips: already_open)
     Gate 3: position size      (skips: size_too_small)
     Gate 4: dry_run path       (inserts to DB, returns trade_id)
       -> log_trade() -> row_id (0 = failed INSERT OR IGNORE)
       -> if row_id: log_opportunity(True, trade_id=trade_id)
       -> if not row_id: log_opportunity(False, "db_insert_failed")
     Gate 5: live path (CLOB submit)
  -> check_and_settle(position) -> bool
     -> fetch_market_resolution(market_id, trade_notes)
     -> is_market_resolved(ms, entry_time_iso)
     -> calc_pnl(side, shares, cost, fee, yes_price)
     -> mark_resolved(market_id, outcome, pnl)

StateEngine
  -> log_trade(record) -> row_id
       INSERT OR IGNORE (duplicate trade_id = silent skip, returns 0)
       on success: update_balance(current - cost - fee)  [BUG-1 FIX]
  -> mark_resolved(market_id, outcome, pnl)
       UPDATE trades SET status=resolved
       update_balance(current + pnl_usdc)
  -> get_open_market_ids() -> Set[str]  [BUG-3 Gate 2b]
  -> get_trade_stats() -> Dict  [dashboard source of truth]

## Log Keys — Pipeline Tracing

Run scan cycle:
  scan_cycle_start         entry point
  pipeline_stage           stage=markets_fetched, signal_scan_complete
  execution_queue_built    fair slot allocation stats
  trade_executed           one per successful DB insert
  price_snapshots_taken    open position price history
  cycle_summary            END: full state snapshot (balance, trades, pnl)

Resolution check:
  checking_resolutions     open_positions count
  resolution_attempt       per-trade: is_negrisk, has_leg_ids, notes_preview
  resolution_settled       outcome, pnl, roi
  resolution_skipped       still live
  resolution_error         exception with error field
  resolution_cycle_done    summary: checked/resolved/skipped/errors/balance

Execution engine:
  balance_deducted         cost+fee removed from balance on open [BUG-1]
  dry_run_trade_db_failed  INSERT OR IGNORE returned row_id=0
  position_settled         outcome, pnl, roi_pct, yes_price
  settle_no_market_id      market_id missing from position
  settle_no_price          yes_price not available from API

## What breaks the pipeline and where

| Symptom                            | Where to look          | Log key                  |
|------------------------------------|------------------------|--------------------------|
| S1 trade not resolving             | check_resolutions logs | resolution_attempt       |
| has_leg_ids=false                  | trade notes in DB      | notes_preview in attempt |
| Balance wrong                      | state_engine.py        | balance_deducted         |
| S10 never executes                 | run_scan_cycle logs    | execution_queue_built    |
| scan_log executed=true, 0 DB rows  | execution_engine.py    | dry_run_trade_db_failed  |
| Dashboard count mismatch           | update_dashboard.py    | trades_executed vs total |
