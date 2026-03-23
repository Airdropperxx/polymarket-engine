"""
scripts/run_scan_cycle.py
v4: pass groups to observer.detect_signals; wire spread_widening hint;
    always collect data even at position cap.
"""
import os, sys, time, subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import yaml, structlog
log = structlog.get_logger(component="run_scan_cycle")

def load_config():
    config_dir = Path("configs")
    with open(config_dir / "engine.yaml") as f:
        cfg = yaml.safe_load(f)
    for p in sorted(config_dir.glob("s*.yaml")):
        try:
            with open(p) as f:
                d = yaml.safe_load(f)
            if isinstance(d, dict):
                cfg.update(d)
        except Exception as e:
            log.warning("config_load_failed", file=p.name, error=str(e))
    return cfg

def commit_data():
    try:
        subprocess.run(["git","config","user.email","engine@polymarket-bot"], capture_output=True)
        subprocess.run(["git","config","user.name","Polymarket Engine"], capture_output=True)
        subprocess.run(["git","add","data/"], capture_output=True)
        s = subprocess.run(["git","diff","--cached","--quiet"], capture_output=True)
        if s.returncode != 0:
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            subprocess.run(["git","commit","-m",f"scan: {ts} [automated]"], capture_output=True)
            subprocess.run(["git","push"], capture_output=True)
            log.info("data_committed")
    except Exception as e:
        log.warning("git_commit_failed", error=str(e))


def build_observer_hints(signals: list) -> dict:
    """Convert raw observer signals into a typed hints dict for strategies."""
    hints: dict = {}
    for sig in signals:
        sig_type = sig.get("type", "")
        mid      = sig.get("market_id", "")
        if not sig_type or not mid:
            continue
        hints.setdefault(sig_type, []).append(mid)
    hints = {k: list(dict.fromkeys(v)) for k, v in hints.items()}
    total = sum(len(v) for v in hints.values())
    log.info("observer_hints_built", types=list(hints.keys()), total_hinted_markets=total)
    return hints


def select_top_trades(all_opps, max_per: int, s1_slots: int) -> list:
    """
    Select best trades for execution:
    - S1 zero-risk arb fills its reserved slots first (sorted by edge DESC)
    - Remaining slots go to highest-edge opportunities from other strategies
    """
    s1_opps    = sorted([o for o in all_opps if o.strategy == "s1_negrisk_arb"],
                        key=lambda o: o.edge, reverse=True)
    other_opps = sorted([o for o in all_opps if o.strategy != "s1_negrisk_arb"],
                        key=lambda o: o.edge, reverse=True)
    selected  = s1_opps[:s1_slots]
    selected += other_opps[:max_per - len(selected)]
    return selected


def main():
    start   = time.time()
    dry_run = os.environ.get("DRY_RUN", "true").lower() == "true"
    log.info("scan_cycle_start", dry_run=dry_run)

    config   = load_config()
    engine   = config.get("engine", {})
    capital  = float(engine.get("capital_usdc", 100.0))
    risk     = engine.get("risk", {})
    max_per  = int(engine.get("max_per_cycle", 25))
    s1_slots = int(engine.get("s1_reserved_slots", 8))

    # ── Data ──────────────────────────────────────────────────────────────
    from engines.data_engine import DataEngine
    data_engine = DataEngine(config)
    markets     = data_engine.fetch_all_markets()
    groups      = data_engine.fetch_negrisk_groups()
    log.info("markets_fetched", total=len(markets), negrisk_groups=len(groups))

    if not markets:
        markets = data_engine.get_cached_markets()
    if not markets:
        log.error("no_markets_available")
        commit_data(); sys.exit(0)

    # ── Observer: ALWAYS runs unconditionally ─────────────────────────────
    from engines.market_observer import MarketObserver
    observer  = MarketObserver(config)
    observer.observe(markets)
    # Pass groups so negrisk_imbalance signals can be detected
    signals   = observer.detect_signals(markets, groups=groups)
    obs_stats = observer.get_stats()

    log.info("observer_stats",
             markets_tracked=obs_stats["markets_tracked"],
             total_data_points=obs_stats["total_data_points"],
             signals_found=len(signals))

    for s in signals[:5]:
        log.info("signal_detected",
                 type=s["type"], category=s["category"],
                 strength=round(s["strength"], 3),
                 yes_price=s.get("yes_price"),
                 question=s.get("question","")[:70])

    observer_hints = build_observer_hints(signals)

    # ── Strategy scan: ALWAYS runs unconditionally ────────────────────────
    from engines.signal_engine import SignalEngine
    from strategies.s10_near_resolution import S10NearResolution
    from strategies.s1_negrisk_arb import S1NegRiskArb
    from strategies.s8_logical_arb import S8LogicalArb
    from strategies.s11_inplay_momentum import S11InplayMomentum

    signal_engine = SignalEngine(config)
    signal_engine.register(S10NearResolution())
    signal_engine.register(S1NegRiskArb())
    signal_engine.register(S8LogicalArb())
    signal_engine.register(S11InplayMomentum())

    cycle    = signal_engine.run_one_cycle(markets, groups, observer_hints=observer_hints)
    all_opps = cycle.get("opportunities", [])
    log.info("signal_scan_complete",
             markets_scanned=len(markets),
             opportunities_found=len(all_opps),
             s1=sum(1 for o in all_opps if o.strategy=="s1_negrisk_arb"),
             s10=sum(1 for o in all_opps if o.strategy=="s10_near_resolution"),
             s11=sum(1 for o in all_opps if o.strategy=="s11_inplay_momentum"))

    for i, opp in enumerate(all_opps[:10]):
        log.info("opportunity",
                 rank=i+1, strategy=opp.strategy,
                 question=opp.market_question[:80],
                 edge=round(opp.edge,4), prob=round(opp.win_probability,3),
                 score=round(opp.score,3),
                 days=round(opp.time_to_resolution_sec/86400,1),
                 category=opp.metadata.get("category","?"),
                 vol=opp.metadata.get("volume_24h",0))

    # ── Execution ─────────────────────────────────────────────────────────
    from engines.state_engine import StateEngine
    from engines.execution_engine import ExecutionEngine

    state_engine = StateEngine("data/trades.db", "data/lessons.json", capital)
    exec_engine  = ExecutionEngine(state_engine, data_engine, config, dry_run)

    # Daily loss guard
    daily_pnl      = state_engine.get_daily_pnl()
    max_daily_loss = capital * float(risk.get("max_daily_loss_pct", 0.99))
    if daily_pnl < -max_daily_loss:
        log.warning("daily_loss_limit", daily_pnl=daily_pnl)
        for opp in all_opps:
            exec_engine.log_opportunity(opp, False, "daily_loss_limit")
        _log_signals(exec_engine, signals, signal_engine, observer_hints)
        commit_data(); sys.exit(0)

    # Position cap check — does NOT exit, just skips execution
    open_count  = state_engine.get_open_position_count()
    max_open    = int(risk.get("max_open_positions", 500))
    can_execute = open_count < max_open
    if not can_execute:
        log.info("at_position_cap", open=open_count, max=max_open,
                 note="logging opps and resolving positions, skipping new execution")

    # Select top trades (S1 priority + highest-edge others)
    to_execute = select_top_trades(all_opps, max_per, s1_slots) if can_execute else []
    to_log_only = [o for o in all_opps if o not in to_execute]

    strategy_map    = {s.name: s for s in signal_engine.strategies}
    trades_executed = 0
    local_open      = open_count

    for opp in to_execute:
        if trades_executed >= max_per:
            break
        if local_open >= max_open:
            exec_engine.log_opportunity(opp, False, "max_positions_mid_cycle")
            continue
        strategy = strategy_map.get(opp.strategy)
        if not strategy:
            continue
        mstate   = data_engine.get_single_market(opp.metadata.get("token_id",""))
        trade_id = exec_engine.execute_opportunity(opp, strategy, mstate, capital)
        if trade_id:
            trades_executed += 1
            local_open      += 1

    for opp in to_log_only:
        reason = "beyond_max_per_cycle" if can_execute else "at_position_cap"
        exec_engine.log_opportunity(opp, False, reason)

    log.info("execution_summary",
             trades_executed=trades_executed,
             at_cap=not can_execute,
             open_positions=local_open)

    # Log observer signals (always)
    _log_signals(exec_engine, signals, signal_engine, observer_hints)

    # ── Resolutions: ALWAYS runs ───────────────────────────────────────────
    resolved_count = 0
    for pos in state_engine.get_open_positions():
        try:
            if exec_engine.check_and_settle(pos):
                resolved_count += 1
        except Exception as e:
            log.warning("settle_failed", error=str(e))

    if resolved_count > 0:
        log.info("positions_resolved", count=resolved_count)
        try:
            from engines.review_engine import ReviewEngine
            ReviewEngine(state_engine, config).run_after_resolution()
        except Exception as e:
            log.warning("review_failed", error=str(e))

    try:
        from engines.monitor_engine import MonitorEngine
        MonitorEngine(config).send_scan_summary(
            markets_scanned=len(markets), opportunities=len(all_opps),
            trades_executed=trades_executed, resolved=resolved_count,
            balance=state_engine.get_current_balance(),
            daily_pnl=state_engine.get_daily_pnl(),
            elapsed_sec=round(time.time()-start,1), dry_run=dry_run,
        )
    except Exception as e:
        log.warning("monitor_failed", error=str(e))

    commit_data()
    log.info("scan_cycle_complete",
             dry_run=dry_run, elapsed_sec=round(time.time()-start,1),
             markets=len(markets), opportunities=len(all_opps),
             trades=trades_executed, resolved=resolved_count,
             open_positions=local_open,
             observer_points=obs_stats["total_data_points"],
             observer_signals=len(signals),
             signal_types=list(observer_hints.keys()))
    sys.exit(0)


def _log_signals(exec_engine, signals, signal_engine, observer_hints):
    for sig in signals[:20]:
        exec_engine.log_opportunity(
            type("Opp", (), {
                "strategy": f"observer_{sig['type']}",
                "market_id": sig["market_id"],
                "market_question": sig.get("question",""),
                "action": "SIGNAL",
                "edge": sig.get("strength", 0),
                "win_probability": sig.get("yes_price", 0),
                "score": sig.get("strength", 0),
                "time_to_resolution_sec": 0,
                "metadata": {
                    "category":    sig.get("category",""),
                    "volume_24h":  sig.get("volume", 0),
                    "buy_price":   sig.get("yes_price", 0),
                    "fee": 0, "spread": 0, "fee_rate_bps": 0,
                    "num_legs": 1, "total_ask": 0,
                    "signal_type": sig["type"],
                    "signal_note": sig.get("note",""),
                    "hinted_to_strategies": [
                        s.name for s in signal_engine.strategies
                        if sig["market_id"] in observer_hints.get(sig["type"], [])
                    ],
                }
            })(), False, f"observer_signal_{sig['type']}"
        )


if __name__ == "__main__":
    main()
