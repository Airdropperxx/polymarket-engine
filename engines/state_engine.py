"""
engines/state_engine.py — SQLite persistence. NEVER places orders. NEVER calls external APIs.

FIXES (v_fix1):
  - Added get_all_trades(limit)  — all trades for analytics
  - Added get_trade_stats()      — precomputed counts/PnL for dashboard (no zeros)
  - Added get_recent_trades(n)   — last N trades any status
  - connect_args check_same_thread=False + pool_pre_ping=True for GH Actions
"""
from __future__ import annotations
import json, time
from dataclasses import dataclass
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional
import structlog
from sqlalchemy import create_engine, text

log = structlog.get_logger(component="state_engine")

@dataclass
class TradeRecord:
    trade_id: str; strategy: str; market_id: str; market_question: str
    side: str; price: float; shares: float; cost_usdc: float; fee_usdc: float
    status: str = "open"; outcome: Optional[str] = None
    pnl_usdc: Optional[float] = None; entry_time: Optional[str] = None
    resolution_time: Optional[str] = None; notes: str = ""

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

class StateEngine:
    def __init__(self, db_path: str = "data/trades.db",
                 lessons_path: str = "data/lessons.json",
                 initial_balance: float = 100.0):
        self.db_path      = db_path
        self.lessons_path = Path(lessons_path)
        self._initial_bal = initial_balance
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )
        self._init_db()

    def _init_db(self) -> None:
        with self._engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT UNIQUE NOT NULL,
                    strategy TEXT, market_id TEXT, market_question TEXT,
                    side TEXT, price REAL, shares REAL,
                    cost_usdc REAL, fee_usdc REAL,
                    status TEXT DEFAULT 'open', outcome TEXT,
                    pnl_usdc REAL, entry_time TEXT,
                    resolution_time TEXT, notes TEXT DEFAULT '',
                    price_history TEXT DEFAULT '[]'
                )
            """))
            # Migration: add price_history column if not present (idempotent)
            try:
                conn.execute(text("ALTER TABLE trades ADD COLUMN price_history TEXT DEFAULT '[]'"))
            except Exception:
                pass  # column already exists

            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS balance (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    current_usdc REAL NOT NULL, updated_at TEXT
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS daily_summary (
                    date TEXT PRIMARY KEY,
                    starting_balance REAL, ending_balance REAL,
                    total_trades INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
                    net_pnl REAL DEFAULT 0.0
                )
            """))

    # ── Trade CRUD ─────────────────────────────────────────────────────────────

    def log_trade(self, trade: TradeRecord) -> int:
        try:
            with self._engine.begin() as conn:
                result = conn.execute(text("""
                    INSERT OR IGNORE INTO trades
                    (trade_id,strategy,market_id,market_question,side,
                     price,shares,cost_usdc,fee_usdc,status,outcome,
                     pnl_usdc,entry_time,resolution_time,notes)
                    VALUES
                    (:trade_id,:strategy,:market_id,:market_question,:side,
                     :price,:shares,:cost_usdc,:fee_usdc,:status,:outcome,
                     :pnl_usdc,:entry_time,:resolution_time,:notes)
                """), {
                    "trade_id":trade.trade_id,"strategy":trade.strategy,
                    "market_id":trade.market_id,"market_question":trade.market_question,
                    "side":trade.side,"price":trade.price,"shares":trade.shares,
                    "cost_usdc":trade.cost_usdc,"fee_usdc":trade.fee_usdc,
                    "status":trade.status,"outcome":trade.outcome,"pnl_usdc":trade.pnl_usdc,
                    "entry_time":trade.entry_time or _now_iso(),
                    "resolution_time":trade.resolution_time,"notes":trade.notes,
                })
                row_id = result.lastrowid or 0
            # BUG-1 FIX: deduct cost+fee from balance when a position opens.
            # Balance lifecycle:
            #   OPEN  -> balance -= (cost + fee)
            #   CLOSE -> balance += pnl_usdc
            #   where pnl WIN  = shares - cost - fee  (net profit returned)
            #   where pnl LOSS = -(cost + fee)         (full loss)
            if row_id and trade.status == "open":
                try:
                    current = self.get_current_balance()
                    spent   = float(trade.cost_usdc or 0) + float(trade.fee_usdc or 0)
                    self.update_balance(current - spent)
                    log.info("balance_deducted", trade_id=trade.trade_id,
                             spent=round(spent, 6), new_balance=round(current - spent, 4))
                except Exception as be:
                    log.error("balance_deduct_failed", trade_id=trade.trade_id, error=str(be))
            log.info("trade_logged", trade_id=trade.trade_id, strategy=trade.strategy)
            return row_id
        except Exception as e:
            log.error("log_trade_failed", trade_id=trade.trade_id, error=str(e)); return 0

    # ── Position queries ───────────────────────────────────────────────────────

    def get_open_positions(self) -> list:
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT * FROM trades WHERE status='open' ORDER BY entry_time DESC"
                )).fetchall()
                return [dict(r._mapping) for r in rows]
        except Exception as e:
            log.warning("get_open_positions_failed", error=str(e)); return []

    def get_open_position_count(self) -> int:
        try:
            with self._engine.connect() as conn:
                row = conn.execute(text("SELECT COUNT(*) FROM trades WHERE status='open'")).fetchone()
                return int(row[0]) if row else 0
        except: return 0

    def get_open_market_ids(self) -> set:
        """Return set of market_ids that have status='open'. Used for duplicate guard."""
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT market_id FROM trades WHERE status='open'"
                )).fetchall()
                return {r[0] for r in rows if r[0]}
        except:
            return set()

    def get_all_trades(self, limit: int = 0) -> list:
        """All trades newest first. limit=0 means no limit."""
        try:
            sql = "SELECT * FROM trades ORDER BY entry_time DESC"
            if limit > 0: sql += f" LIMIT {int(limit)}"
            with self._engine.connect() as conn:
                rows = conn.execute(text(sql)).fetchall()
                return [dict(r._mapping) for r in rows]
        except Exception as e:
            log.warning("get_all_trades_failed", error=str(e)); return []

    def get_recent_trades(self, limit: int = 20) -> list:
        return self.get_all_trades(limit=limit)

    def get_recent_resolved_trades(self, hours: int = 48) -> list:
        try:
            cutoff_ts = int(time.time()) - (hours * 3600)
            cutoff    = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).isoformat()
            with self._engine.connect() as conn:
                rows = conn.execute(text("""
                    SELECT * FROM trades WHERE status='resolved'
                    AND entry_time >= :cutoff ORDER BY resolution_time DESC
                """), {"cutoff": cutoff}).fetchall()
                return [dict(r._mapping) for r in rows]
        except: return []

    def get_trade_stats(self) -> dict:
        """Precomputed stats for dashboard — eliminates hardcoded zeros."""
        try:
            with self._engine.connect() as conn:
                row = conn.execute(text("""
                    SELECT COUNT(*),
                        SUM(CASE WHEN status='open' THEN 1 ELSE 0 END),
                        SUM(CASE WHEN status='resolved' THEN 1 ELSE 0 END),
                        SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END),
                        SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END),
                        COALESCE(SUM(CASE WHEN status='resolved' THEN pnl_usdc ELSE 0 END),0),
                        COALESCE(SUM(CASE WHEN status='resolved' THEN cost_usdc ELSE 0 END),0),
                        COALESCE(MAX(pnl_usdc),0), COALESCE(MIN(pnl_usdc),0)
                    FROM trades
                """)).fetchone()
                strat_rows = conn.execute(text("""
                    SELECT strategy,
                        COUNT(*) as t,
                        SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as w,
                        SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as l,
                        COALESCE(SUM(CASE WHEN status='resolved' THEN pnl_usdc ELSE 0 END),0) as pnl,
                        COALESCE(SUM(CASE WHEN status='resolved' THEN cost_usdc ELSE 0 END),0) as inv
                    FROM trades WHERE status='resolved' GROUP BY strategy
                """)).fetchall()
            total=int(row[0] or 0); opn=int(row[1] or 0); res=int(row[2] or 0)
            wins=int(row[3] or 0); losses=int(row[4] or 0)
            pnl=float(row[5] or 0); inv=float(row[6] or 0)
            best=float(row[7] or 0); worst=float(row[8] or 0)
            by_strategy = {}
            for sr in strat_rows:
                s=sr[0] or "unknown"; nt=int(sr[1] or 0); nw=int(sr[2] or 0)
                sp=float(sr[4] or 0); si=float(sr[5] or 0)
                by_strategy[s] = {
                    "trades":nt, "wins":nw, "losses":int(sr[3] or 0),
                    "win_rate_pct":round(nw/nt*100,1) if nt else 0,
                    "pnl":round(sp,6),
                    "roi_pct":round(sp/si*100,2) if si>0 else 0.0,
                }
            return {
                "total":total,"open":opn,"resolved":res,"wins":wins,"losses":losses,
                "win_rate_pct":round(wins/res*100,1) if res else 0.0,
                "total_pnl":round(pnl,6),"invested":round(inv,2),
                "roi_pct":round(pnl/inv*100,2) if inv>0 else 0.0,
                "best_pnl":round(best,6),"worst_pnl":round(worst,6),
                "by_strategy":by_strategy,
            }
        except Exception as e:
            log.warning("get_trade_stats_failed", error=str(e))
            return {"total":0,"open":0,"resolved":0,"wins":0,"losses":0,
                    "win_rate_pct":0.0,"total_pnl":0.0,"invested":0.0,"roi_pct":0.0,
                    "best_pnl":0.0,"worst_pnl":0.0,"by_strategy":{}}

    # ── Resolution ─────────────────────────────────────────────────────────────

    def snapshot_price(self, trade_id: str, current_price: float, current_ts: str) -> None:
        """Append a price snapshot to the price_history JSON array for an open position."""
        try:
            with self._engine.begin() as conn:
                row = conn.execute(text(
                    "SELECT price_history FROM trades WHERE trade_id=:tid AND status='open'"
                ), {"tid": trade_id}).fetchone()
                if not row: return
                history = json.loads(row[0] or "[]")
                history.append({"ts": current_ts, "price": round(current_price, 4)})
                conn.execute(text(
                    "UPDATE trades SET price_history=:ph WHERE trade_id=:tid"
                ), {"ph": json.dumps(history), "tid": trade_id})
        except Exception as e:
            log.warning("snapshot_price_failed", trade_id=trade_id, error=str(e))

    def mark_resolved(self, market_id: str, outcome: str, pnl_usdc: float) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(text("""
                    UPDATE trades SET status='resolved', outcome=:outcome,
                    pnl_usdc=:pnl, resolution_time=:ts
                    WHERE market_id=:mid AND status='open'
                """), {"outcome":outcome,"pnl":pnl_usdc,"ts":_now_iso(),"mid":market_id})
            current = self.get_current_balance()
            self.update_balance(current + pnl_usdc)
            log.info("position_resolved", market_id=market_id, outcome=outcome, pnl=pnl_usdc)
        except Exception as e:
            log.error("mark_resolved_failed", market_id=market_id, error=str(e))

    # ── P&L / balance ──────────────────────────────────────────────────────────

    def get_daily_pnl(self) -> float:
        try:
            today = date.today().isoformat()
            with self._engine.connect() as conn:
                row = conn.execute(text("""
                    SELECT COALESCE(SUM(pnl_usdc),0.0) FROM trades
                    WHERE status='resolved' AND DATE(resolution_time)=:today
                """), {"today":today}).fetchone()
                return float(row[0]) if row else 0.0
        except: return 0.0

    def get_current_balance(self) -> float:
        try:
            with self._engine.connect() as conn:
                row = conn.execute(text("SELECT current_usdc FROM balance WHERE id=1")).fetchone()
                return float(row[0]) if row else self._initial_bal
        except: return self._initial_bal

    def update_balance(self, new_balance: float) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO balance (id,current_usdc,updated_at) VALUES (1,:bal,:ts)
                    ON CONFLICT(id) DO UPDATE SET current_usdc=excluded.current_usdc,
                    updated_at=excluded.updated_at
                """), {"bal":new_balance,"ts":_now_iso()})
        except Exception as e:
            log.error("update_balance_failed", error=str(e))

    # ── Lessons ────────────────────────────────────────────────────────────────

    def get_lessons(self) -> dict:
        try:
            with open(self.lessons_path) as f: return json.load(f)
        except FileNotFoundError:
            return {"version":1,"lessons":[],"strategy_scores":{}}
        except Exception as e:
            log.warning("get_lessons_failed", error=str(e))
            return {"version":1,"lessons":[],"strategy_scores":{}}

    def save_lessons(self, data: dict) -> None:
        try:
            self.lessons_path.parent.mkdir(parents=True, exist_ok=True)
            self.lessons_path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            log.error("save_lessons_failed", error=str(e))
