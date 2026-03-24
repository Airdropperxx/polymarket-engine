"""
engines/monitor_engine.py -- Telegram alerts. NEVER places orders.

FIXES (v_fix1):
  - send_scan_summary includes win_rate, roi, total_pnl from trade_stats
  - Added send_resolution_alert() for individual trade settlements
  - Added send_portfolio_summary() for full analytics snapshot
"""
from __future__ import annotations
import os
import requests
import structlog

log = structlog.get_logger(component="monitor_engine")


class MonitorEngine:
    def __init__(self, config: dict):
        self.config    = config
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id   = os.environ.get("TELEGRAM_CHAT_ID", "")

    def _send(self, text: str) -> None:
        if not self.bot_token or not self.chat_id:
            log.debug("telegram_not_configured"); return
        try:
            url  = "https://api.telegram.org/bot{}/sendMessage".format(self.bot_token)
            resp = requests.post(url, json={"chat_id":self.chat_id,"text":text,"parse_mode":"HTML"}, timeout=10)
            if not resp.ok: log.warning("telegram_send_failed", status=resp.status_code)
        except Exception as e:
            log.warning("telegram_error", error=str(e))

    def send_scan_summary(self, markets_scanned, opportunities, trades_executed,
                          resolved, balance, daily_pnl, elapsed_sec, dry_run,
                          trade_stats=None) -> None:
        sign  = "+" if daily_pnl >= 0 else ""
        lines = [
            "{} <b>Scan Complete</b>".format("\U0001f535" if dry_run else "\U0001f7e2"),
            "Markets:{} Opps:{} Trades:{} Resolved:{}".format(markets_scanned,opportunities,trades_executed,resolved),
            "Balance: <b>${:.2f}</b>  Daily PnL: <b>{}{:.4f}</b>".format(balance,sign,daily_pnl),
        ]
        if trade_stats:
            wr=trade_stats.get("win_rate_pct",0); tot=trade_stats.get("resolved",0)
            roi=trade_stats.get("roi_pct",0); tpnl=trade_stats.get("total_pnl",0)
            if tot>0:
                lines.append("All-time: {} resolved  WR:{}%  ROI:{:+.2f}%  PnL:{:+.4f}".format(tot,wr,roi,tpnl))
        lines.append("{:.1f}s".format(elapsed_sec))
        self._send("\n".join(lines))

    def send_resolution_alert(self, trade_id, strategy, question, outcome,
                               pnl_usdc, roi_pct, cost_usdc, shares, fee_usdc) -> None:
        emoji = "\u2705" if outcome=="win" else "\u274C"
        sign  = "+" if pnl_usdc>=0 else ""
        lines = [
            "{} <b>Trade Resolved: {}</b>".format(emoji,outcome.upper()),
            "Strategy: {}".format(strategy),
            "Q: {}".format(question[:70]),
            "Shares:{:.4f}  Cost:${:.2f}  Fee:${:.4f}".format(shares,cost_usdc,fee_usdc),
            "PnL: <b>{}{:.4f} USDC</b>  ({}{:.1f}% ROI)".format(sign,pnl_usdc,sign,roi_pct),
            "<i>{}</i>".format(trade_id),
        ]
        self._send("\n".join(lines))

    def send_portfolio_summary(self, balance, daily_pnl, stats) -> None:
        sign  = "+" if daily_pnl>=0 else ""
        lines = [
            "\U0001f4ca <b>Portfolio Update</b>",
            "Balance: <b>${:.2f}</b>  Daily PnL: {}{:.4f}".format(balance,sign,daily_pnl),
            "Resolved:{}  Open:{}  Exposure:${:.2f}".format(stats.get("resolved_trades",0),stats.get("open_trades",0),stats.get("open_exposure",0)),
            "WinRate: <b>{:.1f}%</b>  ROI:{:+.2f}%  PnL:{:+.4f}".format(stats.get("win_rate_pct",0),stats.get("total_roi_pct",0),stats.get("total_pnl",0)),
            "Best:{:+.4f}  Worst:{:+.4f}".format(stats.get("best_trade_pnl",0),stats.get("worst_trade_pnl",0)),
        ]
        by_s = stats.get("by_strategy",{})
        if by_s:
            lines.append("\n<b>By Strategy:</b>")
            for s,sv in by_s.items():
                lines.append("  {}: {}t  WR:{}%  PnL:{:+.4f}  ROI:{:+.2f}%".format(s,sv["trades"],sv["win_rate_pct"],sv["pnl"],sv["roi_pct"]))
        self._send("\n".join(lines))

    def send_error(self, component: str, error: str) -> None:
        self._send("\U0001f6a8 ERROR [{}]: {}".format(component,error[:200]))

    # backwards compat
    def send_daily_summary(self, *a, **kw): self.send_scan_summary(*a, **kw)