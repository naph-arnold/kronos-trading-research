"""Online state backup: consistent copy through a read-only connection, integrity check, manifest, retention."""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from research.state_backup import backup_state, verify_backup


def _db(path, rows=3):
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE heartbeats (at TEXT, detail TEXT)")
    con.executemany("INSERT INTO heartbeats VALUES (?, ?)", [(str(i), "{}") for i in range(rows)])
    con.commit()
    return con  # left open: the "runner" keeps writing while the backup runs


def test_backup_is_consistent_read_only_and_retained(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    live = _db(state / "shadow.sqlite")
    (state / "STOP").write_text("")
    (state / "preregistration.sha256").write_text("x")
    t0 = datetime(2026, 11, 16, tzinfo=timezone.utc)
    first = backup_state(state, tmp_path / "bk", keep=2, now=t0)
    assert first["ok"] and set(first["files"]) == {"shadow.sqlite"}
    live.execute("INSERT INTO heartbeats VALUES ('late', '{}')")  # runner keeps writing
    live.commit()
    backup_state(state, tmp_path / "bk", keep=2, now=t0 + timedelta(days=1))
    third = backup_state(state, tmp_path / "bk", keep=2, now=t0 + timedelta(days=2))
    sets = sorted(p.name for p in (tmp_path / "bk").iterdir())
    assert len(sets) == 2 and third["removed"] == ["backup_20261116T000000Z"]
    copy = sqlite3.connect(str(tmp_path / "bk" / sets[-1] / "shadow.sqlite"))
    assert copy.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0] == 4
    copy.close()
    assert verify_backup(tmp_path / "bk" / sets[-1])["ok"]
    assert not (tmp_path / "bk" / sets[-1] / "STOP").exists()  # operator controls are not copied
    before = hashlib.sha256((state / "shadow.sqlite").read_bytes()).hexdigest()
    backup_state(state, tmp_path / "bk", keep=2, now=t0 + timedelta(days=3))
    assert hashlib.sha256((state / "shadow.sqlite").read_bytes()).hexdigest() == before
    live.close()


def test_backup_detects_tampering_and_refuses_unsafe_destinations(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    _db(state / "shadow.sqlite").close()
    out = backup_state(state, tmp_path / "bk", keep=3)
    target = tmp_path / "bk" / out["backup"].split("\\")[-1].split("/")[-1]
    (target / "shadow.sqlite").write_bytes(b"corrupted")
    assert not verify_backup(target)["ok"]
    with pytest.raises(ValueError, match="outside"):
        backup_state(state, state / "backups")
    with pytest.raises(FileNotFoundError):
        backup_state(tmp_path / "missing", tmp_path / "bk")
