"""
scripts/check_resolutions.py
Checks open positions for resolution hourly. FIXES(v_fix1):
  - Gamma API via execution_engine (no CLOB token_id needed for dry trades)
  - Triggers ReviewEngine after any resolution
  - Logs full P&L math per settled trade
  - Sends Telegram alerts
"""
import os, sys, subprocess, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import yaml, structlog
log = structlog.get_logger(component="check_resolutions")

def load_config():
    d = Path("configs"); cfg = yaml.safe_load(open(d/"engine.yaml"))
    for p in sorted(d.glob("s*.yaml")):
        try:
            v = yaml.safe_load(open(p))
            if isinstance(v,dict): cfg.update(v)
        except: pass
    return cfg

def commit_data():
    try:
        subprocess.run(["git","config","user.email","engine@polymarket-bot"],capture_output=True)
        subprocess.run(["git","config","user.name","Polymarket Engine"],capture_output=True)
        subprocess.run(["git","add","data/","index.html","dashboard.html"],capture_output=True)
        s=subprocess.run(["git","diff","--cached","--quiet"],capture_output=True)
        if s.returncode!=0:
            ts=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
            subprocess.run(["git","commit","-m","resolve: {} [automated]".format(ts)],capture_output=True)
            subprocess.run(["git","push"],capture_output=True)
            log.info("data_committed")
    except Exception as e: log.warning("git_commit_failed",error=str(e))

def main():
    start  = time.time()
    config = load_config()
    db_path      = os.environ.get("DATABASE_PATH","data/trades.db")
    lessons_path = os.environ.get("LESSONS_PATH","data/lessons.json")
    capital      = float(config.get("engine",{}).get("capital_usdc",100.0))
    from engines.state_engine     import StateEngine
    from engines.data_engine      import DataEngine
    from engines.execution_engine import ExecutionEngine
    from engines.trade_analytics  import compute_portfolio_stats
    state_engine = StateEngine(db_path,lessons_path,capital)
    data_engine  = DataEngine(config.get("engine",{}))
    exec_engine  = ExecutionEngine(
        state_engine=state_engine, data_engine=data_engine,
        config=config, dry_run=True)
    open_positions = state_engine.get_open_positions()
    log.info("checking_resolutions",open_positions=len(open_positions))
    if not open_positions:
        log.info("no_open_positions"); sys.exit(0)
    resolved_count=0; resolution_detail=[]; skip_count=0; error_count=0
    for pos in open_positions:
        trade_id   = pos.get("trade_id","?")
        market_id  = pos.get("market_id","")
        question   = pos.get("market_question","")[:60]
        strategy   = pos.get("strategy","?")
        cost       = float(pos.get("cost_usdc",0) or 0)
        shares     = float(pos.get("shares",0) or 0)
        fee        = float(pos.get("fee_usdc",0) or 0)
        price      = float(pos.get("price",0) or 0)
        side       = pos.get("side","YES")
        entry_time = pos.get("entry_time","")
        notes      = pos.get("notes","") or ""
        is_negrisk = market_id.startswith("0x") or len(market_id) > 20
        has_leg_ids = "leg_ids=" in notes

        # DEBUG: full context before each resolution attempt
        log.info("resolution_attempt",
                 trade_id=trade_id, market_id=market_id[:25],
                 strategy=strategy, side=side, price=price,
                 shares=shares, cost=round(cost,4), fee=round(fee,6),
                 entry_time=entry_time[:19] if entry_time else "",
                 is_negrisk=is_negrisk,
                 has_leg_ids=has_leg_ids,
                 notes_preview=notes[:60])

        try:
            settled = exec_engine.check_and_settle(pos)
        except Exception as ex:
            log.error("resolution_error",
                      trade_id=trade_id, market_id=market_id[:25], error=str(ex))
            error_count += 1
            continue

        if settled:
            resolved_count+=1
            all_t=state_engine.get_all_trades(limit=500)
            upd=next((t for t in all_t if t.get("trade_id")==trade_id),None)
            pnl=float(upd.get("pnl_usdc",0) or 0) if upd else 0.0
            outcome=upd.get("outcome","?") if upd else "?"
            roi=round(pnl/cost*100,2) if cost>0 else 0.0
            resolution_detail.append({"trade_id":trade_id,"strategy":strategy,"question":question,
                "side":side,"price":price,"shares":shares,"cost":cost,"fee":fee,
                "pnl":pnl,"roi":roi,"outcome":outcome})
            log.info("resolution_settled",
                     trade_id=trade_id, outcome=outcome, pnl=round(pnl,6),
                     roi_pct=roi, market_id=market_id[:25])
        else:
            skip_count += 1
            log.debug("resolution_skipped",
                      trade_id=trade_id, market_id=market_id[:25],
                      reason="not_resolved_yet")
    log.info("resolution_cycle_done",
             checked=len(open_positions),
             resolved=resolved_count,
             skipped=skip_count,
             errors=error_count,
             elapsed_sec=round(time.time()-start,1),
             balance=round(state_engine.get_current_balance(),4))
    # Portfolio stats
    all_trades=state_engine.get_all_trades()
    stats=compute_portfolio_stats(all_trades)
    log.info("portfolio",total=stats["total_trades"],resolved=stats["resolved_trades"],
             open=stats["open_trades"],wins=stats["wins"],losses=stats["losses"],
             wr=stats["win_rate_pct"],pnl=stats["total_pnl"],roi=stats["total_roi_pct"])
    # ReviewEngine
    if resolved_count>0:
        try:
            from engines.review_engine import ReviewEngine
            r=ReviewEngine(state_engine,config).run_after_resolution()
            log.info("review_done",status=r.get("status"))
        except Exception as e: log.warning("review_failed",error=str(e))
    # Update dashboard
    try:
        import importlib.util
        spec=importlib.util.spec_from_file_location("ud",Path("scripts/update_dashboard.py"))
        mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); mod.main()
    except Exception as e: log.warning("dashboard_failed",error=str(e))
    # Telegram
    if resolved_count>0:
        try:
            from engines.monitor_engine import MonitorEngine
            mon=MonitorEngine(config)
            for rd in resolution_detail:
                mon.send_resolution_alert(trade_id=rd["trade_id"],strategy=rd["strategy"],
                    question=rd["question"],outcome=rd["outcome"],pnl_usdc=rd["pnl"],
                    roi_pct=rd["roi"],cost_usdc=rd["cost"],shares=rd["shares"],fee_usdc=rd["fee"])
            mon.send_portfolio_summary(balance=state_engine.get_current_balance(),
                daily_pnl=state_engine.get_daily_pnl(),stats=stats)
        except Exception as e: log.warning("telegram_failed",error=str(e))
    commit_data(); sys.exit(0)

if __name__=="__main__": main()