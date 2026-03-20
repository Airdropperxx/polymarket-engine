#!/usr/bin/env python3
"""
scripts/run_scan_cycle.py
=========================
GitHub Actions / local cron entry point.

Runs ONE complete scan cycle then exits.
No infinite loop — scheduling is handled by GitHub Actions cron or local crontab.

Usage:
    python scripts/run_scan_cycle.py           # Live mode (DRY_RUN from env)
    DRY_RUN=true python scripts/run_scan_cycle.py  # Force dry run

Called by: .github/workflows/scan.yml (every 30 min)
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
    config_path = os.environ.get("ENGINE_CONFIG", "configs/engine.yaml")
    config = yaml.safe_load(open(config_path))

    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    if dry_run:
        log.warning("run_scan_cycle.dry_run_mode")

    # Wire up engines
    from py_clob_client.client import ClobClient
    from engines.state_engine import StateEngine
    from engines.data_engine import DataEngine
    from engines.execution_engine import ExecutionEngine
    from engines.review_engine import ReviewEngine
    from engines.monitor_engine import MonitorEngine
    from engines.signal_engine import SignalEngine
    from strategies.s10_near_resolution import NearResolutionStrategy
    from strategies.s1_negrisk_arb import NegRiskArbStrategy
    from strategies.s8_logical_arb import LogicalArbStrategy

    clob = ClobClient(
        host="https://clob.polymarket.com",
        key=os.environ["POLYMARKET_PRIVATE_KEY"],
        chain_id=137,
    )

    state   = StateEngine(
        db_path=os.environ.get("DATABASE_PATH", "data/trades.db"),
        lessons_path=os.environ.get("LESSONS_PATH", "data/lessons.json"),
    )
    data    = DataEngine(config)
    monitor = MonitorEngine(config)
    review  = ReviewEngine(state, config)
    execute = ExecutionEngine(clob, state, config, dry_run=dry_run)

    hub = SignalEngine(data, execute, state, review, monitor, config)
    hub.register(NearResolutionStrategy(), "configs/s10_near_resolution.yaml")
    hub.register(NegRiskArbStrategy(),     "configs/s1_negrisk.yaml")
    hub.register(LogicalArbStrategy(),     "configs/s8_logical.yaml")

    result = hub.run_one_cycle()

    log.info("run_scan_cycle.done", **result)
    sys.exit(0)


if __name__ == "__main__":
    main()
