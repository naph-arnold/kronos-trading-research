"""Durable state for the shadow engine (single SQLite file, single writer).

Everything the engine needs to resume after a restart is here: processed bars (strategy state is
rebuilt deterministically by replaying them), signals, every risk decision (approved or rejected),
open simulated positions, closed simulated trades, equity marks, operational events and heartbeats.

Duplicate prevention is structural: one decision per (book, signal) and one approved claim per
(book, dedup key) are enforced by UNIQUE constraints, not by application logic alone.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from trading.audit import to_jsonable
from trading.common import ValidationError
from trading.marketdata import Bar

SCHEMA_VERSION = 1

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS bars (open_time TEXT PRIMARY KEY, open TEXT, high TEXT, low TEXT, close TEXT, "
    "volume TEXT, interval TEXT NOT NULL, source TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS books (book_id TEXT PRIMARY KEY, strategy_key TEXT NOT NULL, "
    "initial_equity TEXT NOT NULL, commission TEXT NOT NULL, realized TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS signals (strategy_key TEXT NOT NULL, signal_id TEXT NOT NULL, "
    "bar_close TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (strategy_key, signal_id))",
    "CREATE TABLE IF NOT EXISTS decisions (book_id TEXT NOT NULL, signal_id TEXT NOT NULL, dedup_key TEXT, "
    "evaluated_at TEXT NOT NULL, approved INTEGER NOT NULL, size TEXT, planned_risk TEXT, reasons TEXT NOT NULL, "
    "context TEXT NOT NULL, PRIMARY KEY (book_id, signal_id))",
    "CREATE TABLE IF NOT EXISTS claims (book_id TEXT NOT NULL, dedup_key TEXT NOT NULL, signal_id TEXT NOT NULL, "
    "PRIMARY KEY (book_id, dedup_key))",
    "CREATE TABLE IF NOT EXISTS positions (book_id TEXT PRIMARY KEY, signal_id TEXT NOT NULL, body TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS trades (book_id TEXT NOT NULL, signal_id TEXT NOT NULL, closed_at TEXT NOT NULL, "
    "pnl TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (book_id, signal_id))",
    "CREATE TABLE IF NOT EXISTS equity (book_id TEXT NOT NULL, at TEXT NOT NULL, equity TEXT NOT NULL, "
    "PRIMARY KEY (book_id, at))",
    "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL, "
    "detail TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS heartbeats (at TEXT PRIMARY KEY, detail TEXT NOT NULL)",
)


def _dump(value) -> str:
    return json.dumps(to_jsonable(value), sort_keys=True)


class ShadowStore:
    def __init__(self, path: Path | str = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._depth = 0
        for statement in _SCHEMA:
            self._db.execute(statement)
        version = self.get_meta("schema_version")
        if version is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        elif version != str(SCHEMA_VERSION):
            raise ValidationError(f"Shadow store schema {version} != {SCHEMA_VERSION}")

    # -- transactions -------------------------------------------------------------------------
    @contextmanager
    def transaction(self):
        """Outermost call opens BEGIN IMMEDIATE; nested calls join it. Rollback on any exception."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._db.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outer:
                    self._db.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outer:
                self._db.execute("COMMIT")

    def _x(self, sql: str, args=()):
        with self._lock:
            return self._db.execute(sql, args)

    # -- meta ---------------------------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self._x("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._x("INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # -- bars ---------------------------------------------------------------------------------
    def add_bar(self, bar: Bar, source: str) -> None:
        self._x("INSERT OR IGNORE INTO bars VALUES (?,?,?,?,?,?,?,?)",
                (bar.open_time.isoformat(), str(bar.open), str(bar.high), str(bar.low), str(bar.close),
                 str(bar.volume), bar.interval, source))

    def load_bars(self, symbol: str = "XAUUSD") -> list[Bar]:
        rows = self._x("SELECT open_time, open, high, low, close, volume, interval FROM bars ORDER BY open_time")
        return [Bar(symbol=symbol, interval=r[6], open_time=datetime.fromisoformat(r[0]), open=Decimal(r[1]),
                    high=Decimal(r[2]), low=Decimal(r[3]), close=Decimal(r[4]), volume=Decimal(r[5]))
                for r in rows.fetchall()]

    def bar_count(self, source: str | None = None) -> int:
        if source is None:
            return self._x("SELECT COUNT(*) FROM bars").fetchone()[0]
        return self._x("SELECT COUNT(*) FROM bars WHERE source=?", (source,)).fetchone()[0]

    # -- books --------------------------------------------------------------------------------
    def ensure_book(self, book_id: str, strategy_key: str, initial_equity: Decimal, commission: Decimal) -> None:
        row = self._x("SELECT strategy_key, initial_equity, commission FROM books WHERE book_id=?",
                      (book_id,)).fetchone()
        if row is None:
            self._x("INSERT INTO books VALUES (?,?,?,?,?)",
                    (book_id, strategy_key, str(initial_equity), str(commission), "0"))
        elif (row[0], Decimal(row[1]), Decimal(row[2])) != (strategy_key, initial_equity, commission):
            raise ValidationError(f"Book {book_id} exists with a different configuration (frozen during the test)")

    def books(self) -> dict[str, dict]:
        rows = self._x("SELECT book_id, strategy_key, initial_equity, commission, realized FROM books").fetchall()
        return {r[0]: {"strategy_key": r[1], "initial_equity": Decimal(r[2]), "commission": Decimal(r[3]),
                       "realized": Decimal(r[4])} for r in rows}

    def set_realized(self, book_id: str, realized: Decimal) -> None:
        self._x("UPDATE books SET realized=? WHERE book_id=?", (str(realized), book_id))

    # -- signals and decisions -------------------------------------------------------------------
    def record_signal(self, strategy_key: str, signal_id: str, bar_close: datetime, status: str, body: dict) -> bool:
        cursor = self._x("INSERT OR IGNORE INTO signals VALUES (?,?,?,?,?)",
                         (strategy_key, signal_id, bar_close.isoformat(), status, _dump(body)))
        return cursor.rowcount == 1

    def set_signal_status(self, strategy_key: str, signal_id: str, status: str) -> None:
        self._x("UPDATE signals SET status=? WHERE strategy_key=? AND signal_id=?", (status, strategy_key, signal_id))

    def signals(self, status: str | None = None) -> list[dict]:
        sql = "SELECT strategy_key, signal_id, bar_close, status, body FROM signals"
        rows = (self._x(sql + " WHERE status=? ORDER BY bar_close", (status,)) if status
                else self._x(sql + " ORDER BY bar_close"))
        return [{"strategy_key": r[0], "signal_id": r[1], "bar_close": r[2], "status": r[3], "body": json.loads(r[4])}
                for r in rows.fetchall()]

    def has_decision(self, book_id: str, signal_id: str) -> bool:
        return self._x("SELECT 1 FROM decisions WHERE book_id=? AND signal_id=?", (book_id, signal_id)).fetchone() \
            is not None

    def claimed(self, book_id: str, dedup_key: str, signal_id: str) -> bool:
        return self._x("SELECT 1 FROM claims WHERE book_id=? AND (dedup_key=? OR signal_id=?)",
                       (book_id, dedup_key, signal_id)).fetchone() is not None

    def record_decision(self, book_id: str, signal_id: str, *, dedup_key: str | None, evaluated_at: datetime,
                        approved: bool, size, planned_risk, reasons, context: dict) -> None:
        """Raises sqlite3.IntegrityError on a second decision for the same (book, signal) or a second approved
        claim for the same dedup key: the caller's transaction then rolls back (duplicate prevention)."""
        with self.transaction():
            self._x("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?)",
                    (book_id, signal_id, dedup_key, evaluated_at.isoformat(), int(approved),
                     None if size is None else str(size), None if planned_risk is None else str(planned_risk),
                     _dump(list(reasons)), _dump(context)))
            if approved:
                self._x("INSERT INTO claims VALUES (?,?,?)", (book_id, dedup_key, signal_id))

    def decisions(self, book_id: str | None = None) -> list[dict]:
        sql = ("SELECT book_id, signal_id, dedup_key, evaluated_at, approved, size, planned_risk, reasons, context "
               "FROM decisions")
        rows = (self._x(sql + " WHERE book_id=? ORDER BY evaluated_at", (book_id,)) if book_id
                else self._x(sql + " ORDER BY evaluated_at"))
        return [{"book_id": r[0], "signal_id": r[1], "dedup_key": r[2], "evaluated_at": r[3], "approved": bool(r[4]),
                 "size": r[5], "planned_risk": r[6], "reasons": json.loads(r[7]), "context": json.loads(r[8])}
                for r in rows.fetchall()]

    # -- positions and trades ----------------------------------------------------------------------
    def open_position(self, book_id: str, signal_id: str, body: dict) -> None:
        self._x("INSERT INTO positions VALUES (?,?,?)", (book_id, signal_id, _dump(body)))

    def update_position(self, book_id: str, body: dict) -> None:
        self._x("UPDATE positions SET body=? WHERE book_id=?", (_dump(body), book_id))

    def positions(self) -> dict[str, dict]:
        return {r[0]: json.loads(r[2]) for r in self._x("SELECT book_id, signal_id, body FROM positions").fetchall()}

    def close_position(self, book_id: str, signal_id: str, closed_at: datetime, pnl: Decimal, body: dict) -> None:
        with self.transaction():
            deleted = self._x("DELETE FROM positions WHERE book_id=? AND signal_id=?", (book_id, signal_id)).rowcount
            if deleted != 1:
                raise ValidationError(f"No open simulated position {signal_id} in {book_id}")
            self._x("INSERT INTO trades VALUES (?,?,?,?,?)",
                    (book_id, signal_id, closed_at.isoformat(), str(pnl), _dump(body)))

    def trades(self, book_id: str | None = None) -> list[dict]:
        sql = "SELECT book_id, body FROM trades"
        rows = (self._x(sql + " WHERE book_id=? ORDER BY closed_at", (book_id,)) if book_id
                else self._x(sql + " ORDER BY closed_at"))
        return [{"book_id": r[0], **json.loads(r[1])} for r in rows.fetchall()]

    def realized_from_trades(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for book_id, pnl in self._x("SELECT book_id, pnl FROM trades").fetchall():
            out[book_id] = out.get(book_id, Decimal("0")) + Decimal(pnl)
        return out

    # -- equity, events, heartbeats -------------------------------------------------------------------
    def add_equity(self, book_id: str, at: datetime, equity: Decimal) -> None:
        self._x("INSERT OR REPLACE INTO equity VALUES (?,?,?)", (book_id, at.isoformat(), str(equity)))

    def equity_curve(self, book_id: str) -> list[tuple[str, Decimal]]:
        rows = self._x("SELECT at, equity FROM equity WHERE book_id=? ORDER BY at", (book_id,)).fetchall()
        return [(r[0], Decimal(r[1])) for r in rows]

    def event(self, at: datetime, kind: str, **detail) -> None:
        self._x("INSERT INTO events (at, kind, detail) VALUES (?,?,?)", (at.isoformat(), kind, _dump(detail)))

    def events(self, kind: str | None = None) -> list[dict]:
        sql = "SELECT at, kind, detail FROM events"
        rows = self._x(sql + " WHERE kind=? ORDER BY id", (kind,)) if kind else self._x(sql + " ORDER BY id")
        return [{"at": r[0], "kind": r[1], **json.loads(r[2])} for r in rows.fetchall()]

    def heartbeat(self, at: datetime, **detail) -> None:
        self._x("INSERT OR REPLACE INTO heartbeats VALUES (?,?)", (at.isoformat(), _dump(detail)))

    def heartbeats(self) -> list[tuple[datetime, dict]]:
        return [(datetime.fromisoformat(r[0]), json.loads(r[1]))
                for r in self._x("SELECT at, detail FROM heartbeats ORDER BY at").fetchall()]

    def close(self) -> None:
        with self._lock:
            self._db.close()
