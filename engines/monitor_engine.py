"""
engines/monitor_engine.py — Telegram alerting. Fire-and-forget. Never raises.
"""

from __future__ import annotations
import os
import structlog
import requests

log = structlog.get_logger(component="monitor_engine")


class MonitorEngine:
    def __init__(self, config: dict):
        self.token    = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id  = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self._enabled = bool(self.token and self.chat_id)
        self.enabled  = self._enabled   # legacy alias

    def _send(self, text: str) -> None:
        """Fire-and-forget POST. Never raises."""
        if not self._enabled:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text[:4096]},
                timeout=5,
            )
        except Exception as e:
            log.debug("telegram_send_failed", error=str(e))

    def send(self, event_type: str, **kwargs) -> None:
        """Unified send interface expected by tests."""
        if not self._enabled:
            return
        try:
            if event_type == "trade_executed":
                msg = (f"TRADE {kwargs.get('strategy','')} | {kwargs.get('action','')}"
                       f"\nQ: {str(kwargs.get('question',''))[:80]}"
                       f"\nSize: {kwargs.get('size',0)} @ {kwargs.get('price',0)}")
            elif event_type == "trade_rejected":
                msg = f"REJECTED {kwargs.get('strategy','')} -- {kwargs.get('reason','')}"
            elif event_type == "daily_summary":
                pnl = kwargs.get('pnl', 0)
                msg = f"Daily P&L: {pnl:+.2f} | Trades: {kwargs.get('trades',0)} | Balance: {kwargs.get('balance',0):.2f}"
            elif event_type == "risk_limit_hit":
                msg = f"RISK LIMIT: {kwargs.get('limit_type','')}"
            elif event_type == "error":
                msg = f"ERROR [{kwargs.get('component','')}]: {str(kwargs.get('error',''))[:150]}"
            elif event_type == "lesson_update":
                msg = f"LESSON: {str(kwargs.get('lesson',''))[:200]}"
            else:
                msg = f"[{event_type}] {kwargs}"
            self._send(msg)
        except Exception as e:
            log.debug("monitor_format_failed", event=event_type, error=str(e))

    # ── Legacy compatibility ─────────────────────────────────────────

    def send_scan_summary(self, markets_scanned: int, opportunities: int,
                          trades_executed: int, resolved: int,
                          balance: float, daily_pnl: float,
                          elapsed_sec: float, dry_run: bool) -> None:
        mode = "DRY" if dry_run else "LIVE"
        self._send(
            f"[{mode}] Scan {elapsed_sec:.1f}s | Markets:{markets_scanned}"
            f" Opps:{opportunities} Trades:{trades_executed} Resolved:{resolved}"
            f" | Bal:${balance:.2f} PnL:{daily_pnl:+.2f}"
        )

    def send_error(self, component: str, error: str) -> None:
        self._send(f"ERROR [{component}]: {error[:150]}")
