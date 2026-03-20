#!/usr/bin/env python3
"""
scripts/run_daily_review.py
============================
Daily AI review entry point.

Runs ReviewEngine.run_daily_review() (7-day trade window),
sends Telegram daily summary, updates lessons.json.

Called by: .github/workflows/daily_review.yml (00:00 UTC)
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import yaml
import structlog

log = structlog.get_logger()


def main() -> None:
    config = yaml.safe_load(open(os.environ.get("ENGINE_CONFIG", "configs/engine.yaml")))

    from engines.state_engine import StateEngine
    from engines.review_engine import ReviewEngine
    from engines.monitor_engine import MonitorEngine

    state   = StateEngine(
        db_path=os.environ.get("DATABASE_PATH", "data/trades.db"),
        lessons_path=os.environ.get("LESSONS_PATH", "data/lessons.json"),
    )
    monitor = MonitorEngine(config)
    review  = ReviewEngine(state, config)

    # Run review
    result = review.run_daily_review()
    log.info("run_daily_review.done", status=result.get("status"))

    # Send any new lessons to Telegram
    for lesson in result.get("new_lessons", []):
        monitor.send("lesson_update", lesson=lesson)

    # Send daily summary
    monitor.send(
        "daily_summary",
        pnl=state.get_daily_pnl(),
        trades=len(state.get_recent_resolved_trades(hours=24)),
        balance=state.get_current_balance(),
    )

    sys.exit(0)


if __name__ == "__main__":
    main()
