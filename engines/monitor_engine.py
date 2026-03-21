"""
engines/monitor_engine.py
=========================
Telegram notification engine.

Sends alerts for all key events.
Silent (log-only) if TELEGRAM_BOT_TOKEN is missing — NEVER crashes the engine.
All messages truncated to < 200 chars.
"""

from __future__ import annotations

import os

import requests
import structlog

log = structlog.get_logger()

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

_TEMPLATES = {
    "trade_executed":  "✅ {strategy} | {action} {question} | ${size:.2f} @ ${price:.3f}",
    "trade_rejected":  "⚠️ REJECTED: {reason} | {strategy}",
    "daily_summary":   "📊 P&L ${pnl:+.2f} | Trades: {trades} | Balance: ${balance:.2f}",
    "risk_limit_hit":  "🚨 RISK LIMIT: {limit_type} hit. Engine halted.",
    "error":           "❌ ERROR: {component} | {error}",
    "lesson_update":   "🧠 LEARNED: {lesson}",
}


class MonitorEngine:
    """Fire-and-forget Telegram alerts. Never raises."""

    def __init__(self, config: dict) -> None:
        self._token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self._enabled = bool(self._token and self._chat_id)

        if not self._enabled:
            log.warning("monitor_engine.telegram_disabled",
                        reason="TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")

    def send(self, event_type: str, **kwargs) -> None:
        """
        Format and send a Telegram message.
        On any error: log warning and return silently.
        """
        template = _TEMPLATES.get(event_type)
        if not template:
            log.warning("monitor_engine.unknown_event", event_type=event_type)
            return

        # Truncate long string fields before formatting
        for field in ("question", "error", "lesson"):
            if field in kwargs and isinstance(kwargs[field], str):
                kwargs[field] = kwargs[field][:60]

        try:
            msg = template.format(**kwargs)
        except KeyError as exc:
            log.warning("monitor_engine.format_error", event=event_type, error=str(exc))
            return

        # Enforce 200-char limit
        if len(msg) > 200:
            msg = msg[:197] + "…"

        log.info("monitor_engine.alert", event_type=event_type, message=msg)

        if not self._enabled:
            return  # Log-only mode

        try:
            resp = requests.post(
                _TELEGRAM_API.format(token=self._token),
                json={"chat_id": self._chat_id, "text": msg},
                timeout=5,
            )
            if not resp.ok:
                log.warning("monitor_engine.send_failed",
                            status=resp.status_code, body=resp.text[:100])
        except Exception as exc:
            log.warning("monitor_engine.send_error", error=str(exc))
            # Never raise — Telegram failure must never affect the engine
