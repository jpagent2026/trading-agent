"""Persistent SQLite ledger: trades, partial sells, exits, stops, P&L, and losing-streak state.

On Railway the database file must live on a mounted volume (default /data) or it is lost on redeploy.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .rules import StreakState

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,            -- instrument actually traded (e.g. SQQQ)
    signal_symbol TEXT NOT NULL,     -- symbol whose chart gave the signal (e.g. QQQ)
    direction TEXT NOT NULL,         -- 'long' or 'short' (an inverse-ETF buy is direction 'long', bias 'short')
    bias TEXT NOT NULL,              -- market direction of the idea: 'long' or 'short'
    setup TEXT NOT NULL,
    timeframe INTEGER,
    status TEXT NOT NULL,            -- 'pending', 'open', 'closed', 'rejected'
    entry_order_id TEXT,
    entry_time TEXT,
    entry_price REAL,                -- planned at signal, replaced by fill price
    qty_initial INTEGER,
    qty_open INTEGER,
    stop_price REAL,
    stop_basis TEXT,
    stop_order_id TEXT,
    stop_placed_at TEXT,
    risk_pct REAL,
    capital_base REAL,
    trims_done INTEGER DEFAULT 0,
    pending_exit TEXT,               -- reason when an exit is queued for the next open
    exit_order_id TEXT,
    exit_reason TEXT,
    exit_time TEXT,
    exit_price REAL,
    realized_pnl REAL DEFAULT 0,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    trade_id INTEGER,
    kind TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS stop_breach_exits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    trade_id INTEGER,
    symbol TEXT NOT NULL,
    direction TEXT,
    qty INTEGER,                     -- shares in the exit order (0 when nothing was sent)
    stop REAL,                       -- the ledger stop that was breached
    trigger_price REAL,              -- latest price that showed the breach
    price_source TEXT,
    reason TEXT NOT NULL,            -- 'pre_check' | 'broker_reject' | 'periodic'
    context TEXT,                    -- where it fired: entry/trim/earnings_sell_half/gtc_refresh/startup/open/cycle
    order_id TEXT,
    status TEXT NOT NULL,            -- submitted | already_pending | queued_for_open | no_position | failed | filled
    error TEXT
);
CREATE TABLE IF NOT EXISTS watchlist (
    session_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    bias TEXT NOT NULL,
    data TEXT,
    PRIMARY KEY (session_date, symbol, bias)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------ generic
    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def event(self, kind: str, trade_id: Optional[int] = None, **detail: Any) -> None:
        self._exec("INSERT INTO events (ts, trade_id, kind, detail) VALUES (?,?,?,?)",
                   (utcnow(), trade_id, kind, json.dumps(detail, default=str)))

    def events(self, limit: int = 100) -> list[dict]:
        rows = self._rows("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            try:
                r["detail"] = json.loads(r["detail"]) if r["detail"] else {}
            except Exception:
                pass
        return rows

    # ------------------------------------------------------------ trades
    def create_trade(self, **f: Any) -> int:
        f.setdefault("status", "pending")
        f.setdefault("entry_time", utcnow())
        cols = ",".join(f.keys())
        qs = ",".join("?" for _ in f)
        cur = self._exec(f"INSERT INTO trades ({cols}) VALUES ({qs})", tuple(f.values()))
        tid = int(cur.lastrowid)
        self.event("entry_submitted", tid, **f)
        return tid

    def update_trade(self, trade_id: int, **f: Any) -> None:
        if not f:
            return
        sets = ",".join(f"{k}=?" for k in f)
        self._exec(f"UPDATE trades SET {sets} WHERE id=?", tuple(f.values()) + (trade_id,))

    def get_trade(self, trade_id: int) -> Optional[dict]:
        rows = self._rows("SELECT * FROM trades WHERE id=?", (trade_id,))
        return rows[0] if rows else None

    def open_trades(self) -> list[dict]:
        return self._rows("SELECT * FROM trades WHERE status IN ('pending','open') ORDER BY id")

    def open_trade_for(self, symbol: str) -> Optional[dict]:
        rows = self._rows("SELECT * FROM trades WHERE symbol=? AND status IN ('pending','open') ORDER BY id DESC",
                          (symbol.upper(),))
        return rows[0] if rows else None

    def recent_trades(self, limit: int = 50) -> list[dict]:
        return self._rows("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))

    def realized_pnl_total(self) -> float:
        rows = self._rows("SELECT COALESCE(SUM(realized_pnl),0) AS s FROM trades")
        return float(rows[0]["s"] or 0.0)

    def closed_count(self) -> dict:
        rows = self._rows("SELECT COUNT(*) AS n, SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) AS w, "
                          "SUM(CASE WHEN realized_pnl<0 THEN 1 ELSE 0 END) AS l FROM trades WHERE status='closed'")
        r = rows[0]
        return {"closed": r["n"] or 0, "winners": r["w"] or 0, "losers": r["l"] or 0}

    # ------------------------------------------------------------ stop-breach exits
    def record_stop_breach(self, **f: Any) -> int:
        f.setdefault("ts", utcnow())
        cols = ",".join(f.keys())
        qs = ",".join("?" for _ in f)
        cur = self._exec(f"INSERT INTO stop_breach_exits ({cols}) VALUES ({qs})", tuple(f.values()))
        return int(cur.lastrowid)

    def stop_breach_exits(self, limit: int = 50) -> list[dict]:
        rows = self._rows("SELECT * FROM stop_breach_exits ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["time"] = r["ts"]
        return rows

    def mark_stop_breach_filled(self, trade_id: int) -> None:
        self._exec("UPDATE stop_breach_exits SET status='filled' WHERE trade_id=? AND status IN "
                   "('submitted','already_pending')", (trade_id,))

    # ------------------------------------------------------------ state
    def get_state(self, key: str, default: Any = None) -> Any:
        rows = self._rows("SELECT value FROM state WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except Exception:
            return rows[0]["value"]

    def set_state(self, key: str, value: Any) -> None:
        self._exec("INSERT INTO state (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value, default=str)))

    def load_streak(self) -> StreakState:
        d = self.get_state("streak", {}) or {}
        return StreakState(**{k: d[k] for k in ("consecutive_losses", "paused_until", "reduced_risk", "equity_peak") if k in d})

    def save_streak(self, s: StreakState) -> None:
        self.set_state("streak", s.__dict__)

    # ------------------------------------------------------------ watchlist
    def save_watchlist(self, session_date: str, items: list[dict]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM watchlist WHERE session_date=?", (session_date,))
            for it in items:
                self._conn.execute("INSERT OR REPLACE INTO watchlist (session_date, symbol, bias, data) VALUES (?,?,?,?)",
                                   (session_date, it["symbol"], it["bias"], json.dumps(it, default=str)))
            self._conn.commit()

    def load_watchlist(self, session_date: str) -> list[dict]:
        rows = self._rows("SELECT data FROM watchlist WHERE session_date=?", (session_date,))
        return [json.loads(r["data"]) for r in rows]

    def latest_watchlist_date(self) -> Optional[str]:
        rows = self._rows("SELECT MAX(session_date) AS d FROM watchlist")
        return rows[0]["d"] if rows else None
