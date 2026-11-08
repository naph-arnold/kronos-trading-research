"""Strategy A (selective, 4H -> 1H -> 15M -> 5M) as a shadow-engine strategy for XAUUSD M5 bars.

The research generator `selective_candidates` is reused unchanged. On each closed 5M bar the adapter
recomputes the candidates over a trailing window of closed bars (`window_days`, default 60 calendar days, so
the 4H EMA(50) is fully converged) and emits a signal only when a candidate is anchored at THIS bar's close.

Semantics kept identical to the research simulator (`trading.mtf.simulator`):
* entry reference = the signal bar's close; stop = the candidate stop; both snapped to the tick grid in the
  conservative direction; target = entry + reward_to_risk * (entry - stop), snapped toward entry;
* the planned reward-to-risk AFTER estimated costs (spread at the fill tick, slippage allowance, commission)
  must be >= min_net_rr, checked at the simulated fill by `net_rr_check` (an engine entry check that can only
  reject);
* the time limit (`max_hold_minutes`) maps to the engine's max holding bars.

Causality: the adapter only sees `HistoryView` (closed bars); higher timeframes drop the still-forming bucket.
A cheap exact pre-check (the 5M trigger of the same window, evaluated for the last bar only)
skips the full computation on most bars; it never changes the result.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal

import numpy as np
import pandas as pd

from trading.common import MARKET_SYMBOLS, Market, Side
from trading.indicators import round_price
from trading.mtf.features import ema
from trading.mtf.frames import Frame, resample
from trading.mtf.strategies import SelectiveParams, selective_candidates
from trading.signals import TradeSignal
from trading.strategies.base import params_hash

INTERVAL = "5m"
STEP = timedelta(minutes=5)


class SelectiveAShadow:
    strategy_id = "xau-mtf-selective-a"
    version = "1.0.0"
    market = Market.XAUUSD_MT5

    def __init__(self, params: SelectiveParams, *, tick_size: Decimal, window_days: int = 60,
                 allow_short: bool = True, dataset_id: str = "unspecified"):
        if window_days < 45:
            raise ValueError("window_days must be >= 45 so the 4H EMA is converged")
        self.params = params
        self.tick = tick_size
        self.window = timedelta(days=window_days)
        self.allow_short = allow_short
        self.dataset_id = dataset_id
        self.signals_emitted = 0
        self.full_evaluations = 0
        self._open: list = []  # cached closed-bar arrays, append-only
        self._ohlc: list[tuple[float, float, float, float]] = []

    # -- cache -------------------------------------------------------------------------------
    def _sync(self, history) -> None:
        n = len(history)
        if len(self._open) > n or (self._open and history[len(self._open) - 1].open_time != self._open[-1]):
            self._open, self._ohlc = [], []  # history is not an extension of the cache: rebuild
        for j in range(len(self._open), n):
            b = history[j]
            if b.interval != INTERVAL:
                raise ValueError(f"Strategy A shadow expects {INTERVAL} bars, got {b.interval}")
            self._open.append(b.open_time)
            self._ohlc.append((float(b.open), float(b.high), float(b.low), float(b.close)))

    def _start_index(self, close_time) -> int:
        start = np.datetime64((close_time - self.window).replace(tzinfo=None), "ns")
        lo, hi = 0, len(self._open)
        while lo < hi:  # first cached bar opening at or after the window start
            mid = (lo + hi) // 2
            if np.datetime64(self._open[mid].replace(tzinfo=None), "ns") < start:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _trigger_now(self, i0: int) -> bool:
        """Exactly the 5M trigger of `selective_candidates` (edge of breakout & close vs EMA) on the same window,
        evaluated for the last bar only. A necessary condition for any candidate at this bar."""
        m = self.params.confirm_bars
        arr = np.asarray(self._ohlc[i0:], dtype=float)
        if len(arr) <= m + 1:
            return False
        c, h, low = arr[:, 3], arr[:, 1], arr[:, 2]
        e = ema(c, self.params.confirm_ema)
        hi_prev = np.array([h[-m - 1:-1].max(), h[-m - 2:-2].max()])
        lo_prev = np.array([low[-m - 1:-1].min(), low[-m - 2:-2].min()])
        last, prev = (c[-1], e[-1]), (c[-2], e[-2])
        up_now, up_prev = last[0] > hi_prev[0] and last[0] > last[1], prev[0] > hi_prev[1] and prev[0] > prev[1]
        dn_now, dn_prev = last[0] < lo_prev[0] and last[0] < last[1], prev[0] < lo_prev[1] and prev[0] < prev[1]
        return bool((up_now and not up_prev) or (self.allow_short and dn_now and not dn_prev))

    def frames(self, close_time) -> dict[str, Frame]:
        i0 = self._start_index(close_time)
        arr = np.asarray(self._ohlc[i0:], dtype=float)
        idx = pd.DatetimeIndex(self._open[i0:]).tz_convert("UTC")
        df = pd.DataFrame(arr, columns=["open", "high", "low", "close"], index=idx).assign(volume=0.0)
        df.index.name = "open_time"
        base = Frame(INTERVAL, df)
        return {INTERVAL: base, **{iv: resample(base, iv) for iv in ("15m", "1h", "4h")}}

    # -- strategy ------------------------------------------------------------------------------
    def __call__(self, history) -> TradeSignal | None:
        self._sync(history)
        bar = history[-1]
        if not self._trigger_now(self._start_index(bar.close_time)):
            return None
        frames = self.frames(bar.close_time)
        if len(frames["4h"]) < self.params.dir_ema:  # 4H EMA undefined -> direction 0 -> no candidate possible
            return None
        self.full_evaluations += 1
        cands = selective_candidates(frames, self.params, allow_short=self.allow_short)
        close_ns = int(pd.Timestamp(bar.close_time).value)
        rows = cands[cands.time_ns == close_ns]
        if rows.empty:
            return None
        row = rows.iloc[0]
        long = int(row.side) == 1
        side = Side.LONG if long else Side.SHORT
        entry = round_price(float(row.entry_ref), self.tick, up=not long)
        stop = round_price(float(row.stop), self.tick, up=not long)
        risk = abs(float(entry) - float(stop))
        target = round_price(float(entry) + int(row.side) * float(row.rr) * risk, self.tick, up=not long)
        if risk <= 0 or (long and not stop < entry < target) or (not long and not target < entry < stop):
            return None
        self.signals_emitted += 1
        return TradeSignal.create(
            signal_id=f"{self.strategy_id}-{bar.close_time:%Y%m%dT%H%M}-{side.value}",
            market=self.market, symbol=MARKET_SYMBOLS[self.market], side=side, entry_price=entry, stop_price=stop,
            take_profit=target, strategy_id=self.strategy_id, strategy_version=self.version,
            generated_at=bar.close_time, bar_close_time=bar.close_time,
            provenance={"dataset_id": self.dataset_id, "interval": INTERVAL,
                        "source_bar_open": bar.open_time.isoformat(), "params_hash": params_hash(self.params),
                        "params": asdict(self.params), "window_days": self.window.days,
                        "context": {"atr15": float(row.atr), "min_net_rr": float(row.min_net_rr)}})


def net_rr_check(min_net_rr: float):
    """Engine entry check: reject unless (target - fill - costs) / (fill - stop fill + costs) >= min_net_rr at the
    actual fill tick, exactly as the research simulator checks it. Can only add a rejection."""
    minimum = Decimal(repr(min_net_rr))

    def check(signal: TradeSignal, tick, instrument, limits) -> str | None:
        if signal.take_profit is None:
            return "Net reward-to-risk check requires a take-profit"
        long = signal.side is Side.LONG
        s = Decimal(1) if long else Decimal(-1)
        slip = limits.slippage_allowance
        fill = tick.ask + slip if long else tick.bid - slip
        stop_fill = signal.stop_price - slip if long else signal.stop_price + slip
        vpu = instrument.value_per_price_unit(Decimal(1))
        reward = (signal.take_profit - fill) * s - instrument.round_trip_cost(Decimal(1), fill, signal.take_profit) / vpu
        loss = (fill - stop_fill) * s + instrument.round_trip_cost(Decimal(1), fill, stop_fill) / vpu
        if loss <= 0 or reward / loss < minimum:
            ratio = "n/a" if loss <= 0 else f"{reward / loss:.3f}"
            return f"Net reward-to-risk after costs below minimum ({ratio} < {minimum})"
        return None

    return check
