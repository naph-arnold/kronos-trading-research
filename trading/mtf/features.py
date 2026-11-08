"""Causal, mathematically defined structure features (all values at index i use candles 0..i only).

* EMA(n):  e_i = a*x_i + (1-a)*e_{i-1}, a = 2/(n+1), seeded with x_0; undefined (NaN) for i < n-1.
* ATR(n):  Wilder smoothing of true range TR_i = max(H_i-L_i, |H_i-C_{i-1}|, |L_i-C_{i-1}|); NaN for i < n.
* Fractal swing high with k bars each side: pivot p is a swing high if H_p = max(H_{p-k..p+k}); it becomes
  KNOWN only at i = p+k (confirmation lag k). Swing lows mirrored.
* Structure trend: with the last two confirmed swing highs SH_prev < SH_last and swing lows SL_prev < SL_last
  -> +1 (higher highs and higher lows); both lower -> -1; otherwise 0.
* Pullback: within the last m candles, min(L) <= EMA + d*ATR (long; mirrored for short).
* Breakout trigger: C_i > max(H_{i-m..i-1}) (long), edge-triggered (true now, false on the previous candle).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(x: np.ndarray, n: int) -> np.ndarray:
    out = pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()
    out[: n - 1] = np.nan
    return out


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> np.ndarray:
    prev = np.concatenate([[np.nan], close[:-1]])
    tr = np.nanmax(np.vstack([high - low, np.abs(high - prev), np.abs(low - prev)]), axis=0)
    out = pd.Series(tr).ewm(alpha=1.0 / n, adjust=False).mean().to_numpy()
    out[:n] = np.nan
    return out


def rolling_max_prev(x: np.ndarray, m: int) -> np.ndarray:
    """max(x[i-m .. i-1]) (excludes the current candle)."""
    return pd.Series(x).shift(1).rolling(m, min_periods=m).max().to_numpy()


def rolling_min(x: np.ndarray, m: int) -> np.ndarray:
    """min(x[i-m+1 .. i]) (includes the current candle)."""
    return pd.Series(x).rolling(m, min_periods=m).min().to_numpy()


def confirmed_swings(high: np.ndarray, low: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Arrays (length n) holding at index i the pivot value confirmed exactly at i (NaN otherwise)."""
    w = 2 * k + 1
    hmax = pd.Series(high).rolling(w, min_periods=w).max().to_numpy()
    lmin = pd.Series(low).rolling(w, min_periods=w).min().to_numpy()
    piv_h = np.full(len(high), np.nan)
    piv_l = np.full(len(low), np.nan)
    idx = np.arange(len(high))
    p = idx - k
    ok = p >= 0
    is_h = ok & (high[np.clip(p, 0, None)] == hmax)
    is_l = ok & (low[np.clip(p, 0, None)] == lmin)
    piv_h[is_h] = high[p[is_h]]
    piv_l[is_l] = low[p[is_l]]
    return piv_h, piv_l


def _last_two(pivots: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    s = pd.Series(pivots)
    last = s.ffill()
    vals = s.dropna()
    prev_vals = vals.shift(1)
    prev = pd.Series(np.nan, index=s.index)
    prev.loc[vals.index] = prev_vals.values
    return last.to_numpy(), prev.ffill().to_numpy()


def structure_trend(high: np.ndarray, low: np.ndarray, k: int) -> np.ndarray:
    ph, pl = confirmed_swings(high, low, k)
    sh, sh_prev = _last_two(ph)
    sl, sl_prev = _last_two(pl)
    up = (sh > sh_prev) & (sl > sl_prev)
    down = (sh < sh_prev) & (sl < sl_prev)
    return np.where(up, 1, np.where(down, -1, 0)).astype(int)


def edge(cond: np.ndarray) -> np.ndarray:
    prev = np.concatenate([[False], cond[:-1]])
    return cond & ~prev
