"""
engines/state_engine.py — SQLite persistence. NEVER places orders. NEVER calls external APIs.

Handles:
  - Trade logging (trades.db)
  - Balance tracking
  - Open position management
  - Resolution recording
  - lessons.json read/write

get_daily_pnl() returns 0.0 on empty DB, NEVER raises.
All read methods are safe: return empty/zero values on any error.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional

import structlog
from sqlalchemy import create_engine, text

log = structlog.get_logger(component="state_engine")


@dataclass
class TradeRecord:
    trade_id:        str
    strategy:        str
    market_id:       str
    market_question: str
    side:            str        # 'YES' | 'NO' | 'ALL_YES'
    price:           float
    shares:          float
    cost_usdc:       float
    fee_usdc:        float
    status:          str        = "open"   # 'open' | 'resolved' | 'cancelled'
    outcome:         Optional[str]  = None  # 'win' | 'loss' | 'push'
    pnl_usdc:        Optional[float] = None
    entry_time:      Optional[str]  = None
    resolution_time: Optional[str]  = None
    notes:           str        = ""


class StateEngine:
    def __init__(self,
                 db_path:         str   = "data/trades.db",
                 lessons_path:    str   = "data/lessons.json",
                 initial_balance: float = 100.0):
        self.db_path      = db_path
        self.lessons_path = Path(lessons_path)
        self._initial_bal = initial_balance

        # Ensure data directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self._engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )
        self._init_db()

    # ── Schema init ────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with self._engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS trades (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id         TEXT UNIQUE NOT NULL,
                    strategy         TEXT,
                    market_id        TEXT,
                    market_question  TEXT,
                    side             TEXT,
                    price            REAL,
                    shares           REAL,
                    cost_usdc        REAL,
                    fee_usdc         REAL,
                    status           TEXT DEFAULT 'open',
                    outcome          TEXT,
                    pnl_usdc         REAL,
                    entry_time       TEXT,
                    resolution_time  TEXT,
                    notes            TEXT DEFAULT ''
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS balance (
                    id           INTEGER PRIMARY KEY CHECK(id = 1),
                    current_usdc REAL NOT NULL,
                    updated_at   TEXT
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS daily_summary (
                    date              TEXT PRIMARY KEY,
                    starting_balance  REAL,
                    ending_balance    REAL,
                    total_trades      INTEGER DEFAULT 0,
                    wins              INTEGER DEFAULT 0,
                    losses            INTEGER DEFAULT 0,
                    net_pnl           REAL DEFAULT 0.0,
                    fees_paid         REAL DEFAULT 0.0
                )
            """))

            # Seed balance if first run
            row = conn.execute(text("SELECT current_usdc FROM balance WHERE id=1")).fetchone()
            if not row:
                conn.execute(text(
                    "INSERT INTO balance (id, current_usdc, updated_at) VALUES (1, :bal, :ts)"
                ), {"bal": self._initial_bal, "ts": _now_iso()})

    # ── Trade logging ──────────────────────────────────────────────────────────

    def log_trade(self, trade: TradeRecord) -> int:
        try:
            with self._engine.begin() as conn:
                result = conn.execute(text("""
                    INSERT OR IGNORE INTO trades
                    (trade_id, strategy, market_id, market_question, side,
                     price, shares, cost_usdc, fee_usdc, status,
                     outcome, pnl_usdc, entry_time, resolution_time, notes)
                    VALUES
                    (:trade_id, :strategy, :market_id, :market_question, :side,
                     :price, :shares, :cost_usdc, :fee_usdc, :status,
                     :outcome, :pnl_usdc, :entry_time, :resolution_time, :notes)
                """), {
                    "trade_id":        trade.trade_id,
                    "strategy":        trade.strategy,
                    "market_id":       trade.market_id,
                    "market_question": trade.market_question,
                    "side":            trade.side,
                    "price":           trade.price,
                    "shares":          trade.shares,
                    "cost_usdc":       trade.cost_usdc,
                    "fee_usdc":        trade.fee_usdc,
                    "status":          trade.status,
                    "outcome":         trade.outcome,
                    "pnl_usdc":        trade.pnl_usdc,
                    "entry_time":      trade.entry_time or _now_iso(),
                    "resolution_time": trade.resolution_time,
                    "notes":           trade.notes,
                })
                row_id = result.lastrowid or 0
            log.info("trade_logged", trade_id=trade.trade_id, strategy=trade.strategy)
            return row_id
        except Exception as e:
            log.error("log_trade_failed", trade_id=trade.trade_id, error=str(e))
            return 0

    # ── Position queries ───────────────────────────────────────────────────────

    def get_open_positions(self) -> list[dict]:
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT * FROM trades WHERE status='open'")
                ).fetchall()
                return [dict(r._mapping) for r in rows]
        except Exception as e:
            log.warning("get_open_positions_failed", error=str(e))
            return []

    def get_open_position_count(self) -> int:
        try:
            with self._engine.connect() as conn:
                row = conn.execute(
                    text("SELECT COUNT(*) FROM trades WHERE status='open'")
                ).fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    def mark_resolved(self, market_id: str, outcome: str, pnl_usdc: float) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(text("""
                    UPDATE trades
                    SET status='resolved', outcome=:outcome,
                        pnl_usdc=:pnl, resolution_time=:ts
                    WHERE market_id=:mid AND status='open'
                """), {
                    "outcome": outcome,
                    "pnl":     pnl_usdc,
                    "ts":      _now_iso(),
                    "mid":     market_id,
                })

            # Update balance
            current = self.get_current_balance()
            self.update_balance(current + pnl_usdc)
            log.info("position_resolved",
                     market_id=market_id, outcome=outcome, pnl=pnl_usdc)
        except Exception as e:
            log.error("mark_resolved_failed", market_id=market_id, error=str(e))

    # ── P&L / balance ──────────────────────────────────────────────────────────

    def get_daily_pnl(self) -> float:
        """Returns 0.0 on empty DB or any error. NEVER raises."""
        try:
            today = date.today().isoformat()
            with self._engine.connect() as conn:
                row = conn.execute(text("""
                    SELECT COALESCE(SUM(pnl_usdc), 0.0)
                    FROM trades
                    WHERE status='resolved'
                    AND DATE(resolution_time) = :today
                """), {"today": today}).fetchone()
                return float(row[0]) if row else 0.0
        except Exception:
            return 0.0

    def get_current_balance(self) -> float:
        try:
            with self._engine.connect() as conn:
                row = conn.execute(
                    text("SELECT current_usdc FROM balance WHERE id=1")
                ).fetchone()
                return float(row[0]) if row else self._initial_bal
        except Exception:
            return self._initial_bal

    def update_balance(self, new_balance: float) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO balance (id, current_usdc, updated_at)
                    VALUES (1, :bal, :ts)
                    ON CONFLICT(id) DO UPDATE SET
                        current_usdc = excluded.current_usdc,
                        updated_at   = excluded.updated_at
                """), {"bal": round(new_balance, 4), "ts": _now_iso()})
        except Exception as e:
            log.warning("update_balance_failed", error=str(e))

    def get_recent_resolved_trades(self, hours: int = 48) -> list[dict]:
        try:
            cutoff_ts = int(time.time()) - (hours * 3600)
            with self._engine.connect() as conn:
                rows = conn.execute(text("""
                    SELECT * FROM trades
                    WHERE status = 'resolved'
                    AND entry_time >= :cutoff
                    ORDER BY resolution_time DESC
                """), {"cutoff": datetime.fromtimestamp(
                    cutoff_ts, tz=timezone.utc).isoformat()}).fetchall()
                return [dict(r._mapping) for r in rows]
        except Exception:
            return []

    # ── Lessons ────────────────────────────────────────────────────────────────

    def get_lessons(self) -> dict:
        try:
            with open(self.lessons_path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {"version": 1, "lessons": [], "strategy_scores": {}}
        except Exception as e:
            log.warning("get_lessons_failed", error=str(e))
            return {}

    def save_lessons(self, lessons: dict) -> None:
        try:
            self.lessons_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.lessons_path, "w") as f:
                json.dump(lessons, f, indent=2)
        except Exception as e:
            log.error("save_lessons_failed", error=str(e))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
