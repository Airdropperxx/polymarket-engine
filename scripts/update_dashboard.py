#!/usr/bin/env python3
"""
scripts/update_dashboard.py
FIXES(v_fix1): real stats from get_trade_stats(), no hardcoded zeros.
Full trade list for analytics tab. Per-trade edge/ev from notes.
"""
import html as _html, json, os, re, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv; load_dotenv()
import structlog
log = structlog.get_logger(component="update_dashboard")

def esc(t): return "" if t is None else _html.escape(str(t))

def parse_scan_log(path="data/scan_log.json"):
    out={"total_opportunities":0,"trades_executed":0,"trades_rejected":0,"by_strategy":{}}
    try:
        entries=json.loads(Path(path).read_text()) if Path(path).exists() else []
        out["total_opportunities"]=len(entries)
        for e in entries:
            s=e.get("strategy","unknown")
            if s not in out["by_strategy"]:
                out["by_strategy"][s]={"total":0,"executed":0,"rejected":0}
            out["by_strategy"][s]["total"]+=1
            if e.get("executed"):
                out["trades_executed"]+=1; out["by_strategy"][s]["executed"]+=1
            elif e.get("reason_skipped"):
                out["trades_rejected"]+=1; out["by_strategy"][s]["rejected"]+=1
    except: pass
    return out

def _load_scan_log(limit: int = 500) -> list:
    """Load last N entries from scan_log.json."""
    try:
        p = Path("data/scan_log.json")
        if not p.exists(): return []
        entries = json.loads(p.read_text())
        return entries[-limit:] if len(entries) > limit else entries
    except Exception: return []

def _load_json_file(path: str) -> dict:
    """Load a JSON file, return {} on missing/error."""
    try:
        p = Path(path)
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception: return {}

def get_engine_state():
    from engines.state_engine import StateEngine
    db      = os.environ.get("DATABASE_PATH","data/trades.db")
    lpath   = os.environ.get("LESSONS_PATH","data/lessons.json")
    if not os.path.exists(db): return None
    state   = StateEngine(db,lpath)
    open_p  = state.get_open_positions()
    ts      = state.get_trade_stats()
    recent  = state.get_recent_resolved_trades(hours=48)
    all_t   = state.get_all_trades()  # all trades for complete P&L analytics
    scan    = parse_scan_log()
    lessons = state.get_lessons()
    wins24  = sum(1 for t in recent if t.get("outcome")=="win")
    losses24= sum(1 for t in recent if t.get("outcome")=="loss")
    trades_display=[]
    for t in all_t:
        cost=float(t.get("cost_usdc",0) or 0)
        pnl=float(t.get("pnl_usdc",0) or 0)
        roi=round(pnl/cost*100,2) if (cost>0 and t.get("status")=="resolved") else None
        notes=t.get("notes","") or ""
        edge=ev=None
        for part in notes.split():
            if part.startswith("edge="):
                try: edge=float(part.split("=")[1])
                except: pass
            if part.startswith("ev="):
                try: ev=float(part.split("=")[1])
                except: pass
        trades_display.append({
            "time":t.get("entry_time",""),"res_time":t.get("resolution_time",""),
            "trade_id":t.get("trade_id",""),"strategy":esc(t.get("strategy","")),
            "market":esc(t.get("market_question",""))[:75],"market_id":t.get("market_id",""),
            "side":esc(t.get("side","")),"price":float(t.get("price",0) or 0),
            "shares":float(t.get("shares",0) or 0),"size":cost,
            "fee":float(t.get("fee_usdc",0) or 0),"status":t.get("status","open"),
            "outcome":esc(t.get("outcome","") or ""),"pnl":pnl,"roi":roi,
            "edge":edge,"ev":ev,"notes":esc(notes[:100])
        })
    dry=os.environ.get("DRY_RUN","false").lower()=="true"
    return {
        "timestamp":datetime.now(timezone.utc).isoformat()+"Z",
        "balance":float(state.get_current_balance()),
        "daily_pnl":float(state.get_daily_pnl()),
        "open_positions":len(open_p),
        "open_exposure":round(sum(float(p.get("cost_usdc",0) or 0) for p in open_p),2),
        "dry_run":dry,
        "total_trades":ts["total"],"resolved_trades":ts["resolved"],
        "wins_all_time":ts["wins"],"losses_all_time":ts["losses"],
        "win_rate_pct":ts["win_rate_pct"],"total_pnl":ts["total_pnl"],
        "total_invested":ts["invested"],"roi_pct":ts["roi_pct"],
        "best_trade_pnl":ts["best_pnl"],"worst_trade_pnl":ts["worst_pnl"],
        "by_strategy":ts["by_strategy"],
        "resolved_24h":len(recent),"wins_24h":wins24,"losses_24h":losses24,
        "trades_executed":scan["trades_executed"],"trades_rejected":scan["trades_rejected"],
        "total_opportunities":scan["total_opportunities"],"scan_by_strategy":scan["by_strategy"],
        "recent_trades":trades_display,
        "lessons_count":len(lessons.get("lessons",[])),"strategy_scores":lessons.get("strategy_scores",{}),"lessons_list":lessons.get("lessons",[]),
        "lessons_updated":lessons.get("last_updated",""),
        # Scan History tab — last 500 scan_log entries
        "scan_log":_load_scan_log(500),
        # CLOB enriched opportunities
        "enriched_opps":_load_json_file("data/enriched_opportunities.json"),
    }

def update_dashboard_html(data):
    # Writes engine-data JSON block into dashboard.html.
    # Uses ensure_ascii=False + str.split to avoid re.sub unicode escape errors.
    for dp in ["dashboard.html", "index.html"]:
        if os.path.exists(dp):
            break
    else:
        return False
    try:
        c = Path(dp).read_text(encoding="utf-8")
        # ensure_ascii=False: keeps UTF-8 chars as-is (é stays é, not \u00e9)
        # This eliminates \uXXXX sequences from the JSON output entirely.
        jstr = json.dumps(data, indent=2, ensure_ascii=False)
        new_block = '<script id="engine-data" type="application/json">\n' + jstr + '\n</script>'

        # Use string split — immune to regex escape issues in replacement content.
        # Find the opening tag by splitting on it, never by regex substitution.
        OPEN_TAG  = '<script id="engine-data"'
        CLOSE_TAG = '</script>'

        if OPEN_TAG in c:
            # Find the block and replace it entirely
            before = c[:c.index(OPEN_TAG)]
            after_start = c.index(OPEN_TAG) + len(OPEN_TAG)
            # Find the closing </script> after the opening tag
            close_pos = c.find(CLOSE_TAG, after_start)
            if close_pos == -1:
                # Malformed — append instead
                c = c.replace("</body>", new_block + "\n</body>")
            else:
                after = c[close_pos + len(CLOSE_TAG):]
                c = before + new_block + after
        else:
            # First time — inject before </body>
            c = c.replace("</body>", new_block + "\n</body>")

        Path(dp).write_text(c, encoding="utf-8")
        return True
    except Exception as e:
        log.error("update_html_failed", error=str(e))
        return False
def main():
    data=get_engine_state()
    if not data: print("No database, skipping."); return
    update_dashboard_html(data)
    try:
        Path("data").mkdir(exist_ok=True)
        Path("data/dashboard_state.json").write_text(json.dumps(data,indent=2))
    except: pass
    print("Dashboard updated.")

if __name__=="__main__": main()