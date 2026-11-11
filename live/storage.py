"""
SQLite logger for the live bot.

Single file at logs/bot.sqlite holds:
  - predictions: every model output (one row per bar per symbol)
  - orders:      every order ticket (dry-run rows have dry_run=1, no IB ids)
  - equity:      periodic snapshots of account equity (Phase 3+)
  - events:      free-form log messages (startup, errors, disconnects, etc.)

Dashboard (Phase 4) reads from these tables directly — no extra layer.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import LOGS_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    symbol       TEXT    NOT NULL,
    model        TEXT    NOT NULL,
    prediction   INTEGER NOT NULL,
    confidence   REAL    NOT NULL,
    close_price  REAL,
    atr          REAL,
    features_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_pred_ts     ON predictions(ts);
CREATE INDEX IF NOT EXISTS idx_pred_symbol ON predictions(symbol);

CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    sleeve       TEXT    NOT NULL DEFAULT 'tactical',
    symbol       TEXT    NOT NULL,
    action       TEXT    NOT NULL,
    quantity     REAL    NOT NULL,
    entry_price  REAL    NOT NULL,
    tp_price     REAL,
    stop_price   REAL,
    parent_id    INTEGER,
    tp_id        INTEGER,
    stop_id      INTEGER,
    dry_run      INTEGER NOT NULL,
    status       TEXT    NOT NULL,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_ts     ON orders(ts);
CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);

CREATE TABLE IF NOT EXISTS equity (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT    NOT NULL,
    equity    REAL    NOT NULL,
    cash      REAL,
    positions REAL
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    source  TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

-- Phase 3 — portfolio sizing decisions. One row per symbol per cycle.
-- Phase 6 — `sleeve` column added so we can filter long-term vs tactical.
CREATE TABLE IF NOT EXISTS targets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id        TEXT    NOT NULL,
    ts              TEXT    NOT NULL,
    sleeve          TEXT    NOT NULL DEFAULT 'tactical',
    symbol          TEXT    NOT NULL,
    prediction      INTEGER,
    confidence      REAL,
    sentiment_score REAL,
    close_price     REAL,
    realized_vol    REAL,
    signal_weight   REAL,
    vol_scale       REAL,
    target_shares   INTEGER NOT NULL,
    position_value  REAL,
    reason          TEXT
);
CREATE INDEX IF NOT EXISTS idx_targets_cycle  ON targets(cycle_id);
CREATE INDEX IF NOT EXISTS idx_targets_ts     ON targets(ts);
CREATE INDEX IF NOT EXISTS idx_targets_sleeve ON targets(sleeve);

-- Phase 6 — sentiment readings, written by the sentiment worker.
CREATE TABLE IF NOT EXISTS sentiment (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT    NOT NULL,
    symbol     TEXT    NOT NULL,
    source     TEXT    NOT NULL,
    score      REAL    NOT NULL,        -- normalized to [-1, 1]
    confidence REAL,                    -- provider's own confidence, 0..1
    article_count INTEGER,
    raw_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sentiment_symbol_ts ON sentiment(symbol, ts);

-- Phase 6 — open positions per sleeve. Updated by the runner on fills.
-- For paper trading we track our own view; for live we sync from IB.
CREATE TABLE IF NOT EXISTS holdings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sleeve      TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    shares      REAL    NOT NULL,
    avg_cost    REAL    NOT NULL,
    opened_at   TEXT    NOT NULL,
    last_update TEXT    NOT NULL,
    UNIQUE(sleeve, symbol)
);

-- Phase 6 — rebalance history for the long-term sleeve.
CREATE TABLE IF NOT EXISTS rebalances (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    sleeve        TEXT NOT NULL,
    reason        TEXT NOT NULL,
    equity_before REAL,
    notes         TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, db_path: Path | None = None) -> None:
        self.path = Path(db_path) if db_path else LOGS_DIR / "bot.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- writes ---

    def log_prediction(
        self,
        *,
        symbol: str,
        model: str,
        prediction: int,
        confidence: float,
        close_price: float | None,
        atr: float | None,
        features: dict[str, float] | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO predictions (ts, symbol, model, prediction, confidence, "
                "close_price, atr, features_json) VALUES (?,?,?,?,?,?,?,?)",
                (
                    _now(),
                    symbol,
                    model,
                    int(prediction),
                    float(confidence),
                    close_price,
                    atr,
                    json.dumps(features, default=float) if features else None,
                ),
            )
            return cur.lastrowid

    def log_order(
        self,
        *,
        symbol: str,
        action: str,
        quantity: float,
        entry_price: float,
        tp_price: float | None,
        stop_price: float | None,
        parent_id: int | None,
        tp_id: int | None,
        stop_id: int | None,
        dry_run: bool,
        status: str,
        sleeve: str = "tactical",
        note: str | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO orders (ts, sleeve, symbol, action, quantity, entry_price, "
                "tp_price, stop_price, parent_id, tp_id, stop_id, dry_run, status, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _now(),
                    sleeve,
                    symbol,
                    action,
                    float(quantity),
                    float(entry_price),
                    tp_price,
                    stop_price,
                    parent_id,
                    tp_id,
                    stop_id,
                    int(bool(dry_run)),
                    status,
                    note,
                ),
            )
            return cur.lastrowid

    def log_equity(
        self, equity: float, cash: float | None = None, positions: float | None = None
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO equity (ts, equity, cash, positions) VALUES (?,?,?,?)",
                (_now(), float(equity), cash, positions),
            )

    def log_target(
        self,
        *,
        cycle_id: str,
        symbol: str,
        prediction: int | None,
        confidence: float | None,
        close_price: float | None,
        realized_vol: float | None,
        signal_weight: float | None,
        vol_scale: float | None,
        target_shares: int,
        position_value: float | None,
        reason: str | None,
        sleeve: str = "tactical",
        sentiment_score: float | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO targets (cycle_id, ts, sleeve, symbol, prediction, confidence, "
                "sentiment_score, close_price, realized_vol, signal_weight, vol_scale, "
                "target_shares, position_value, reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cycle_id,
                    _now(),
                    sleeve,
                    symbol,
                    prediction,
                    confidence,
                    sentiment_score,
                    close_price,
                    realized_vol,
                    signal_weight,
                    vol_scale,
                    int(target_shares),
                    position_value,
                    reason,
                ),
            )
            return cur.lastrowid

    def log_sentiment(
        self,
        *,
        symbol: str,
        source: str,
        score: float,
        confidence: float | None = None,
        article_count: int | None = None,
        raw_json: str | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO sentiment (ts, symbol, source, score, confidence, "
                "article_count, raw_json) VALUES (?,?,?,?,?,?,?)",
                (_now(), symbol, source, float(score), confidence, article_count, raw_json),
            )
            return cur.lastrowid

    def latest_sentiment(self, symbol: str, max_age_sec: int) -> dict[str, Any] | None:
        """Return the most recent sentiment row for symbol if it's fresher than max_age_sec."""
        cutoff_iso = datetime.now(timezone.utc).timestamp() - max_age_sec
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM sentiment WHERE symbol = ? ORDER BY id DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        if row is None:
            return None
        row_ts = datetime.fromisoformat(row["ts"]).timestamp()
        if row_ts < cutoff_iso:
            return None
        return dict(row)

    def get_holdings(self, sleeve: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM holdings WHERE sleeve = ? ORDER BY symbol", (sleeve,)
                ).fetchall()
            ]

    def delete_holding(self, *, sleeve: str, symbol: str) -> None:
        """Used by the satellite sleeve when it exits a position."""
        with self._conn() as c:
            c.execute(
                "DELETE FROM holdings WHERE sleeve = ? AND symbol = ?",
                (sleeve, symbol),
            )

    def latest_sentiment_batch(
        self,
        symbols: list[str],
        max_age_sec: int,
    ) -> dict[str, dict[str, Any]]:
        """One latest-fresh-sentiment-row per symbol. Empty if none fresh enough."""
        out: dict[str, dict[str, Any]] = {}
        for sym in symbols:
            row = self.latest_sentiment(sym, max_age_sec)
            if row is not None:
                out[sym] = row
        return out

    def upsert_holding(
        self,
        *,
        sleeve: str,
        symbol: str,
        shares: float,
        avg_cost: float,
    ) -> None:
        with self._conn() as c:
            existing = c.execute(
                "SELECT id FROM holdings WHERE sleeve = ? AND symbol = ?",
                (sleeve, symbol),
            ).fetchone()
            if existing:
                c.execute(
                    "UPDATE holdings SET shares=?, avg_cost=?, last_update=? WHERE id=?",
                    (shares, avg_cost, _now(), existing["id"]),
                )
            else:
                c.execute(
                    "INSERT INTO holdings (sleeve, symbol, shares, avg_cost, "
                    "opened_at, last_update) VALUES (?,?,?,?,?,?)",
                    (sleeve, symbol, shares, avg_cost, _now(), _now()),
                )

    def log_rebalance(
        self,
        *,
        sleeve: str,
        reason: str,
        equity_before: float | None = None,
        notes: str | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO rebalances (ts, sleeve, reason, equity_before, notes) "
                "VALUES (?,?,?,?,?)",
                (_now(), sleeve, reason, equity_before, notes),
            )
            return cur.lastrowid

    def last_rebalance(self, sleeve: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM rebalances WHERE sleeve = ? ORDER BY id DESC LIMIT 1",
                (sleeve,),
            ).fetchone()
        return dict(row) if row else None

    def recent_target_cycles(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return the latest N cycles, each with its per-symbol targets nested."""
        with self._conn() as c:
            cycle_rows = c.execute(
                "SELECT cycle_id, MAX(ts) AS ts FROM targets "
                "GROUP BY cycle_id ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for row in cycle_rows:
                rows = c.execute(
                    "SELECT * FROM targets WHERE cycle_id = ? ORDER BY symbol",
                    (row["cycle_id"],),
                ).fetchall()
                out.append(
                    {
                        "cycle_id": row["cycle_id"],
                        "ts": row["ts"],
                        "targets": [dict(r) for r in rows],
                    }
                )
            return out

    def log_event(self, level: str, source: str, message: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO events (ts, level, source, message) VALUES (?,?,?,?)",
                (_now(), level, source, message),
            )

    # --- reads (used by the dashboard later) ---

    def recent_predictions(
        self, limit: int = 50, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM predictions"
        args: tuple = ()
        if symbol:
            q += " WHERE symbol = ?"
            args = (symbol,)
        q += " ORDER BY id DESC LIMIT ?"
        args = args + (limit,)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    def recent_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            ]

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            ]

    def equity_history(self, limit: int = 1000) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM equity ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            ]
