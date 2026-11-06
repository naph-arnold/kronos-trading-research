"""Trading-session calendars and session-aware bar validation.

Sessions are defined in *server* wall-clock time (how MT5 brokers publish
them) and evaluated from UTC through a verified `ServerTimeZone`. The MT5
Python API does not expose session tables, so the calendar is configuration
that must be checked against the broker's contract specification; holidays
and early closes are explicit UTC closures supplied by the operator.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from functools import cached_property
from datetime import datetime, timedelta
from typing import Sequence

from trading.clock import NEW_YORK, NY_CLOSE, ServerTimeZone
from trading.common import ValidationError, require_utc
from trading.marketdata import INTERVALS, Bar, validate_series

DAY_MINUTES = 1440


@dataclass(frozen=True)
class SessionWindow:
    weekday: int  # server weekday, Monday=0
    start_minute: int  # inclusive, minutes after server midnight
    end_minute: int  # exclusive, may be 1440 (midnight)

    def validate(self) -> None:
        if (type(self.weekday) is not int or not 0 <= self.weekday <= 6
                or type(self.start_minute) is not int or type(self.end_minute) is not int
                or not 0 <= self.start_minute < self.end_minute <= DAY_MINUTES):
            raise ValidationError(f"Invalid session window {self}")


@dataclass(frozen=True)
class TradingCalendar:
    symbol: str
    server_tz: ServerTimeZone
    windows: tuple[SessionWindow, ...]
    closures: tuple[tuple[datetime, datetime], ...] = ()  # UTC [start, end) holidays / early closes
    open_buffer: timedelta = timedelta(minutes=15)  # no new trades just after an open (wide spreads)
    close_buffer: timedelta = timedelta(minutes=15)  # nor just before a close
    always_open: bool = field(default=False)
    # "template" (unconfirmed), "rule_based" (exchange holiday rules added), "broker_verified"
    # (template + holidays confirmed against broker history and a verified server offset).
    verification: str = "template"
    # Risk-day boundary relative to this calendar's time zone (e.g. +7h on New York time = 17:00 NY close).
    risk_day_shift: timedelta = timedelta(0)
    # Additional calendars that must ALSO be open (intersection; e.g. broker server-time sessions on top of
    # exchange market hours). Constraints can only make the calendar stricter.
    constraints: tuple["TradingCalendar", ...] = ()

    def validate(self) -> None:
        if not self.always_open and not self.windows:
            raise ValidationError("Calendar needs session windows")
        for window in self.windows:
            window.validate()
        for constraint in self.constraints:
            if constraint.symbol != self.symbol:
                raise ValidationError("Constraint calendar is for another symbol")
            constraint.validate()
        for start, end in self.closures:
            require_utc(start, "closure start")
            require_utc(end, "closure end")
            if end <= start:
                raise ValidationError("Closure end must follow start")
        if self.open_buffer < timedelta(0) or self.close_buffer < timedelta(0):
            raise ValidationError("Buffers must be non-negative")

    def _window_at(self, server: datetime) -> SessionWindow | None:
        minute = server.hour * 60 + server.minute
        for window in self.windows:
            if window.weekday == server.weekday() and window.start_minute <= minute < window.end_minute:
                return window
        return None

    @cached_property
    def _merged_closures(self) -> tuple[list, list]:
        merged: list[list] = []
        for start, end in sorted(self.closures):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return [m[0] for m in merged], [m[1] for m in merged]

    def _closure_at(self, moment: datetime) -> tuple[datetime, datetime] | None:
        """Binary search over merged closures (thousands of CME closures over multi-year backtests)."""
        if not self.closures:
            return None
        starts, ends = self._merged_closures
        i = bisect_right(starts, moment) - 1
        if i >= 0 and moment < ends[i]:
            return starts[i], ends[i]
        return None

    def is_open(self, moment: datetime) -> bool:
        require_utc(moment, "moment")
        if self._closure_at(moment):
            return False
        if not all(c.is_open(moment) for c in self.constraints):
            return False
        return self.always_open or self._window_at(self.server_tz.to_server(moment)) is not None

    def _period(self, moment: datetime) -> tuple[datetime, datetime]:
        """UTC bounds of the contiguous open period containing `moment` (windows chained across midnight)."""
        server = self.server_tz.to_server(moment)
        window = self._window_at(server)
        midnight = server.replace(hour=0, minute=0, second=0, microsecond=0)
        start = midnight + timedelta(minutes=window.start_minute)
        end = midnight + timedelta(minutes=window.end_minute)
        for _ in range(7):  # extend backwards across adjacent windows
            if window.start_minute != 0:
                break
            prev = self._window_at(start - timedelta(minutes=1))
            if prev is None or prev.end_minute != DAY_MINUTES:
                break
            window = prev
            start = start - timedelta(days=1) + timedelta(minutes=prev.start_minute)
        window = self._window_at(end - timedelta(minutes=1))
        for _ in range(7):  # and forwards
            if window.end_minute != DAY_MINUTES:
                break
            nxt = self._window_at(end)
            if nxt is None or nxt.start_minute != 0:
                break
            window = nxt
            end = end + timedelta(minutes=nxt.end_minute)
        return self.server_tz.to_utc(start), self.server_tz.to_utc(end)

    def tradeable_reason(self, moment: datetime) -> str | None:
        """None if new orders may be placed at `moment`, else the reason they may not."""
        require_utc(moment, "moment")
        self.validate()
        closure = self._closure_at(moment)
        if closure:
            return f"{self.symbol} closed (scheduled closure until {closure[1].isoformat()})"
        for constraint in self.constraints:
            reason = constraint.tradeable_reason(moment)
            if reason:
                return f"{reason} (broker/constraint calendar)"
        if self.always_open:
            return None
        if not self.is_open(moment):
            return f"{self.symbol} session closed at server time {self.server_tz.to_server(moment).isoformat()}"
        start, end = self._period(moment)
        for c_start, c_end in self.closures:  # closures truncate the period
            if start <= c_start < end and c_start > moment:
                end = c_start
            if start < c_end <= moment:
                start = c_end
        if moment - start < self.open_buffer:
            return f"{self.symbol} within {self.open_buffer} of session open"
        if end - moment <= self.close_buffer:
            return f"{self.symbol} within {self.close_buffer} of session close"
        return None


def _weekdays(start_minute: int, end_minute: int, days=range(5)) -> tuple[SessionWindow, ...]:
    return tuple(SessionWindow(day, start_minute, end_minute) for day in days)


# Template for NY-close brokers: Mon-Fri 01:00-24:00 server time = Sun 18:00 to Fri 17:00 New York,
# with the daily 17:00-18:00 NY break. CONFIRM against your broker's XAUUSD contract specification.
XAUUSD_NY_CLOSE_TEMPLATE = TradingCalendar(symbol="XAUUSD", server_tz=NY_CLOSE, windows=_weekdays(60, DAY_MINUTES))

def _ny_market_windows() -> tuple[SessionWindow, ...]:
    """Gold (COMEX/Globex and OTC spot) market hours in New York local time:
    Sunday 18:00 -> Friday 17:00 with a daily 17:00-18:00 break."""
    windows = [SessionWindow(6, 18 * 60, DAY_MINUTES)]
    for day in range(4):  # Mon-Thu
        windows += [SessionWindow(day, 0, 17 * 60), SessionWindow(day, 18 * 60, DAY_MINUTES)]
    windows.append(SessionWindow(4, 0, 17 * 60))
    return tuple(windows)


# Broker-independent XAUUSD market hours defined in New York time (DST handled by the US rule). Preferred over
# server-time templates, which are wrong in US/EU DST-mismatch weeks for EET-based brokers.
XAUUSD_NY_MARKET_HOURS = TradingCalendar(symbol="XAUUSD", server_tz=NEW_YORK, windows=_ny_market_windows(),
                                         risk_day_shift=timedelta(hours=7))

BTCUSDT_ALWAYS_OPEN = TradingCalendar(symbol="BTCUSDT", server_tz=ServerTimeZone("UTC", timedelta(0)),
                                      windows=(), open_buffer=timedelta(0), close_buffer=timedelta(0),
                                      always_open=True, verification="broker_verified")  # 24/7 venue


def validate_session_bars(bars: Sequence[Bar], calendar: TradingCalendar) -> list[tuple[datetime, datetime]]:
    """Every bar must open inside a session, and every gap must be fully explained by closed time.

    Returns the (explained) gaps. An unexplained gap means missing data during an open session.
    """
    calendar.validate()
    gaps = validate_series(bars, allow_gaps=True)
    for bar in bars:
        if bar.symbol != calendar.symbol:
            raise ValidationError("Bars and calendar are for different symbols")
        if not calendar.is_open(bar.open_time):
            raise ValidationError(f"Bar at {bar.open_time.isoformat()} lies outside the trading session")
    step = INTERVALS[bars[0].interval]
    for gap_start, gap_end in gaps:
        moment = gap_start
        while moment < gap_end:
            if calendar.is_open(moment):
                raise ValidationError(f"Unexplained missing bar at {moment.isoformat()} during open session")
            moment += step
    return gaps
