"""Experimental Strategy B4H (scalp4h-v1): 4H-led scalping, research only, no orders.

A new, separately versioned strategy. The previously tested H1-led scalper (`strategies.scalp_candidates`,
tag "B") is not modified and remains the baseline.

Hierarchy (long; short mirrored where the venue allows it). Every input is a completed candle (`frames.align`):
  4H directional bias (mandatory): C4 > EMA(dir_ema) and EMA(dir_ema) > the same EMA `dir_slope_bars` 4H candles
                    earlier. This is the same definition as Strategy A's 4H filter.
  1H structure and momentum (h1_confirm=True): fractal structure trend (k = struct_k) == +1 and C1 > EMA(h1_ema).
                    Confirms the 4H bias and never replaces it. With h1_confirm=False the 1H level is skipped
                    (ablation that isolates the 4H filter).
  15M context     : C15 > EMA(ctx_ema)
  5M setup        : pullback  min(L over last pull_bars 5M) <= EMA(pull_ema) + pull_atr * ATR5
  1M entry        : edge of [C1m > max(H over previous trigger_bars 1M) and C1m > EMA(trigger_ema)]
  stop / target   : below the 5M pullback low minus stop_buffer_atr * ATR5 (>= min_stop_atr * ATR5 away);
                    target = reward_to_risk * risk; refused at the fill unless net RR after costs >= min_net_rr.
The 15M/5M/1M levels, stop, target and time limit are the same as the baseline, so a comparison isolates the
directional hierarchy.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from trading.mtf.features import edge, ema, rolling_max_prev, structure_trend
from trading.mtf.frames import Frame, align, take
from trading.mtf.strategies import _candidates, _direction, _pullback

STRATEGY_VERSION = "scalp4h-v1"
TAG = "B4H"


@dataclass(frozen=True)
class Scalp4HParams:
    dir_ema: int = 50
    dir_slope_bars: int = 3
    h1_confirm: bool = True
    struct_k: int = 2
    h1_ema: int = 20
    ctx_ema: int = 20
    pull_ema: int = 20
    pull_bars: int = 3
    pull_atr: float = 0.25
    atr_n: int = 14
    trigger_bars: int = 5
    trigger_ema: int = 20
    stop_buffer_atr: float = 0.10
    min_stop_atr: float = 0.5
    reward_to_risk: float = 1.5
    min_net_rr: float = 1.5
    max_hold_minutes: int = 60


def h1_confirmation(f1h: Frame, struct_k: int, h1_ema: int) -> np.ndarray:
    """+1 / -1 / 0 per 1H candle: fractal structure trend agrees with the close's side of EMA(h1_ema)."""
    h, low, c = (f1h.df[k].to_numpy() for k in ("high", "low", "close"))
    st = structure_trend(h, low, struct_k)
    e = ema(c, h1_ema)
    return np.where((st == 1) & (c > e), 1, np.where((st == -1) & (c < e), -1, 0))


def scalp4h_candidates(frames: dict[str, Frame], p: Scalp4HParams, *, allow_short: bool) -> pd.DataFrame:
    f4, f1h, f15, f5, f1 = frames["4h"], frames["1h"], frames["15m"], frames["5m"], frames["1m"]
    d4 = take(_direction(f4, p.dir_ema, p.dir_slope_bars).astype(float), align(f1, f4), 0)
    if p.h1_confirm:
        h1 = take(h1_confirmation(f1h, p.struct_k, p.h1_ema).astype(float), align(f1, f1h), 0)
    else:
        h1 = d4  # level skipped: always agrees with the 4H bias
    c15 = f15.df["close"].to_numpy()
    ctx = take(np.sign(c15 - ema(c15, p.ctx_ema)), align(f1, f15), 0)
    pl, ps, stop_l, stop_s, a5 = _pullback(f5, p.pull_ema, p.pull_bars, p.pull_atr, p.atr_n, p.stop_buffer_atr)
    i5 = align(f1, f5)
    c1, hi1, lo1 = (f1.df[k].to_numpy() for k in ("close", "high", "low"))
    e1 = ema(c1, p.trigger_ema)
    up = edge((c1 > rolling_max_prev(hi1, p.trigger_bars)) & (c1 > e1))
    down = edge((c1 < -rolling_max_prev(-lo1, p.trigger_bars)) & (c1 < e1))
    long_mask = up & (d4 == 1) & (h1 == 1) & (ctx == 1) & (take(pl.astype(float), i5, 0) == 1)
    short_mask = down & (d4 == -1) & (h1 == -1) & (ctx == -1) & (take(ps.astype(float), i5, 0) == 1)
    return _candidates(f1, long_mask, short_mask, take(stop_l, i5), take(stop_s, i5), take(a5, i5), p, TAG,
                       p.min_stop_atr, allow_short)
