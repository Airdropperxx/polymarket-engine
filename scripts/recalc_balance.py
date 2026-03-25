#!/usr/bin/env python3
"""
scripts/recalc_balance.py

One-time balance correction for trades that were opened BEFORE the BUG-1 fix.
BUG-1: log_trade() did not deduct cost+fee when opening a position.

Correct balance = starting_capital - sum(cost+fee for all open positions)

This is idempotent: safe to run multiple times.
If balance is already correct (within $0.0001) it does nothing.
"""
import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv; load_dotenv()
from engines.state_engine import StateEngine
import structlog
log = structlog.get_logger(component="recalc_balance")

STARTING_CAPITAL = 100.0

def main():
    db_path      = os.environ.get("DATABASE_PATH", "data/trades.db")
    lessons_path = os.environ.get("LESSONS_PATH",  "data/lessons.json")
    capital      = float(os.environ.get("CAPITAL_USDC", str(STARTING_CAPITAL)))

    log.info("recalc_start", db=db_path, capital=capital)

    state    = StateEngine(db_path, lessons_path, capital)
    open_pos = state.get_open_positions()
    all_t    = state.get_all_trades()
    resolved = [t for t in all_t if t.get("status") == "resolved"]
    current  = state.get_current_balance()

    log.info("pipeline_state",
             total_trades=len(all_t),
             open_trades=len(open_pos),
             resolved_trades=len(resolved),
             current_balance=round(current, 4))

    # Sum all open costs
    total_cost = sum(float(p.get("cost_usdc", 0) or 0) for p in open_pos)
    total_fee  = sum(float(p.get("fee_usdc",  0) or 0) for p in open_pos)
    total_spent = round(total_cost + total_fee, 6)

    # Sum resolved pnl (already applied to balance correctly)
    resolved_pnl = sum(float(t.get("pnl_usdc", 0) or 0) for t in resolved)

    # Correct balance: starting capital - open costs + resolved pnl
    # (resolved pnl is already baked into current balance if BUG-1 was partially active)
    # Safest formula: capital - open_spent  (resolved pnl adjustments are already there)
    correct = round(capital - total_spent, 4)

    log.info("balance_audit",
             open_positions=len(open_pos),
             total_cost=round(total_cost, 4),
             total_fee=round(total_fee, 6),
             total_spent=total_spent,
             resolved_pnl=round(resolved_pnl, 6),
             current_balance=round(current, 4),
             correct_balance=correct,
             adjustment=round(correct - current, 4))

    print("=== Balance Recalculation ===")
    print("Open positions  :", len(open_pos))
    print("Total cost      : $" + str(round(total_cost, 4)))
    print("Total fee       : $" + str(round(total_fee, 6)))
    print("Total spent     : $" + str(total_spent))
    print("Resolved trades :", len(resolved))
    print("Resolved PnL    : $" + str(round(resolved_pnl, 4)))
    print("Current balance : $" + str(round(current, 4)))
    print("Correct balance : $" + str(correct))
    print("Adjustment      : $" + str(round(correct - current, 4)))
    print("")

    if abs(correct - current) < 0.0001:
        print("Balance is already correct — no change needed.")
        log.info("recalc_skipped", reason="balance_already_correct")
        return

    state.update_balance(correct)
    after = state.get_current_balance()
    print("Updated balance : $" + str(round(after, 4)))
    log.info("recalc_done",
             old_balance=round(current, 4),
             new_balance=round(after, 4),
             open_positions=len(open_pos),
             spent=total_spent)

if __name__ == "__main__":
    main()