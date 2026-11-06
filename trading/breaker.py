"""Per-market circuit breaker and emergency kill switch, persisted across restarts.

Tripping is automatic or manual; resetting is manual only and records the
operator. With a `state_path`, trips are written atomically to JSON before
`trip()` returns; on start-up an unreadable, corrupt or foreign state file is
treated as *tripped* (fail closed). An optional kill file halts the market
while it exists. Single process per market; not a cross-process lock.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from trading.common import Market

STATE_VERSION = 1


class CircuitBreaker:
    def __init__(self, market: Market, *, kill_file: Path | None = None, state_path: Path | None = None):
        self.market = market
        self.kill_file = Path(kill_file) if kill_file is not None else None
        self.state_path = Path(state_path) if state_path is not None else None
        self._lock = threading.Lock()
        self._trips: list[dict] = []
        self._history: list[dict] = []
        self._load_error: str | None = None
        if self.state_path is not None:
            self._load()

    # -- persistence -----------------------------------------------------------
    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state.get("version") != STATE_VERSION or state.get("market") != self.market.value:
                raise ValueError("version/market mismatch")
            trips, history = state["trips"], state.get("history", [])
            if not isinstance(trips, list) or not all(isinstance(t, dict) and t.get("reason") for t in trips):
                raise ValueError("malformed trips")
            self._trips, self._history = trips, history if isinstance(history, list) else []
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            self._load_error = f"Breaker state unreadable ({type(exc).__name__}); manual reset required"

    def _save(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        payload = {"version": STATE_VERSION, "market": self.market.value, "trips": self._trips,
                   "history": self._history[-500:]}
        try:
            with staging.open("w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staging, self.state_path)
        finally:
            staging.unlink(missing_ok=True)

    # -- operations ------------------------------------------------------------
    def trip(self, reason: str) -> None:
        """Trip and persist. In-memory trip holds even if persisting fails (error re-raised)."""
        entry = {"at": datetime.now(timezone.utc).isoformat(), "reason": reason or "unspecified"}
        with self._lock:
            self._trips.append(entry)
            self._history.append({"event": "trip", **entry})
            self._save()

    def reset(self, *, operator: str) -> None:
        if not operator or not operator.strip():
            raise ValueError("Manual reset requires an operator identity")
        with self._lock:
            self._history.append({"event": "reset", "at": datetime.now(timezone.utc).isoformat(),
                                  "operator": operator.strip(), "cleared": [t["reason"] for t in self._trips],
                                  "cleared_load_error": self._load_error})
            self._trips = []
            self._load_error = None
            self._save()

    def reasons(self) -> list[str]:
        with self._lock:
            reasons = [trip["reason"] for trip in self._trips]
            if self._load_error:
                reasons.append(self._load_error)
        try:
            if self.kill_file is not None and self.kill_file.exists():
                reasons.append(f"Kill file present: {self.kill_file}")
        except OSError:
            reasons.append("Kill file state unreadable")
        return reasons

    def history(self) -> list[dict]:
        with self._lock:
            return list(self._history)

    @property
    def is_tripped(self) -> bool:
        return bool(self.reasons())
