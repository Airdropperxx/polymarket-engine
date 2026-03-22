"""
engines/monitor_engine.py — Telegram alerts. NEVER raises. Silent if token missing.
Messages < 200 chars per the spec.
"""

from __future__ import annotations

import os
import structlog
import requests

log = structlog.get_logger(component="monitor_engine")


class MonitorEngine:
    def __init__(self, config: dict):
        self.token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)
        self.config  = config

    def _send(self, text: str) -> None:
        if not self.enabled:
            log.debug("telegram_disabled")
            return
        try:
            # Truncate to 200 chars per spec
            msg = text[:200]
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            requests.post(url, json={"chat_id": self.chat_id, "text": msg},
                          timeout=5)
        except Exception as e:
            log.debug("telegram_send_failed", error=str(e))
            # Never raise

    def send_scan_summary(self, markets_scanned: int, opportunities: int,
                          trades_executed: int, resolved: int,
                          balance: float, daily_pnl: float,
                          elapsed_sec: float, dry_run: bool) -> None:
        dr = "[DRY] " if dry_run else ""
        pnl_sign = "+" if daily_pnl >= 0 else ""
        self._send(
            f"{dr}Scan done: {markets_scanned} mkts, "
            f"{opportunities} opps, {trades_executed} trades, "
            f"{resolved} resolved | "
            f"Bal=${balance:.2f} PnL={pnl_sign}{daily_pnl:.2f} ({elapsed_sec:.0f}s)"
        )

    def send_trade_alert(self, trade_id: str, strategy: str,
                         market_question: str, size: float,
                         edge: float, dry_run: bool) -> None:
        dr = "[DRY] " if dry_run else ""
        q  = market_question[:60]
        self._send(
            f"{dr}TRADE {strategy}: {q}... "
            f"${size:.2f} edge={edge:.3f}"
        )

    def send_error(self, component: str, error: str) -> None:
        self._send(f"ERROR in {component}: {error[:120]}")

    def send_daily_summary(self, balance: float, daily_pnl: float,
                           trades: int, wins: int) -> None:
        pnl_sign = "+" if daily_pnl >= 0 else ""
        self._send(
            f"Daily summary: Bal=${balance:.2f} "
            f"PnL={pnl_sign}{daily_pnl:.2f} | "
            f"{trades} trades, {wins} wins"
        )
