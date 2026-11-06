"""Causal, incremental technical indicators (shared by both pipelines).

Every indicator is updated with one *closed* bar at a time and only ever sees
past data, so strategies built on them cannot look ahead. Values are None
until the warm-up period is complete. Floats are used for indicator maths;
prices handed to the risk engine are converted back to tick-aligned Decimals.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from trading.marketdata import INTERVALS, Bar


class EMA:
    """Exponential moving average seeded with the simple average of the first `period` values."""

    def __init__(self, period: int):
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period, self.alpha = period, 2.0 / (period + 1)
        self._seed: list[float] = []
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
        else:
            self.value += self.alpha * (x - self.value)
        return self.value


class WilderRMA:
    def __init__(self, period: int):
        self.period = period
        self._seed: list[float] = []
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
        else:
            self.value = (self.value * (self.period - 1) + x) / self.period
        return self.value


class ATR:
    def __init__(self, period: int = 14):
        self._rma = WilderRMA(period)
        self._prev_close: float | None = None
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        tr = high - low if self._prev_close is None else max(
            high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        self.value = self._rma.update(tr)
        return self.value


class ADX:
    """Wilder's ADX (+DI/-DI smoothed, DX smoothed)."""

    def __init__(self, period: int = 14):
        self.period = period
        self._tr, self._plus, self._minus = WilderRMA(period), WilderRMA(period), WilderRMA(period)
        self._dx = WilderRMA(period)
        self._prev: tuple[float, float, float] | None = None
        self.value: float | None = None
        self.plus_di: float | None = None
        self.minus_di: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._prev is None:
            self._prev = (high, low, close)
            return None
        ph, pl, pc = self._prev
        self._prev = (high, low, close)
        up, down = high - ph, pl - low
        plus_dm = up if up > down and up > 0 else 0.0
        minus_dm = down if down > up and down > 0 else 0.0
        tr = max(high - low, abs(high - pc), abs(low - pc))
        tr_s, p_s, m_s = self._tr.update(tr), self._plus.update(plus_dm), self._minus.update(minus_dm)
        if tr_s is None or not tr_s:
            return self.value
        self.plus_di, self.minus_di = 100 * p_s / tr_s, 100 * m_s / tr_s
        total = self.plus_di + self.minus_di
        dx = 0.0 if total == 0 else 100 * abs(self.plus_di - self.minus_di) / total
        self.value = self._dx.update(dx)
        return self.value


class RollingWindow:
    def __init__(self, size: int):
        self.values: deque = deque(maxlen=size)

    def update(self, x) -> None:
        self.values.append(x)

    @property
    def full(self) -> bool:
        return len(self.values) == self.values.maxlen


@dataclass(frozen=True)
class Swing:
    kind: str  # "high" or "low"
    price: float
    time: datetime  # open time of the pivot bar
    confirmed_at: datetime  # close time of the bar that confirmed it (no lookahead before this)


class SwingDetector:
    """Fractal pivots confirmed only after `right` later bars have closed.

    A pivot high at bar i is known at the close of bar i+right, never earlier.
    """

    def __init__(self, left: int = 3, right: int = 3):
        self.left, self.right = left, right
        self._bars: deque = deque(maxlen=left + right + 1)

    def update(self, bar: Bar) -> list[Swing]:
        self._bars.append(bar)
        if len(self._bars) < self._bars.maxlen:
            return []
        bars = list(self._bars)
        pivot = bars[self.left]
        others = bars[: self.left] + bars[self.left + 1:]
        found = []
        if all(pivot.high > b.high for b in others):
            found.append(Swing("high", float(pivot.high), pivot.open_time, bar.close_time))
        if all(pivot.low < b.low for b in others):
            found.append(Swing("low", float(pivot.low), pivot.open_time, bar.close_time))
        return found


class Resampler:
    """Aggregates closed lower-timeframe bars into closed higher-timeframe bars.

    A higher-timeframe bar is emitted only when its final constituent bar has
    closed, so H1 context derived from M15 never includes an unfinished hour.
    Incomplete buckets (gaps) are emitted only if `allow_partial`.
    """

    def __init__(self, interval: str, *, allow_partial: bool = True):
        self.interval, self.step = interval, INTERVALS[interval]
        self.allow_partial = allow_partial
        self._bucket: list[Bar] = []

    def _bucket_start(self, moment: datetime) -> datetime:
        epoch = datetime(1970, 1, 1, tzinfo=moment.tzinfo)
        return moment - (moment - epoch) % self.step

    def _emit(self) -> Bar | None:
        bars, self._bucket = self._bucket, []
        if not bars:
            return None
        return Bar(bars[0].symbol, self.interval, self._bucket_start(bars[0].open_time), bars[0].open,
                   max(b.high for b in bars), min(b.low for b in bars), bars[-1].close,
                   sum((b.volume for b in bars), Decimal("0")))

    def update(self, bar: Bar) -> list[Bar]:
        """Completed higher-timeframe bars finished (or skipped past) by this closed bar, oldest first."""
        emitted = []
        if self._bucket and self._bucket_start(bar.open_time) != self._bucket_start(self._bucket[0].open_time):
            # Previous bucket ended without its last bar (gap): emit as partial.
            partial = self._emit()
            if self.allow_partial and partial is not None:
                emitted.append(partial)
        self._bucket.append(bar)
        if bar.close_time == self._bucket_start(bar.open_time) + self.step:
            emitted.append(self._emit())
        return emitted


def round_price(value: float, tick: Decimal, *, up: bool) -> Decimal:
    """Snap a float price to the tick grid in the conservative direction."""
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    return (Decimal(repr(value)) / tick).to_integral_value(rounding=rounding) * tick


def minutes_of_day(moment: datetime) -> int:
    return moment.hour * 60 + moment.minute


ONE_DAY = timedelta(days=1)
