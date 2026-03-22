"""
scripts/check_resolutions.py — Check open positions for resolution.
Runs hourly via resolve_check.yml.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
import structlog
from engines.state_engine import StateEngine
from engines.data_engine import DataEngine
from engines.execution_engine import ExecutionEngine
from engines.review_engine import ReviewEngine

log = structlog.get_logger(component="check_resolutions")


def main():
    with open("configs/engine.yaml") as f:
        engine_cfg = yaml.safe_load(f)

    capital = float(engine_cfg.get("engine", {}).get("capital_usdc", 100.0))

    state_engine = StateEngine(
        db_path="data/trades.db",
        lessons_path="data/lessons.json",
        initial_balance=capital,
    )
    data_engine = DataEngine(engine_cfg)
    execution_engine = ExecutionEngine(
        state_engine=state_engine,
        data_engine=data_engine,
        config=engine_cfg,
        dry_run=True,   # resolution check never places orders
    )

    open_positions = state_engine.get_open_positions()
    log.info("checking_resolutions", open_positions=len(open_positions))

    resolved_count = 0
    for pos in open_positions:
        settled = execution_engine.check_and_settle(pos)
        if settled:
            resolved_count += 1

    if resolved_count > 0:
        reviewer = ReviewEngine(state_engine, engine_cfg)
        reviewer.run_after_resolution()
        log.info("review_triggered", resolved=resolved_count)

    # Commit any state changes
    import subprocess
    subprocess.run(["git", "config", "user.email", "engine@polymarket-bot"],
                   capture_output=True)
    subprocess.run(["git", "config", "user.name", "Polymarket Engine"],
                   capture_output=True)
    subprocess.run(["git", "add", "data/"], capture_output=True)
    status = subprocess.run(["git", "diff", "--cached", "--quiet"],
                            capture_output=True)
    if status.returncode != 0:
        import time
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        subprocess.run(["git", "commit", "-m", f"resolve: {ts} [{resolved_count} settled]"],
                       capture_output=True)
        subprocess.run(["git", "push"], capture_output=True)

    log.info("resolution_check_complete", resolved=resolved_count)
    sys.exit(0)


if __name__ == "__main__":
    main()
