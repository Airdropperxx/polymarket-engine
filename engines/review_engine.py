"""
engines/review_engine.py -- AI learning loop. Runs after every resolution.
FIXES(v_fix1): prompt has full P&L math, win_rate, ROI, edge/EV accuracy.
"""
from __future__ import annotations
import json, os, re
from datetime import datetime, timezone
import structlog
from engines.state_engine import StateEngine
from engines.trade_analytics import compute_portfolio_stats, format_trade_summary
log = structlog.get_logger(component="review_engine")

SYSTEM_PROMPT = (
    "You are a trading strategy reviewer for a Polymarket prediction market engine.\n"
    "Review resolved trades with full P&L math, output ONLY valid JSON:\n"
    "{\n"
    '  \"strategy_score_updates\": {\n'
    '    \"s10_near_resolution\": {\"allocation_delta\": 0.0, \"notes\": \"1-sentence reason\"},\n'
    '    \"s1_negrisk_arb\":      {\"allocation_delta\": 0.0, \"notes\": \"1-sentence reason\"},\n'
    '    \"s11_inplay_momentum\": {\"allocation_delta\": 0.0, \"notes\": \"1-sentence reason\"},\n'
    '    \"s8_logical_arb\":      {\"allocation_delta\": 0.0, \"notes\": \"1-sentence reason\"}\n'
    "  },\n"
    '  \"new_lessons\": [\"Specific lesson grounded in data with actual numbers\"],\n'
    '  \"deprecated_lesson_indices\": [],\n'
    '  \"summary\": \"2-sentence summary\"\n'
    "}\n"
    "Rules:\n"
    "- allocation_delta in [-0.15,+0.15] only if 3+ resolved trades justify\n"
    "- win_rate<40% on 5+ trades -> reduce -0.05 to -0.10\n"
    "- roi_pct>10% on 3+ trades -> increase +0.05\n"
    "- edge_accuracy<50% means prob model wrong\n"
    "- Lessons MUST cite specific numbers (prices, edges, win rates)"
)

class ReviewEngine:
    def __init__(self, state: StateEngine, config: dict):
        self.state = state; self.config = config

    def run_after_resolution(self) -> dict:
        try:
            api_key = os.environ.get("ANTHROPIC_API_KEY","")
            if not api_key: return {"status":"skipped"}
            import anthropic
            client   = anthropic.Anthropic(api_key=api_key)
            recent   = self.state.get_recent_resolved_trades(hours=72)
            all_t    = self.state.get_all_trades()
            lessons  = self.state.get_lessons()
            balance  = self.state.get_current_balance()
            daily    = self.state.get_daily_pnl()
            if not recent: return {"status":"skipped"}
            all_s  = compute_portfolio_stats(all_t)
            rec_s  = compute_portfolio_stats(recent)
            def _edge(t):
                for p in (t.get("notes","") or "").split():
                    if p.startswith("edge="):
                        try: return float(p.split("=")[1])
                        except: pass
                return 0.0
            def _ev(t):
                for p in (t.get("notes","") or "").split():
                    if p.startswith("ev="):
                        try: return float(p.split("=")[1])
                        except: pass
                return 0.0
            pe=[t for t in recent if _edge(t)>0]
            ea=(round(sum(1 for t in pe if t.get("outcome")=="win")/len(pe)*100,1) if pe else None)
            pv=[t for t in recent if _ev(t)>0]
            eva=(round(sum(1 for t in pv if t.get("outcome")=="win")/len(pv)*100,1) if pv else None)
            prompt = _build_prompt(recent,rec_s,all_s,lessons,balance,daily,ea,eva)
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=1200,
                system=SYSTEM_PROMPT, messages=[{"role":"user","content":prompt}])
            raw = resp.content[0].text.strip()
            upd = self._parse(raw)
            if not upd: return {"status":"parse_failed"}
            self._apply(lessons, upd)
            self.state.save_lessons(lessons)
            log.info("review_complete",new=len(upd.get("new_lessons",[])),
                     summary=upd.get("summary","")[:80])
            return {"status":"ok","updates":upd}
        except Exception as e:
            log.error("review_failed",error=str(e)); return {"status":"error"}

    def _parse(self, raw):
        if not raw: return None
        try: return json.loads(raw)
        except: pass
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try: return json.loads(m.group())
            except: pass
        return None

    def _apply(self, lessons, upd):
        try:
            scores = lessons.setdefault("strategy_scores",{})
            for s,u in upd.get("strategy_score_updates",{}).items():
                delta=float(u.get("allocation_delta",0.0))
                sv=scores.get(s,{})
                cur=float(sv.get("score",1.0) if isinstance(sv,dict) else sv)
                new_s=round(max(0.1,min(2.0,cur+delta)),3)
                scores[s]={"score":new_s,"last_delta":delta,"notes":u.get("notes",""),
                            "updated_at":datetime.now(timezone.utc).isoformat()}
                if delta!=0: log.info("score_updated",s=s,old=cur,new=new_s)
            existing=lessons.setdefault("lessons",[])
            for lsn in upd.get("new_lessons",[]):
                if lsn and len(lsn)>10:
                    existing.append({"text":lsn,"created_at":datetime.now(timezone.utc).isoformat()})
            for i in sorted(upd.get("deprecated_lesson_indices",[]),reverse=True):
                if isinstance(i,int) and 0<=i<len(existing): existing.pop(i)
            if len(existing)>30: lessons["lessons"]=existing[-30:]
            lessons["last_updated"]=datetime.now(timezone.utc).isoformat()
        except Exception as e: log.error("apply_failed",error=str(e))


def _build_prompt(recent,rec_s,all_s,lessons,balance,daily,ea,eva):
    lines=["=== RECENTLY RESOLVED TRADES (72h) ==="]
    for t in recent: lines.append(format_trade_summary(t))
    rr=rec_s
    lines+=["","=== RECENT PERFORMANCE (72h) ===",
            "Resolved:{} Wins:{} Losses:{} WinRate:{}%".format(
                rr["resolved_trades"],rr["wins"],rr["losses"],rr["win_rate_pct"]),
            "Invested:${:.2f} PnL:${:+.4f} ROI:{:+.2f}%".format(
                rr["total_invested"],rr["total_pnl"],rr["total_roi_pct"]),
            "Best:${:+.4f} Worst:${:+.4f}".format(rr["best_trade_pnl"],rr["worst_trade_pnl"])]
    if ea  is not None: lines.append("Edge Accuracy: {:.1f}%".format(ea))
    if eva is not None: lines.append("EV Accuracy: {:.1f}%".format(eva))
    aa=all_s
    lines+=["","=== ALL-TIME PORTFOLIO ===",
            "Total:{} Resolved:{} Open:{} WR:{}%".format(
                aa["total_trades"],aa["resolved_trades"],aa["open_trades"],aa["win_rate_pct"]),
            "Balance:${:.2f} DailyPnL:${:+.4f} TotalROI:{:+.2f}% Exp:${:.2f}".format(
                balance,daily,aa["total_roi_pct"],aa["open_exposure"]),
            "","=== PER-STRATEGY ==="]
    for s,v in aa["by_strategy"].items():
        lines.append("  {}: {}t WR:{}% PnL:${:+.4f} ROI:{:+.2f}%".format(
            s,v["trades"],v["win_rate_pct"],v["total_pnl"],v["roi_pct"]))
    lines+=["","=== CURRENT LESSONS ==="]
    existing=lessons.get("lessons",[])
    for i,lsn in enumerate(existing):
        lines.append("  [{}] {}".format(i,lsn.get("text",lsn) if isinstance(lsn,dict) else lsn))
    if not existing: lines.append("  (none yet)")
    lines+=["","=== STRATEGY SCORES ==="]
    for s,v in lessons.get("strategy_scores",{}).items():
        sc=v.get("score",1.0) if isinstance(v,dict) else v
        lines.append("  {}: {}".format(s,sc))
    lines.append("\nProvide JSON review.")
    return "\n".join(lines)