#!/usr/bin/env python3
"""
scripts/run_scan_cycle.py
=========================
GitHub Actions / local cron entry point.

Runs ONE complete scan cycle then exits.
No infinite loop — scheduling is handled by GitHub Actions cron or local crontab.
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

    # Create engines
    from engines.state_engine import StateEngine
    from engines.data_engine import DataEngine
    from engines.monitor_engine import MonitorEngine
    from engines.review_engine import ReviewEngine
    from engines.signal_engine import SignalEngine

    state   = StateEngine(
        db_path=os.environ.get("DATABASE_PATH", "data/trades.db"),
        lessons_path=os.environ.get("LESSONS_PATH", "data/lessons.json"),
    )
    data    = DataEngine(config)
    monitor = MonitorEngine(config)
    review  = ReviewEngine(state, config)

    # Try to create execution engine
    execute = None
    try:
        from engines.execution_engine import ExecutionEngine
        try:
            from py_clob_client.client import ClobClient
            clob = ClobClient(
                host="https://clob.polymarket.com",
                key=os.environ.get("POLYMARKET_PRIVATE_KEY", ""),
                chain_id=137,
            )
        except ImportError:
            log.warning("run_scan_cycle.clob_client_unavailable")
            clob = None
        except Exception as e:
            log.warning("run_scan_cycle.clob_init_error", error=str(e))
            clob = None

        if clob:
            execute = ExecutionEngine(clob, state, config, dry_run=dry_run)
    except ImportError:
        log.warning("run_scan_cycle.execution_engine_unavailable")

    # Mock execution if not available
    if execute is None:
        from unittest.mock import MagicMock
        execute = MagicMock()
        execute.execute_opportunity = lambda *args, **kwargs: None

    hub = SignalEngine(data, execute, state, review, monitor, config)

    # Register strategies - try imports, skip if unavailable
    try:
        from strategies.s10_near_resolution import NearResolutionStrategy
        hub.register(NearResolutionStrategy(), "configs/s10_near_resolution.yaml")
    except ImportError:
        log.warning("run_scan_cycle.s10_unavailable")

    try:
        from strategies.s1_negrisk_arb import NegRiskArbStrategy
        hub.register(NegRiskArbStrategy(), "configs/s1_negrisk.yaml")
    except ImportError:
        log.warning("run_scan_cycle.s1_unavailable")

    try:
        from strategies.s8_logical_arb import LogicalArbStrategy
        hub.register(LogicalArbStrategy(), "configs/s8_logical.yaml")
    except ImportError:
        log.warning("run_scan_cycle.s8_unavailable")

    result = hub.run_one_cycle()

    log.info("run_scan_cycle.done", **result)
    sys.exit(0)


if __name__ == "__main__":
    main()