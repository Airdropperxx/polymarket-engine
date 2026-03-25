#!/usr/bin/env python3
"""
scripts/reset_dry_run_data.py
Clears all dry-run trade history and resets balance to $100.
Run once via GitHub Actions workflow_dispatch to start fresh.
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from engines.state_engine import StateEngine
from sqlalchemy import text

def main():
    db_path = os.environ.get('DATABASE_PATH', 'data/trades.db')
    lessons_path = os.environ.get('LESSONS_PATH', 'data/lessons.json')

    state = StateEngine(db_path, lessons_path, initial_balance=100.0)

    with state._engine.begin() as conn:
        # Delete all trades
        result = conn.execute(text('DELETE FROM trades'))
        print(f'Deleted {result.rowcount} trades')
        # Reset balance to $100
        conn.execute(text("DELETE FROM balance"))
        from datetime import datetime, timezone
        conn.execute(text(
            "INSERT INTO balance (id, current_usdc, updated_at) VALUES (1, 100.0, :ts)"
        ), {'ts': datetime.now(timezone.utc).isoformat()})
        print('Balance reset to $100')
        # Clear daily_summary
        conn.execute(text('DELETE FROM daily_summary'))
        print('Daily summary cleared')

    print('Reset complete. Ready for fresh dry-run with $1 position sizes.')

if __name__ == '__main__':
    main()
