"""Consistent, persisted daily-loss accounting per demo account.

Definitions (applied identically in demo operation and in replay):

* Risk day: MT5/XAUUSD uses the broker *server* calendar day (server midnight =
  17:00 New York on NY-close servers, the daily gold break). Binance spot uses
  the UTC calendar day.
* Equity: account equity including unrealized P&L, in account currency
  (MT5 `account_info().equity`; Binance: free+locked USDT plus BTC marked at
  the bid, computed by the adapter).
* Day-start equity: the first equity observed in the risk day, raised to the
  last equity observed on an earlier day if that is higher (losses between the
  last observation and the boundary are attributed to the new day: conservative).
  With no history at all, the first observation is used and flagged `estimated`.
* Cash flows (deposits/withdrawals/testnet resets) must be recorded with
  `record_cash_flow`, which shifts day-start equity so they are not counted as
  P&L.
* Daily loss fraction = (day_start - equity) / day_start. Once it reaches the
  halt threshold the day is *latched* halted, even if equity later recovers;
  the latch clears only on the next risk day.
* Observations must be chronological per account; regressions fail closed.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from trading.clock import ServerTimeZone
from trading.common import Market, ValidationError, check_identifier, require_utc, to_decimal
from trading.state import AccountSnapshot


class DayBoundary:
    """Maps a UTC instant to a risk day."""

    def __init__(self, name: str, server_tz: ServerTimeZone | None = None):
        self.name = name
        self.server_tz = server_tz

    def trading_day(self, moment: datetime) -> date:
        require_utc(moment, "moment")
        return (self.server_tz.to_server(moment) if self.server_tz else moment).date()

    @classmethod
    def utc(cls) -> "DayBoundary":
        return cls("utc_midnight")

    @classmethod
    def server(cls, tz: ServerTimeZone) -> "DayBoundary":
        return cls(f"server_midnight:{tz.name}", tz)


@dataclass(frozen=True)
class DailyLossState:
    market: str
    account_id: str
    trading_day: str
    boundary: str
    start_equity: Decimal
    current_equity: Decimal
    loss_fraction: Decimal
    halted: bool
    estimated_start: bool


class DailyLossLedger:
    def __init__(self, path: Path | str, *, market: Market, boundary: DayBoundary, halt_fraction: Decimal):
        if not isinstance(market, Market):
            raise ValidationError("Unknown market")
        halt_fraction = to_decimal(halt_fraction, "halt_fraction", positive=True)
        if halt_fraction >= 1:
            raise ValidationError("halt_fraction must be < 1")
        self.market, self.boundary, self.halt_fraction = market, boundary, halt_fraction
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS daily (market TEXT NOT NULL, account_id TEXT NOT NULL, day TEXT NOT NULL, "
            "boundary TEXT NOT NULL, start_equity TEXT NOT NULL, last_equity TEXT NOT NULL, "
            "last_at TEXT NOT NULL, halted INTEGER NOT NULL, estimated INTEGER NOT NULL, "
            "PRIMARY KEY (market, account_id, day))")

    def _row(self, account_id: str, day: str):
        return self._db.execute(
            "SELECT start_equity, last_equity, last_at, halted, estimated, boundary FROM daily "
            "WHERE market=? AND account_id=? AND day=?", (self.market.value, account_id, day)).fetchone()

    def _latest_before(self, account_id: str, day: str):
        return self._db.execute(
            "SELECT day, last_equity, last_at FROM daily WHERE market=? AND account_id=? AND day<? "
            "ORDER BY day DESC LIMIT 1", (self.market.value, account_id, day)).fetchone()

    def _state(self, account_id, day, start, equity, halted, estimated) -> DailyLossState:
        return DailyLossState(self.market.value, account_id, day, self.boundary.name, start, equity,
                              (start - equity) / start, halted, estimated)

    def observe(self, account_id: str, equity: object, at: datetime) -> DailyLossState:
        check_identifier(account_id, "account_id")
        equity = to_decimal(equity, "equity", positive=True)
        require_utc(at, "at")
        day = self.boundary.trading_day(at).isoformat()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                latest = self._db.execute(
                    "SELECT MAX(last_at) FROM daily WHERE market=? AND account_id=?",
                    (self.market.value, account_id)).fetchone()[0]
                if latest is not None and datetime.fromisoformat(latest) > at:
                    raise ValidationError("Equity observation is older than a previous one")
                row = self._row(account_id, day)
                if row is None:
                    previous = self._latest_before(account_id, day)
                    start, estimated = equity, previous is None
                    if previous is not None:
                        start = max(equity, Decimal(previous[1]))
                    halted = False
                else:
                    if row[5] != self.boundary.name:
                        raise ValidationError("Stored day boundary differs from configured boundary")
                    start, halted, estimated = Decimal(row[0]), bool(row[3]), bool(row[4])
                halted = halted or (start - equity) / start >= self.halt_fraction
                self._db.execute(
                    "INSERT INTO daily VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(market, account_id, day) DO UPDATE "
                    "SET last_equity=excluded.last_equity, last_at=excluded.last_at, halted=excluded.halted",
                    (self.market.value, account_id, day, self.boundary.name, str(start), str(equity),
                     at.isoformat(timespec="microseconds"), int(halted), int(estimated)))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return self._state(account_id, day, start, equity, halted, estimated)

    def record_cash_flow(self, account_id: str, amount: object, at: datetime) -> None:
        """Deposit (+) or withdrawal/reset (-) on the current risk day; call before the next observe()."""
        check_identifier(account_id, "account_id")
        amount = to_decimal(amount, "amount")
        require_utc(at, "at")
        day = self.boundary.trading_day(at).isoformat()
        with self._lock:
            row = self._row(account_id, day)
            if row is None:
                raise ValidationError("Observe equity for the day before recording cash flows")
            start, last = Decimal(row[0]) + amount, Decimal(row[1]) + amount
            if start <= 0 or last <= 0:
                raise ValidationError("Cash flow would make day-start equity non-positive")
            self._db.execute("UPDATE daily SET start_equity=?, last_equity=? WHERE market=? AND account_id=? "
                             "AND day=?", (str(start), str(last), self.market.value, account_id, day))

    def apply(self, snapshot: AccountSnapshot) -> AccountSnapshot:
        """Observe a snapshot and return it with ledger-derived day-start equity and halt latch."""
        if snapshot.market is not self.market:
            raise ValidationError("Snapshot belongs to another market")
        state = self.observe(snapshot.account_id, snapshot.equity, snapshot.snapshot_at)
        return replace(snapshot, day_start_equity=state.start_equity, daily_loss_halted=state.halted)

    def close(self) -> None:
        with self._lock:
            self._db.close()
