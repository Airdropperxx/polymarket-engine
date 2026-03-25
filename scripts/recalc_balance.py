#!/usr/bin/env python3
"""
scripts/recalc_balance.py
One-time balance correction for trades opened before BUG-1 fix.

BUG-1: log_trade() did not deduct cost+fee from balance when opening.
This corrects balance = starting_capital - sum(cost+fee for open positions).
Safe to run multiple times (idempotent).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv; load_dotenv()
from engines.state_engine import StateEngine
import structlog
log = structlog.get_logger(component="recalc_balance")

def main():
    state      = StateEngine("data/trades.db", "data/lessons.json")
    open_pos   = state.get_open_positions()
    capital    = 100.0

    total_cost  = sum(float(p.get("cost_usdc", 0) or 0) for p in open_pos)
    total_fee   = sum(float(p.get("fee_usdc",  0) or 0) for p in open_pos)
    total_spent = total_cost + total_fee
    current     = state.get_current_balance()
    correct     = capital - total_spent

    print("Open positions  :", len(open_pos))
    print("Total cost      : $" + str(round(total_cost, 4)))
    print("Total fee       : $" + str(round(total_fee, 6)))
    print("Total spent     : $" + str(round(total_spent, 4)))
    print("Current balance : $" + str(round(current, 4)))
    print("Correct balance : $" + str(round(correct, 4)))
    print("Adjustment      : $" + str(round(correct - current, 4)))

    if abs(correct - current) < 0.0001:
        print("Balance already correct — no change needed.")
        return

    state.update_balance(correct)
    after = state.get_current_balance()
    print("Updated balance : $" + str(round(after, 4)))
    log.info("balance_recalculated",
             old=round(current,4), new=round(after,4),
             open_positions=len(open_pos), spent=round(total_spent,4))

if __name__ == "__main__":
    main()