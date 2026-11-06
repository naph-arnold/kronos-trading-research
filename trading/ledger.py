"""Durable duplicate-signal ledger (SQLite). Claims are atomic per dedup key and signal ID."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from trading.common import require_utc
from trading.signals import TradeSignal


class SignalLedger:
    def __init__(self, path: Path | str = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL" if str(path) != ":memory:" else "PRAGMA journal_mode=MEMORY")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS claims (dedup_key TEXT PRIMARY KEY, market TEXT NOT NULL, "
            "signal_id TEXT NOT NULL, claimed_at TEXT NOT NULL, UNIQUE(market, signal_id))")

    def seen(self, signal: TradeSignal) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM claims WHERE dedup_key = ? OR (market = ? AND signal_id = ?) LIMIT 1",
                (signal.dedup_key, signal.market.value, signal.signal_id)).fetchone()
        return row is not None

    def claim(self, signal: TradeSignal, *, now: datetime) -> bool:
        """Atomically record the signal. False if it (or an equivalent) was already claimed."""
        require_utc(now, "now")
        with self._lock:
            try:
                self._db.execute("INSERT INTO claims VALUES (?, ?, ?, ?)",
                                 (signal.dedup_key, signal.market.value, signal.signal_id, now.isoformat()))
            except sqlite3.IntegrityError:
                return False
        return True

    def close(self) -> None:
        with self._lock:
            self._db.close()
