"""XAUUSD scalp v2: three independently defined hypotheses, implemented exactly as pre-registered in
research/forward_test/xau_scalp_v2_preregistration.json (commit c766013). Causal: every rule uses completed candles;
candidates are emitted at the trigger bar's close and filled by the simulator at the next 1m open.

The stop multiple `m` (x ATR14 of completed 5m bars) is the only research variable; everything else is fixed.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from trading.mtf.features import atr, edge, ema, rolling_max_prev, rolling_min
from trading.mtf.frames import Frame, align, take
from trading.mtf.strategies import CANDIDATE_COLUMNS

RR, MIN_NET_RR, MAX_HOLD = 2.0, 1.5, 240
ENTRY_HOURS = (7, 20)
NS_H = 3_600_000_000_000


def _hour(ns: np.ndarray) -> np.ndarray:
    return (ns // NS_H) % 24


def _frame(df: Frame):
    return (df.df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))


def _out(time_ns, side, entry, stop, a, tag) -> pd.DataFrame:
    if len(time_ns) == 0:
        return pd.DataFrame(columns=CANDIDATE_COLUMNS)
    df = pd.DataFrame({"time_ns": time_ns, "side": side, "entry_ref": entry, "stop": stop, "rr": RR,
                       "min_net_rr": MIN_NET_RR, "max_hold_minutes": MAX_HOLD, "atr": a, "tag": tag})
    dist = (df.entry_ref - df.stop) * df.side
    df = df[np.isfinite(df.stop) & np.isfinite(df.atr) & (dist > 0)]
    return df.sort_values("time_ns", kind="stable").reset_index(drop=True)


def _window(ns: np.ndarray, hours=ENTRY_HOURS) -> np.ndarray:
    h = _hour(ns)
    return (h >= hours[0]) & (h < hours[1])


def _mask_out(frame: Frame, long_mask, short_mask, a, m, tag, extra_stop_long=None, extra_stop_short=None):
    c = frame.df["close"].to_numpy(dtype=float)
    rows = []
    for side, mask in ((1, long_mask), (-1, short_mask)):
        idx = np.flatnonzero(mask & np.isfinite(a))
        stop = c[idx] - side * m * a[idx]
        extra = extra_stop_long if side == 1 else extra_stop_short
        if extra is not None:  # the farther of the structural and the volatility stop
            stop = np.minimum(stop, extra[idx]) if side == 1 else np.maximum(stop, extra[idx])
        rows.append(_out(frame.close_ns[idx], side, c[idx], stop, a[idx], tag))
    return pd.concat(rows, ignore_index=True).sort_values("time_ns", kind="stable").reset_index(drop=True)


# --------------------------------------------------------------------------- H1 compression -> expansion

def h1_compression_expansion(frames: dict[str, Frame], m: float) -> pd.DataFrame:
    f1, f5, f1h = frames["1m"], frames["5m"], frames["1h"]
    _, h5, l5, c5 = _frame(f5)
    a14, a100 = atr(h5, l5, c5, 14), atr(h5, l5, c5, 100)
    box_hi, box_lo = -rolling_min(-h5, 12), rolling_min(l5, 12)
    ok5 = (a14 / a100 <= 0.75) & (box_hi - box_lo <= 2.0 * a100)
    e50 = ema(f1h.df["close"].to_numpy(dtype=float), 50)
    slope = e50 - np.concatenate([np.full(3, np.nan), e50[:-3]])
    i5, ih = align(f1, f5), align(f1, f1h)
    bh, bl, a, ok = take(box_hi, i5), take(box_lo, i5), take(a14, i5), take(ok5.astype(float), i5, 0) == 1
    sl = take(slope, ih)
    c1 = f1.df["close"].to_numpy(dtype=float)
    win = _window(f1.close_ns)
    long_mask = edge(c1 > bh + 0.10 * a) & ok & (sl >= 0) & win
    short_mask = edge(c1 < bl - 0.10 * a) & ok & (sl <= 0) & win
    return _mask_out(f1, long_mask, short_mask, a, m, f"H1_m{m}")


# --------------------------------------------------------------------------- H2 trend pullback continuation

def h2_trend_pullback(frames: dict[str, Frame], m: float) -> pd.DataFrame:
    f5, f15, f1h, f4h = frames["5m"], frames["15m"], frames["1h"], frames["4h"]
    _, h15, l15, c15 = _frame(f15)
    a15 = atr(h15, l15, c15, 14)
    n = len(h15)
    hi_v = np.full(n, np.nan)
    lo_v = np.full(n, np.nan)
    up = np.zeros(n, dtype=bool)
    dn = np.zeros(n, dtype=bool)
    if n >= 12:
        wh, wl = sliding_window_view(h15, 12), sliding_window_view(l15, 12)
        ah, al = wh.argmax(axis=1), wl.argmin(axis=1)
        H, L = wh.max(axis=1), wl.min(axis=1)
        big = (H - L) >= 2.5 * a15[11:]
        hi_v[11:], lo_v[11:] = H, L
        up[11:] = big & (al < ah)
        dn[11:] = big & (ah < al)
    c1h = f1h.df["close"].to_numpy(dtype=float)
    e1h = ema(c1h, 50)
    rising = e1h > np.concatenate([np.full(3, np.nan), e1h[:-3]])
    falling = e1h < np.concatenate([np.full(3, np.nan), e1h[:-3]])
    c4h = f4h.df["close"].to_numpy(dtype=float)
    e4h = ema(c4h, 50)
    tl = (c1h > e1h) & rising
    ts = (c1h < e1h) & falling
    _, h5, l5, c5 = _frame(f5)
    a5 = atr(h5, l5, c5, 14)
    i15, ih, i4 = align(f5, f15), align(f5, f1h), align(f5, f4h)
    H, L = take(hi_v, i15), take(lo_v, i15)
    R = H - L
    upa, dna = take(up.astype(float), i15, 0) == 1, take(dn.astype(float), i15, 0) == 1
    tla, tsa = take(tl.astype(float), ih, 0) == 1, take(ts.astype(float), ih, 0) == 1
    c4a, e4a = take(c4h, i4), take(e4h, i4)
    win = _window(f5.close_ns)
    pull_l = (l5 <= H - 0.382 * R) & (l5 >= H - 0.618 * R) & (c5 > H - 0.618 * R)
    pull_s = (h5 >= L + 0.382 * R) & (h5 <= L + 0.618 * R) & (c5 < L + 0.618 * R)
    trig_l = edge(c5 > rolling_max_prev(h5, 3))
    trig_s = edge(c5 < -rolling_max_prev(-l5, 3))
    long_mask = trig_l & pull_l & upa & tla & (c4a > e4a) & win
    short_mask = trig_s & pull_s & dna & tsa & (c4a < e4a) & win
    return _mask_out(f5, long_mask, short_mask, a5, m, f"H2_m{m}")


# --------------------------------------------------------------------------- H3 liquidity sweep -> displacement

def h3_sweep_displacement(frames: dict[str, Frame], m: float) -> pd.DataFrame:
    f1, f5 = frames["1m"], frames["5m"]
    t1 = f1.open_time
    day1 = t1 // (24 * NS_H)
    asia = _hour(t1) < 7
    d = pd.DataFrame({"day": day1[asia], "h": f1.df["high"].to_numpy(dtype=float)[asia],
                      "l": f1.df["low"].to_numpy(dtype=float)[asia]}).groupby("day").agg(hi=("h", "max"), lo=("l", "min"))
    o5, h5, l5, c5 = _frame(f5)
    a5 = atr(h5, l5, c5, 14)
    day5 = f5.open_time // (24 * NS_H)
    ahi = d["hi"].reindex(day5).to_numpy()
    alo = d["lo"].reindex(day5).to_numpy()
    close_h = _hour(f5.close_ns)
    in_sweep = (close_h >= 7) & (close_h < 16)
    sweep_s = in_sweep & (h5 > ahi + 0.10 * a5) & (c5 < ahi)
    sweep_l = in_sweep & (l5 < alo - 0.10 * a5) & (c5 > alo)
    n = len(c5)
    entries = {1: [], -1: []}
    used: set = set()
    for side, sweeps in ((-1, np.flatnonzero(sweep_s)), (1, np.flatnonzero(sweep_l))):
        for j in sweeps:
            if (day5[j], side) in used or not np.isfinite(a5[j]):
                continue
            for k in range(j + 1, min(j + 4, n)):
                if day5[k] != day5[j] or not np.isfinite(a5[k]):
                    break
                body = (o5[k] - c5[k]) * (1 if side == -1 else -1)
                beyond = c5[k] < l5[j] if side == -1 else c5[k] > h5[j]
                if body >= 1.0 * a5[k] and beyond:
                    structural = h5[j] + 0.10 * a5[j] if side == -1 else l5[j] - 0.10 * a5[j]
                    entries[side].append((k, structural))
                    used.add((day5[j], side))
                    break
    rows = []
    for side, items in entries.items():
        if not items:
            continue
        idx = np.array([k for k, _ in items])
        structural = np.array([s for _, s in items])
        ok = _window(f5.close_ns[idx])
        idx, structural = idx[ok], structural[ok]
        vol_stop = c5[idx] - side * m * a5[idx]
        stop = np.maximum(vol_stop, structural) if side == -1 else np.minimum(vol_stop, structural)
        rows.append(_out(f5.close_ns[idx], side, c5[idx], stop, a5[idx], f"H3_m{m}"))
    if not rows:
        return pd.DataFrame(columns=CANDIDATE_COLUMNS)
    return pd.concat(rows, ignore_index=True).sort_values("time_ns", kind="stable").reset_index(drop=True)


HYPOTHESES = {"H1_compression_expansion": h1_compression_expansion, "H2_trend_pullback_continuation": h2_trend_pullback,
              "H3_liquidity_sweep_displacement": h3_sweep_displacement}
