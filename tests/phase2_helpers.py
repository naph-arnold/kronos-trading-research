"""SYNTHETIC data generators for Phase 2 tests only. Never used as market evidence."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from trading.marketdata import Bar
from trading.sessions import XAUUSD_NY_CLOSE_TEMPLATE


def random_walk(symbol: str, interval: str, start: datetime, count: int, *, price: float, vol: float,
                seed: int = 7, calendar=None, step: timedelta | None = None) -> list[Bar]:
    rng = random.Random(seed)
    step = step or {"15m": timedelta(minutes=15), "1h": timedelta(hours=1)}[interval]
    bars, moment, last = [], start, price
    while len(bars) < count:
        if calendar is None or calendar.is_open(moment):
            drift = rng.gauss(0, vol)
            o = last
            c = max(o * (1 + drift), 1.0)
            h = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
            lo = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
            q = D("0.01")
            bars.append(Bar.create(symbol=symbol, interval=interval, open_time=moment,
                                   open=D(repr(o)).quantize(q), high=D(repr(h)).quantize(q),
                                   low=D(repr(lo)).quantize(q), close=D(repr(c)).quantize(q),
                                   volume=D(str(rng.randint(50, 500)))))
            last = float(bars[-1].close)
        moment += step
    # Repair any quantization inconsistencies (keep OHLC valid).
    fixed = []
    for b in bars:
        hi = max(b.high, b.open, b.close)
        lo = min(b.low, b.open, b.close)
        fixed.append(Bar(b.symbol, b.interval, b.open_time, b.open, hi, lo, b.close, b.volume))
    return fixed


def xau_m15(count: int = 3000, seed: int = 11, start=datetime(2026, 9, 7, 0, tzinfo=timezone.utc)):
    return random_walk("XAUUSD", "15m", start, count, price=2650.0, vol=0.0012, seed=seed,
                       calendar=XAUUSD_NY_CLOSE_TEMPLATE)


def btc_h1(count: int = 1500, seed: int = 5, start=datetime(2026, 1, 1, tzinfo=timezone.utc)):
    return random_walk("BTCUSDT", "1h", start, count, price=60000.0, vol=0.006, seed=seed)
