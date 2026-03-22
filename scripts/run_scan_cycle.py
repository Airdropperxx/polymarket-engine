"""
scripts/run_scan_cycle.py — Entry point for scan.yml GitHub Actions workflow.
"""

import os
import sys
import time
import subprocess
from pathlib import Path

# Add project root to path so 'engines' and 'strategies' are importable
sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
import structlog

log = structlog.get_logger(component="run_scan_cycle")


def load_config() -> dict:
    """Load engine.yaml + all s*.yaml strategy configs into one dict."""
    config_dir = Path("configs")

    # Engine config
    engine_path = config_dir / "engine.yaml"
    if not engine_path.exists():
        log.error("engine_yaml_missing", path=str(engine_path))
        sys.exit(1)

    with open(engine_path) as f:
        cfg = yaml.safe_load(f)

    # All strategy configs (s10_near_resolution.yaml, s1_negrisk.yaml, etc.)
    for yml_path in sorted(config_dir.glob("s*.yaml")):
        try:
            with open(yml_path) as f:
                strat_cfg = yaml.safe_load(f)
            if isinstance(strat_cfg, dict):
                cfg.update(strat_cfg)
                log.info("strategy_config_loaded", file=yml_path.name)
        except Exception as e:
            log.warning("strategy_config_load_failed",
                        file=yml_path.name, error=str(e))

    return cfg


def commit_data() -> None:
    """Commit data/ changes back to repo for persistence across GHA runs."""
    try:
        subprocess.run(["git", "config", "user.email", "engine@polymarket-bot"],
                       capture_output=True)
        subprocess.run(["git", "config", "user.name", "Polymarket Engine"],
                       capture_output=True)
        subprocess.run(["git", "add", "data/"], capture_output=True)

        status = subprocess.run(["git", "diff", "--cached", "--quiet"],
                                 capture_output=True)
        if status.returncode != 0:
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            subprocess.run(
                ["git", "commit", "-m", f"scan: {ts} [automated]"],
                capture_output=True,
            )
            subprocess.run(["git", "push"], capture_output=True)
            log.info("data_committed", component="run_scan_cycle")
        else:
            log.info("data_no_changes", component="run_scan_cycle")
    except Exception as e:
        log.warning("git_commit_failed", error=str(e))


def main():
    start    = time.time()
    dry_run  = os.environ.get("DRY_RUN", "true").lower() == "true"

    log.info("scan_cycle_start", dry_run=dry_run)

    # ── Config ────────────────────────────────────────────────────────────
    config  = load_config()
    engine  = config.get("engine", {})
    capital = float(engine.get("capital_usdc", 100.0))
    risk    = engine.get("risk", {})
    max_per = int(engine.get("max_per_cycle", 3))

    # ── Data Engine ───────────────────────────────────────────────────────
    from engines.data_engine import DataEngine

    data_engine = DataEngine(config)
    markets     = data_engine.fetch_all_markets()
    groups      = data_engine.fetch_negrisk_groups()

    log.info("markets_fetched",
             component="run_scan_cycle",
             total=len(markets),
             negrisk_groups=len(groups))

    if not markets:
        log.warning("no_markets_fetched_using_cache",
                    component="run_scan_cycle")
        markets = data_engine.get_cached_markets()

    if not markets:
        log.error("no_markets_available", component="run_scan_cycle")
        commit_data()
        sys.exit(0)

    # ── Signal Engine ─────────────────────────────────────────────────────
    from engines.signal_engine import SignalEngine
    from strategies.s10_near_resolution import S10NearResolution
    from strategies.s1_negrisk_arb import S1NegRiskArb
    from strategies.s8_logical_arb import S8LogicalArb

    signal_engine = SignalEngine(config)
    signal_engine.register(S10NearResolution())
    signal_engine.register(S1NegRiskArb())
    signal_engine.register(S8LogicalArb())

    cycle = signal_engine.run_one_cycle(markets, groups)
    all_opps = cycle.get("opportunities", [])

    log.info("signal_scan_complete",
             component="run_scan_cycle",
             markets_scanned=len(markets),
             opportunities_found=len(all_opps))

    # Log top opportunities for visibility even in dry-run
    for i, opp in enumerate(all_opps[:5]):
        log.info("top_opportunity",
                 rank=i + 1,
                 strategy=opp.strategy,
                 question=opp.market_question[:60],
                 edge=opp.edge,
                 probability=opp.win_probability,
                 score=opp.score,
                 days_left=round(opp.time_to_resolution_sec / 86400, 1))

    # ── State + Execution ─────────────────────────────────────────────────
    from engines.state_engine import StateEngine
    from engines.execution_engine import ExecutionEngine

    state_engine = StateEngine(
        db_path      = "data/trades.db",
        lessons_path = "data/lessons.json",
        initial_balance = capital,
    )
    exec_engine = ExecutionEngine(
        state_engine = state_engine,
        data_engine  = data_engine,
        config       = config,
        dry_run      = dry_run,
    )

    # Daily loss circuit breaker
    daily_pnl      = state_engine.get_daily_pnl()
    max_daily_loss = capital * float(risk.get("max_daily_loss_pct", 0.05))
    if daily_pnl < -max_daily_loss:
        log.warning("daily_loss_limit_hit",
                    daily_pnl=daily_pnl, max_loss=max_daily_loss)
        commit_data()
        sys.exit(0)

    # Open position limit
    open_count = state_engine.get_open_position_count()
    max_open   = int(risk.get("max_open_positions", 5))
    if open_count >= max_open:
        log.info("max_open_positions_reached", open=open_count, max=max_open)
        commit_data()
        sys.exit(0)

    # Execute top opportunities
    trades_executed = 0
    current_balance = state_engine.get_current_balance()
    strategy_map    = {s.name: s for s in signal_engine.strategies}

    for opp in all_opps[:max_per]:
        if trades_executed >= max_per:
            break
        if open_count + trades_executed >= max_open:
            break

        strategy = strategy_map.get(opp.strategy)
        if not strategy:
            continue

        market_state = data_engine.get_single_market(
            opp.metadata.get("token_id", "")
        )

        trade_id = exec_engine.execute_opportunity(
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

    # ── Resolutions ───────────────────────────────────────────────────────
    resolved_count = 0
    for pos in state_engine.get_open_positions():
        try:
            if exec_engine.check_and_settle(pos):
                resolved_count += 1
        except Exception as e:
            log.warning("resolution_check_failed",
                        market_id=pos.get("market_id"), error=str(e))

    # ── Review ────────────────────────────────────────────────────────────
    if resolved_count > 0:
        try:
            from engines.review_engine import ReviewEngine
            ReviewEngine(state_engine, config).run_after_resolution()
        except Exception as e:
            log.warning("review_engine_failed", error=str(e))

    # ── Monitor ───────────────────────────────────────────────────────────
    try:
        from engines.monitor_engine import MonitorEngine
        MonitorEngine(config).send_scan_summary(
            markets_scanned = len(markets),
            opportunities   = len(all_opps),
            trades_executed = trades_executed,
            resolved        = resolved_count,
            balance         = state_engine.get_current_balance(),
            daily_pnl       = state_engine.get_daily_pnl(),
            elapsed_sec     = round(time.time() - start, 1),
            dry_run         = dry_run,
        )
    except Exception as e:
        log.warning("monitor_engine_failed", error=str(e))

    # ── Commit ────────────────────────────────────────────────────────────
    commit_data()

    log.info("scan_cycle_complete",
             component="run_scan_cycle",
             dry_run=dry_run,
             elapsed_sec=round(time.time() - start, 1),
             markets=len(markets),
             opportunities=len(all_opps),
             trades=trades_executed,
             resolved=resolved_count)

    sys.exit(0)


if __name__ == "__main__":
    main()
