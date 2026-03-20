"""
engines/state_engine.py
=======================
SQLite persistence layer for the Polymarket engine.

Stores: trades, positions, daily P&L, capital balance.
Also manages read/write of data/lessons.json.

NEVER places orders. NEVER calls external APIs.
All methods return safe defaults on empty/missing data — never raise on reads.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# TradeRecord dataclass
# ---------------------------------------------------------------------------

@dataclass
class TradeRecord:
    """A trade as stored in SQLite."""

    trade_id: str
    strategy: str
    market_id: str
    market_question: str
    side: str               # 'YES' | 'NO'
    price: float
    shares: float
    cost_usdc: float
    fee_usdc: float
    status: str = "open"    # 'open' | 'resolved' | 'cancelled'
    outcome: Optional[str] = None    # 'win' | 'loss' | 'push'
    pnl_usdc: Optional[float] = None
    entry_time: Optional[str] = None
    resolution_time: Optional[str] = None
    notes: str = ""


# ---------------------------------------------------------------------------
# StateEngine
# ---------------------------------------------------------------------------

class StateEngine:
    """SQLite-backed state store for all engine data."""

    _CREATE_TRADES = """
    CREATE TABLE IF NOT EXISTS trades (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id          TEXT UNIQUE NOT NULL,
        strategy          TEXT NOT NULL,
        market_id         TEXT NOT NULL,
        market_question   TEXT,
        side              TEXT NOT NULL,
        price             REAL NOT NULL,
        shares            REAL NOT NULL,
        cost_usdc         REAL NOT NULL,
        fee_usdc          REAL DEFAULT 0.0,
        status            TEXT NOT NULL DEFAULT 'open',
        outcome           TEXT,
        pnl_usdc          REAL,
        entry_time        TEXT,
        resolution_time   TEXT,
        notes             TEXT DEFAULT ''
    )"""

    _CREATE_BALANCE = """
    CREATE TABLE IF NOT EXISTS balance (
        id            INTEGER PRIMARY KEY CHECK (id = 1),
        current_usdc  REAL NOT NULL DEFAULT 100.00,
        updated_at    TEXT NOT NULL
    )"""

    _CREATE_DAILY = """
    CREATE TABLE IF NOT EXISTS daily_summary (
        date              TEXT PRIMARY KEY,
        starting_balance  REAL,
        ending_balance    REAL,
        total_trades      INTEGER DEFAULT 0,
        wins              INTEGER DEFAULT 0,
        losses            INTEGER DEFAULT 0,
        net_pnl           REAL DEFAULT 0.0,
        fees_paid         REAL DEFAULT 0.0
    )"""

    def __init__(
        self,
        db_path: str,
        lessons_path: str = "data/lessons.json",
        initial_balance: float = 100.0,
    ) -> None:
        self.db_path = db_path
        self.lessons_path = Path(lessons_path)

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.lessons_path.parent.mkdir(parents=True, exist_ok=True)

        with self._conn() as conn:
            conn.execute(self._CREATE_TRADES)
            conn.execute(self._CREATE_BALANCE)
            conn.execute(self._CREATE_DAILY)
            conn.execute(
                "INSERT OR IGNORE INTO balance (id, current_usdc, updated_at) VALUES (1, ?, ?)",
                (initial_balance, _now()),
            )
            conn.commit()

        log.info("state_engine.init", db_path=db_path)

    # -----------------------------------------------------------------------
    # Trades
    # -----------------------------------------------------------------------

    def log_trade(self, trade: TradeRecord) -> int:
        """Insert a new trade. Returns the SQLite row ID."""
        entry_time = trade.entry_time or _now()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO trades
                   (trade_id, strategy, market_id, market_question, side,
                    price, shares, cost_usdc, fee_usdc, status, entry_time, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade.trade_id, trade.strategy, trade.market_id,
                    trade.market_question, trade.side, trade.price,
                    trade.shares, trade.cost_usdc, trade.fee_usdc,
                    trade.status, entry_time, trade.notes,
                ),
            )
            conn.commit()
        log.info("state_engine.trade_logged",
                 trade_id=trade.trade_id, strategy=trade.strategy, cost_usdc=trade.cost_usdc)
        return cur.lastrowid

    def get_open_positions(self) -> list[dict]:
        """Return all trades with status='open'."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status = 'open' ORDER BY entry_time"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_open_position_count(self) -> int:
        """Count of open trades."""
        with self._conn() as conn:
            result = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status = 'open'"
            ).fetchone()
        return int(result[0]) if result else 0

    def mark_resolved(self, market_id: str, outcome: str, pnl_usdc: float) -> None:
        """Mark all open trades for a market as resolved with outcome and P&L."""
        with self._conn() as conn:
            conn.execute(
                """UPDATE trades
                   SET status = 'resolved', outcome = ?, pnl_usdc = ?, resolution_time = ?
                   WHERE market_id = ? AND status = 'open'""",
                (outcome, pnl_usdc, _now(), market_id),
            )
            conn.commit()
        log.info("state_engine.resolved", market_id=market_id, outcome=outcome, pnl=pnl_usdc)

    def get_recent_resolved_trades(self, hours: int = 48) -> list[dict]:
        """Return resolved trades from the last N hours."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM trades
                   WHERE status = 'resolved' AND resolution_time >= ?
                   ORDER BY resolution_time DESC""",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -----------------------------------------------------------------------
    # Risk helpers
    # -----------------------------------------------------------------------

    def get_daily_pnl(self) -> float:
        """Net P&L for today. Returns 0.0 on empty database — NEVER raises."""
        today = date.today().isoformat()
        try:
            with self._conn() as conn:
                result = conn.execute(
                    "SELECT COALESCE(SUM(pnl_usdc), 0.0) FROM trades WHERE DATE(resolution_time) = ?",
                    (today,),
                ).fetchone()
            return float(result[0]) if result else 0.0
        except Exception as exc:
            log.error("state_engine.daily_pnl_error", error=str(exc))
            return 0.0

    # -----------------------------------------------------------------------
    # Capital
    # -----------------------------------------------------------------------

    def get_current_balance(self) -> float:
        """Return current USDC balance."""
        with self._conn() as conn:
            result = conn.execute("SELECT current_usdc FROM balance WHERE id = 1").fetchone()
        return float(result[0]) if result else 100.0

    def update_balance(self, new_balance: float) -> None:
        """Persist new USDC balance."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE balance SET current_usdc = ?, updated_at = ? WHERE id = 1",
                (new_balance, _now()),
            )
            conn.commit()

    # -----------------------------------------------------------------------
    # Lessons (lessons.json)
    # -----------------------------------------------------------------------

    def get_lessons(self) -> dict:
        """Load lessons.json. Returns safe default dict if file missing or corrupt."""
        default = {"lessons": [], "strategy_scores": {}, "deprecated_lessons": [], "capital_history": []}
        if not self.lessons_path.exists():
            return default
        try:
            return json.loads(self.lessons_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            log.error("state_engine.lessons_parse_error", error=str(exc))
            return default

    def save_lessons(self, lessons: dict) -> None:
        """Write lessons dict to lessons.json atomically."""
        self.lessons_path.write_text(
            json.dumps(lessons, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
