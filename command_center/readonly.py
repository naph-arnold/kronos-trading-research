"""Read-only access to the runners' SQLite stores.

Every connection is opened with the SQLite URI `mode=ro`, so this process can never write to a runner's database
(SQLite itself rejects writes). `ReadOnlyShadowStore` reuses the query methods of `trading.shadow.store.ShadowStore`
without its constructor, which would open the file read-write and run schema statements.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from trading.shadow.store import ShadowStore


def connect_ro(path: Path | str, timeout: float = 5.0) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=timeout,
                          check_same_thread=False)
    con.execute("PRAGMA query_only = ON")
    return con


class ReadOnlyShadowStore(ShadowStore):
    """ShadowStore query methods over a read-only connection. All write methods are refused."""

    def __init__(self, path: Path | str):  # noqa: D107 - deliberately does not call ShadowStore.__init__
        if not Path(path).exists():
            raise FileNotFoundError(path)
        self.path = str(path)
        self._lock = threading.RLock()
        self._db = connect_ro(path)
        self._depth = 0

    def _refuse(self, *args, **kwargs):
        raise PermissionError("Command Center is read-only")

    transaction = set_meta = add_bar = ensure_book = set_realized = record_signal = _refuse
    set_signal_status = record_decision = open_position = update_position = close_position = _refuse
    add_equity = event = heartbeat = _refuse

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
