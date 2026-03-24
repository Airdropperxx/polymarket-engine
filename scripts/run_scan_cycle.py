"""
scripts/run_scan_cycle.py — Entry point for scan.yml GitHub Actions workflow.

One complete cycle:
  1. Load config
  2. DataEngine: fetch/refresh market data (uses compressed snapshot)
  3. SignalEngine: scan all strategies → rank opportunities
  4. ExecutionEngine: execute top opportunities (with DRY_RUN guard)
  5. StateEngine: check resolutions, update balance
  6. ReviewEngine: run if new resolutions
  7. MonitorEngine: send Telegram summary
  8. Commit data/ to git (snapshot + trades.db + lessons.json)

This script exits with code 0 on success, 1 on unrecoverable error.
GitHub Actions reads the exit code and marks the step pass/fail.
"""

import os
import sys
import time
import subprocess
import structlog
import yaml
from pathlib import Path

log = structlog.get_logger(component="run_scan_cycle")


def load_config() -> dict:
    config_path = Path("configs/engine.yaml")
    if not config_path.exists():
        log.error("config_missing", path=str(config_path))
        sys.exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f)


def load_strategy_configs() -> dict:
    """
    Load all per-strategy YAML configs and merge into one dict.

    Permanent fix: uses yaml.safe_load_all() so multi-document YAML files
    (those containing --- separators) never raise ComposerError.
    Skips 'strategies.yaml' which is a legacy concatenated file.
    """
    merged = {}
    configs_dir = Path("configs")
    for yml_path in sorted(configs_dir.glob("s*.yaml")):
        # Skip the legacy multi-doc concatenation file if it still exists
        if yml_path.name == "strategies.yaml":
            log.warning("skipping_legacy_multi_doc_yaml", file=yml_path.name)
            continue
        try:
            with open(yml_path) as f:
                # safe_load_all handles both single-doc and multi-doc YAML files
                for doc in yaml.safe_load_all(f):
                    if isinstance(doc, dict):
                        merged.update(doc)
        except Exception as e:
            log.warning("strategy_config_load_failed", file=yml_path.name, error=str(e))
    return merged


def commit_data() -> None:
    """Commit data/ changes back to repo. Required for GHA persistence."""
    try:
        result = subprocess.run(
            ["git", "config", "user.email", "engine@polymarket-bot"],
            capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Polymarket Engine"],
            capture_output=True
        )
        subprocess.run(["git", "add", "data/"], capture_output=True)

        # Only commit if there are actual changes
        status = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            capture_output=True
        )
        if status.returncode != 0:  # returncode=1 means changes exist
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            subprocess.run(
                ["git", "commit", "-m", f"scan: {ts} [automated]"],
                capture_output=True
            )
            subprocess.run(["git", "push"], capture_output=True)
            log.info("data_committed", timestamp=ts)
        else:
            log.info("data_no_changes_skip_commit")
    except Exception as e:
        log.warning("git_commit_failed", error=str(e))
        # Non-fatal — data is still on disk for next run


def main():
    start_time = time.time()
    dry_run    = os.environ.get("DRY_RUN", "true").lower() == "true"

    log.info("scan_cycle_start", dry_run=dry_run)

    # ── 1. Load config ────────────────────────────────────────────────────────
    engine_cfg   = load_config()
    strategy_cfg = load_strategy_configs()
    full_cfg     = {**engine_cfg, **strategy_cfg}

    capital      = float(engine_cfg.get("engine", {}).get("capital_usdc", 100.0))
    risk_cfg     = engine_cfg.get("engine", {}).get("risk", {})
    max_per_cycle = int(engine_cfg.get("engine", {}).get("max_per_cycle", 3))

    # ── 2. DataEngine: fetch markets ──────────────────────────────────────────
    from engines.data_engine import DataEngine

    data_engine = DataEngine(full_cfg)
    markets     = data_engine.fetch_all_markets()
    groups      = data_engine.fetch_negrisk_groups()

    log.info("markets_fetched",
             total=len(markets),
             negrisk_groups=len(groups))

    if not markets:
        log.warning("no_markets_fetched_trying_cache")
        markets = data_engine.get_cached_markets()

    if not markets:
        # No data from API or cache — log but DO NOT exit.
        # Resolution checks, dashboard updates, and commits still need to run.
        # The data engine is fully decoupled: its failure must not stop settlements.
        log.warning("no_markets_available_continuing_for_settlements",
                    reason="Gamma API unreachable and cache empty — skipping signal scan only")
    else:
        log.info("markets_ready", count=len(markets))

    # ── 3. SignalEngine: scan and rank (only if markets available) ─────────────
    all_opps = []
    if markets:
        from engines.signal_engine import SignalEngine
        from strategies.s10_near_resolution import S10NearResolution
        from strategies.s1_negrisk_arb import S1NegRiskArb
        from strategies.s8_logical_arb import S8LogicalArb

        signal_engine = SignalEngine(full_cfg)
        signal_engine.register(S10NearResolution())
        signal_engine.register(S1NegRiskArb())
        signal_engine.register(S8LogicalArb())

        cycle_result = signal_engine.run_one_cycle(markets, groups)
        all_opps     = cycle_result.get("opportunities", [])
        log.info("signal_scan_complete",
                 opportunities_found=len(all_opps),
                 markets_scanned=cycle_result.get("markets_scanned", 0))
    else:
        log.info("signal_scan_skipped", reason="no_markets")
        signal_engine = None

    # ── 4. ExecutionEngine ────────────────────────────────────────────────────
    from engines.state_engine import StateEngine
    from engines.execution_engine import ExecutionEngine

    state_engine = StateEngine(
        db_path          = "data/trades.db",
        lessons_path     = "data/lessons.json",
        initial_balance  = capital,
    )
    execution_engine = ExecutionEngine(
        state_engine = state_engine,
        data_engine  = data_engine,
        config       = full_cfg,
        dry_run      = dry_run,
    )

    # Safety checks
    daily_pnl = state_engine.get_daily_pnl()
    max_daily_loss = capital * float(risk_cfg.get("max_daily_loss_pct", 0.05))
    if daily_pnl < -max_daily_loss:
        log.warning("daily_loss_limit_hit",
                    daily_pnl=daily_pnl, max_loss=max_daily_loss)
        commit_data()
        sys.exit(0)

    open_count = state_engine.get_open_position_count()
    max_open   = int(risk_cfg.get("max_open_positions", 5))
    if open_count >= max_open:
        log.info("max_open_positions_reached",
                 open=open_count, max=max_open)
        commit_data()
        sys.exit(0)

    # Execute top opportunities
    trades_executed = 0
    current_balance = state_engine.get_current_balance()

    # Get strategy instances (needed for size() calls)
    strategy_map = {s.name: s for s in signal_engine.strategies} if signal_engine else {}

    for opp in all_opps[:max_per_cycle]:
        if trades_executed >= max_per_cycle:
            break
        if open_count + trades_executed >= max_open:
            break

        strategy = strategy_map.get(opp.strategy)
        if not strategy:
            continue

        # Re-fetch market for fresh fee_rate_bps before execution
        market_state = data_engine.get_single_market(
            opp.metadata.get("token_id", "")
        )

        trade_id = execution_engine.execute_opportunity(
            opp, strategy, market_state, current_balance
        )
        if trade_id:
            trades_executed += 1
            log.info("trade_executed",
                     trade_id=trade_id,
                     strategy=opp.strategy,
                     edge=opp.edge,
                     score=opp.score,
                     dry_run=dry_run)

    # ── 5. Check resolutions (Gamma API — works for ALL dry+live trades) ──────
    open_positions = state_engine.get_open_positions()
    resolved_count = 0

    for pos in open_positions:
        try:
            # check_and_settle now uses Gamma API by market_id — no token_id needed
            settled = execution_engine.check_and_settle(pos)
            if settled:
                resolved_count += 1
        except Exception as e:
            log.warning("resolution_check_failed",
                        market_id=pos.get("market_id"), error=str(e))

    # ── 6. ReviewEngine: run if new resolutions ────────────────────────────────
    if resolved_count > 0:
        try:
            from engines.review_engine import ReviewEngine
            reviewer = ReviewEngine(state_engine, full_cfg)
            reviewer.run_after_resolution()
        except Exception as e:
            log.warning("review_engine_failed", error=str(e))

    # ── 7. MonitorEngine: send summary ───────────────────────────────────────
    try:
        from engines.monitor_engine import MonitorEngine
        monitor = MonitorEngine(full_cfg)
        elapsed = round(time.time() - start_time, 1)
        monitor.send_scan_summary(
            markets_scanned  = len(markets),
            opportunities    = len(all_opps),
            trades_executed  = trades_executed,
            resolved         = resolved_count,
            balance          = state_engine.get_current_balance(),
            daily_pnl        = state_engine.get_daily_pnl(),
            elapsed_sec      = elapsed,
            dry_run          = dry_run,
            trade_stats      = state_engine.get_trade_stats(),
        )
    except Exception as e:
        log.warning("monitor_engine_failed", error=str(e))

    # ── 8. Update dashboard ──────────────────────────────────────────────────
    try:
        import importlib.util
        _spec = importlib.util.spec_from_file_location("ud", Path("scripts/update_dashboard.py"))
        _mod  = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _mod.main()
    except Exception as e:
        log.warning("dashboard_update_failed", error=str(e))

    # ── 9. Commit data/ to git ─────────────────────────────────────────────────
    commit_data()

    elapsed = round(time.time() - start_time, 1)
    log.info("scan_cycle_complete",
             elapsed_sec=elapsed,
             markets=len(markets),
             opportunities=len(all_opps),
             trades=trades_executed,
             resolved=resolved_count,
             dry_run=dry_run)

    sys.exit(0)


if __name__ == "__main__":
    main()
