"""Value-area strategies on completed candles (research hypotheses, NOT validated strategies; no orders).

Levels: for a signal candle that opens at t, the levels are those of the most recent COMPLETED profile period
that ended at or before t (frozen after that period closed; `profile.levels_at`). The levels never change during
a candle, and every rule below uses only candles that have closed. Each new profile period ("level set") resets
all state; at most one signal per side per level set.

Strategy A - value-area rejection (short side at VAH; long side mirrored at VAL):
1. Excursion: some candle within the last `max_excursion_bars` (same level set) traded above VAH (high > VAH).
2. Return: the current completed candle closes back INSIDE the value area (VAL < close < VAH) and either it
   traded above VAH itself or the previous candle closed at/above VAH (the return happens now).
3. Entry: next execution candle's open (simulator). Stop: the excursion extreme (highest high of the excursion
   window) + stop_buffer_atr * ATR. Target: POC (`target="poc"`) or the opposite boundary VAL
   (`target="opposite"`); the target must lie beyond the entry in the trade direction.
Strategy B - value-area breakout and retest (long side above VAH; short side mirrored below VAL):
1. Break: a completed candle closes above VAH after the previous candle (same level set) closed at/below it.
2. Retest: within `retest_bars` candles after the break, a candle's low comes back to VAH + retest_tol_atr*ATR
   while still closing above VAH. A close back at/below VAH before that cancels the setup (invalidation).
3. Confirmation: the NEXT completed candle also closes above VAH (price remains outside); otherwise invalidated.
4. Entry next open; stop = lowest low of the retest and confirmation candles - stop_buffer_atr*ATR; target =
   entry + reward_to_risk * risk.
Both: optional 4H directional confirmation (completed 4H candles; close vs EMA(50) with EMA slope over 3 4H
candles, as in trading.mtf.strategies._direction): longs need +1, shorts -1. Every candidate must also pass the
simulator's net reward-to-risk after costs (min_net_rr) and the unchanged risk engine.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from trading.mtf.features import atr
from trading.mtf.frames import Frame, align, take
from trading.mtf.strategies import _direction

TAGS = {"rejection": "VPA", "breakout": "VPB"}


@dataclass(frozen=True)
class VPParams:
    strategy: str = "rejection"          # "rejection" | "breakout"
    profile_period: str = "day"          # "day" | "session" (mapped per market to period kinds)
    va_pct: float = 0.70
    bin_bps: float = 2.0
    signal_tf: str = "5m"
    htf_filter: bool = False             # 4H directional confirmation
    level_source: str = "volume"         # "volume" (market's weight kind) | "tpo" | "range" (controls)
    target: str = "poc"                  # rejection: "poc" | "opposite"
    reward_to_risk: float = 2.0          # breakout target in R
    max_excursion_bars: int = 12
    retest_bars: int = 12
    retest_tol_atr: float = 0.25
    stop_buffer_atr: float = 0.10
    min_stop_atr: float = 0.5
    atr_n: int = 14
    dir_ema: int = 50
    dir_slope_bars: int = 3
    min_net_rr: float = 1.0
    max_hold_minutes: int = 240


FEATURES = ("va_width_atr", "dist_poc_atr", "profile_total", "level_pid")


def _rows(out: list, i, side, entry, stop, rr, p: VPParams, a, lv, tag):
    out.append((i, side, entry, stop, rr, a, (lv["vah"][i] - lv["val"][i]) / a, abs(entry - lv["poc"][i]) / a,
                lv["total"][i], int(lv["pid"][i])))


def value_area_candidates(sig: Frame, lv: dict, p: VPParams, *, allow_short: bool, d4: np.ndarray | None = None) -> pd.DataFrame:
    """Candidates on the signal frame. `lv` = levels per signal candle (keys vah, val, poc, total, pid) taken at the
    candle's OPEN time; `d4` = 4H direction aligned to the signal candles (required when htf_filter)."""
    o = sig.df
    h, low, c = (o[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
    a = atr(h, low, c, p.atr_n)
    n = len(c)
    if p.htf_filter and d4 is None:
        raise ValueError("htf_filter needs the aligned 4H direction")
    allow_l = (d4 == 1) if p.htf_filter else np.ones(n, bool)
    allow_s = ((d4 == -1) if p.htf_filter else np.ones(n, bool)) & allow_short
    pid, vah, val, poc = lv["pid"], lv["vah"], lv["val"], lv["poc"]
    out: list = []
    tag = TAGS[p.strategy]
    cur, set_start = -2, 0
    used_l = used_s = False
    st_l = st_s = 0  # breakout state: 0 idle, 1 broken, 2 retested
    b_l = b_s = r_l = r_s = 0
    for i in range(n):
        if pid[i] != cur:
            cur, set_start = pid[i], i
            used_l = used_s = False
            st_l = st_s = 0
        if pid[i] < 0 or not np.isfinite(a[i]) or a[i] <= 0:
            continue
        H, L = vah[i], val[i]
        prev_ok = i - 1 >= set_start
        if p.strategy == "rejection":
            ws = max(set_start, i - p.max_excursion_bars + 1)
            inside = L < c[i] < H
            if not used_s and allow_s[i] and inside:
                ext = h[ws:i + 1].max()
                if ext > H and (h[i] > H or (prev_ok and c[i - 1] >= H)):
                    tgt = poc[i] if p.target == "poc" else L
                    stop = ext + p.stop_buffer_atr * a[i]
                    if tgt < c[i] and stop - c[i] >= p.min_stop_atr * a[i]:
                        _rows(out, i, -1, c[i], stop, (c[i] - tgt) / (stop - c[i]), p, a[i], lv, tag)
                        used_s = True
            if not used_l and allow_l[i] and inside:
                ext = low[ws:i + 1].min()
                if ext < L and (low[i] < L or (prev_ok and c[i - 1] <= L)):
                    tgt = poc[i] if p.target == "poc" else H
                    stop = ext - p.stop_buffer_atr * a[i]
                    if tgt > c[i] and c[i] - stop >= p.min_stop_atr * a[i]:
                        _rows(out, i, 1, c[i], stop, (tgt - c[i]) / (c[i] - stop), p, a[i], lv, tag)
                        used_l = True
        else:
            # long breakout above VAH
            if not used_l:
                if st_l == 0:
                    if prev_ok and c[i] > H and c[i - 1] <= H:
                        st_l, b_l = 1, i
                elif st_l == 1:
                    if c[i] <= H or i - b_l > p.retest_bars:
                        st_l = 0
                    elif low[i] <= H + p.retest_tol_atr * a[i]:
                        st_l, r_l = 2, i
                elif st_l == 2:
                    if c[i] > H and allow_l[i]:
                        stop = min(low[r_l], low[i]) - p.stop_buffer_atr * a[i]
                        if c[i] - stop >= p.min_stop_atr * a[i]:
                            _rows(out, i, 1, c[i], stop, p.reward_to_risk, p, a[i], lv, tag)
                            used_l = True
                    st_l = 0
            if allow_short and not used_s:
                if st_s == 0:
                    if prev_ok and c[i] < L and c[i - 1] >= L:
                        st_s, b_s = 1, i
                elif st_s == 1:
                    if c[i] >= L or i - b_s > p.retest_bars:
                        st_s = 0
                    elif h[i] >= L - p.retest_tol_atr * a[i]:
                        st_s, r_s = 2, i
                elif st_s == 2:
                    if c[i] < L and allow_s[i]:
                        stop = max(h[r_s], h[i]) + p.stop_buffer_atr * a[i]
                        if stop - c[i] >= p.min_stop_atr * a[i]:
                            _rows(out, i, -1, c[i], stop, p.reward_to_risk, p, a[i], lv, tag)
                            used_s = True
                    st_s = 0
    cols = ["i", "side", "entry_ref", "stop", "rr", "atr", *FEATURES]
    df = pd.DataFrame(out, columns=cols)
    df.insert(0, "time_ns", sig.close_ns[df.pop("i").to_numpy(dtype=int)] if len(df) else np.zeros(0, np.int64))
    df["min_net_rr"] = p.min_net_rr
    df["max_hold_minutes"] = p.max_hold_minutes
    df["tag"] = tag
    return df.sort_values("time_ns", kind="stable").reset_index(drop=True)


def direction_4h(sig: Frame, f4: Frame, p: VPParams) -> np.ndarray:
    return take(_direction(f4, p.dir_ema, p.dir_slope_bars).astype(float), align(sig, f4), 0)
