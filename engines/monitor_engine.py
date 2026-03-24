"""
engines/monitor_engine.py — Telegram alerts via raw HTTP (no library needed).
NEVER raises. Silent if token missing. Messages kept short.
"""

from __future__ import annotations

import os
import requests
import structlog

log = structlog.get_logger(component="monitor_engine")


class MonitorEngine:
    def __init__(self, config: dict):
        self.token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.enabled = bool(self.token and self.chat_id)

    def _send(self, text: str) -> None:
        """Fire-and-forget Telegram message via raw HTTP POST. Never raises."""
        if not self.enabled:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text[:4096]},
                timeout=5,
            )
        except Exception as e:
            log.debug("telegram_send_failed", error=str(e))

    def send_scan_summary(self, markets_scanned: int, opportunities: int,
                          trades_executed: int, resolved: int,
                          balance: float, daily_pnl: float,
                          elapsed_sec: float, dry_run: bool) -> None:
        dr = "[DRY] " if dry_run else ""
        sign = "+" if daily_pnl >= 0 else ""
        self._send(
            f"{dr}Scan: {markets_scanned}mkts {opportunities}opps "
            f"{trades_executed}trades {resolved}resolved | "
            f"${balance:.2f} pnl={sign}{daily_pnl:.2f} ({elapsed_sec:.0f}s)"
        )

    def send_trade_alert(self, trade_id: str, strategy: str,
                         market_question: str, size: float,
                         edge: float, dry_run: bool) -> None:
        dr = "[DRY] " if dry_run else ""
        self._send(f"{dr}TRADE {strategy}: {market_question[:60]} ${size:.2f} edge={edge:.3f}")

    def send_error(self, component: str, error: str) -> None:
        self._send(f"ERROR {component}: {error[:150]}")

    def send_daily_summary(self, balance: float, daily_pnl: float,
                           trades: int, wins: int) -> None:
        sign = "+" if daily_pnl >= 0 else ""
        self._send(f"Daily: ${balance:.2f} pnl={sign}{daily_pnl:.2f} {trades}trades {wins}wins")

