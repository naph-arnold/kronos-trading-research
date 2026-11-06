"""Canonical OHLCV bars and quotes shared by both markets.

Timestamps are bar *open* times in UTC; a bar is closed at open_time + interval.
Adapters must convert broker server time (MT5) to UTC before building bars.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Sequence

from trading.common import ValidationError, require_utc, to_decimal

INTERVALS = {
    "1m": timedelta(minutes=1), "5m": timedelta(minutes=5), "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30), "1h": timedelta(hours=1), "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Bar:
    symbol: str
    interval: str
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @classmethod
    def create(cls, *, symbol: str, interval: str, open_time: datetime, open: object, high: object,
               low: object, close: object, volume: object = 0) -> "Bar":
        bar = cls(symbol, interval, open_time,
                  to_decimal(open, "open", positive=True), to_decimal(high, "high", positive=True),
                  to_decimal(low, "low", positive=True), to_decimal(close, "close", positive=True),
                  to_decimal(volume, "volume", nonnegative=True))
        bar.validate()
        return bar

    @property
    def close_time(self) -> datetime:
        return self.open_time + INTERVALS[self.interval]

    def validate(self, *, require_alignment: bool = True) -> None:
        if self.interval not in INTERVALS:
            raise ValidationError(f"Unsupported interval {self.interval!r}")
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValidationError("Bar symbol required")
        require_utc(self.open_time, "bar open_time")
        if require_alignment and (self.open_time - _EPOCH) % INTERVALS[self.interval]:
            raise ValidationError(f"Bar open time {self.open_time.isoformat()} not aligned to {self.interval}")
        prices = (self.open, self.high, self.low, self.close)
        if any(not isinstance(p, Decimal) or not p.is_finite() or p <= 0 for p in prices):
            raise ValidationError("Bar prices must be finite positive Decimals")
        if not isinstance(self.volume, Decimal) or not self.volume.is_finite() or self.volume < 0:
            raise ValidationError("Bar volume must be a finite non-negative Decimal")
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close, self.high):
            raise ValidationError("Inconsistent OHLC prices")


def validate_series(bars: Sequence[Bar], *, allow_gaps: bool = False,
                    require_alignment: bool = True) -> list[tuple[datetime, datetime]]:
    """Strictly increasing, single symbol/interval series. Returns gaps found.

    `allow_gaps=True` is for session markets (XAUUSD weekends/maintenance); gaps
    are returned so callers can verify them against a session calendar.
    """
    if not bars:
        raise ValidationError("Empty bar series")
    symbol, interval = bars[0].symbol, bars[0].interval
    gaps = []
    previous = None
    for bar in bars:
        bar.validate(require_alignment=require_alignment)
        if bar.symbol != symbol or bar.interval != interval:
            raise ValidationError("Mixed symbol or interval in series")
        if previous is not None:
            if bar.open_time <= previous.open_time:
                raise ValidationError(f"Duplicate or out-of-order bar at {bar.open_time.isoformat()}")
            if bar.open_time != previous.close_time:
                if bar.open_time < previous.close_time:
                    raise ValidationError("Overlapping bars")
                if not allow_gaps:
                    raise ValidationError(f"Missing bars between {previous.close_time.isoformat()} "
                                          f"and {bar.open_time.isoformat()}")
                gaps.append((previous.close_time, bar.open_time))
        previous = bar
    return gaps


def assert_fresh(bars: Sequence[Bar], *, now: datetime, max_age: timedelta) -> Bar:
    """Return the last bar if it is closed and its close is no older than max_age."""
    require_utc(now, "now")
    if not bars:
        raise ValidationError("No bars available")
    last = bars[-1]
    if last.close_time > now:
        raise ValidationError("Last bar has not closed yet")
    if now - last.close_time > max_age:
        raise ValidationError(f"Stale feed: last bar closed {now - last.close_time} ago")
    return last


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    time: datetime

    @classmethod
    def create(cls, *, symbol: str, bid: object, ask: object, time: datetime) -> "Quote":
        quote = cls(symbol, to_decimal(bid, "bid", positive=True), to_decimal(ask, "ask", positive=True), time)
        quote.validate()
        return quote

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    def validate(self) -> None:
        require_utc(self.time, "quote time")
        if any(not isinstance(p, Decimal) or not p.is_finite() or p <= 0 for p in (self.bid, self.ask)):
            raise ValidationError("Quote prices must be finite positive Decimals")
        if self.ask < self.bid:
            raise ValidationError("Crossed quote (ask < bid)")


def bar_from_research_row(row: dict, *, symbol: str, interval: str) -> Bar:
    """Adapter for rows produced by research.market_data / research.datasets."""
    moment = datetime.fromisoformat(str(row["timestamps"]).replace("Z", "+00:00"))
    return Bar.create(symbol=symbol, interval=interval, open_time=require_utc(moment, "timestamps"),
                      open=row["open"], high=row["high"], low=row["low"], close=row["close"],
                      volume=row.get("volume", 0))
