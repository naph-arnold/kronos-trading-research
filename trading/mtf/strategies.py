"""Strategy A (selective), Strategy B (scalping) and research baselines as causal candidate generators.

A candidate is produced at the close of the confirming candle (`time_ns`) and can be executed no earlier than
the next execution candle's open. Every rule uses completed candles only (see `frames.align`).

Strategy A - selective, 4H -> 1H -> 15M -> 5M (long; short mirrored where the venue allows it):
  4H direction  : C4 > EMA(dir_ema) and EMA(dir_ema) > EMA(dir_ema) `dir_slope_bars` 4H candles earlier
  1H structure  : fractal structure trend (k = struct_k) == +1 and C1 > EMA(struct_ema)
  15M setup     : pullback  min(L over last pull_bars 15M) <= EMA(pull_ema) + pull_atr * ATR15
                  and C15 > EMA(hold_ema) (the deeper 15M trend still holds)
  5M trigger    : edge of [C5 > max(H over previous confirm_bars 5M) and C5 > EMA(confirm_ema)]
  stop          : min(L over last pull_bars 15M) - stop_buffer_atr * ATR15  (>= min_stop_atr * ATR15 away)
  target        : entry + reward_to_risk * (entry - stop); entries are refused unless the planned reward-to-risk
                  after estimated costs is >= min_net_rr (checked at the simulated fill)
Strategy B - scalping, 1H -> 15M -> 5M -> 1M: the same structure one level lower
  1H direction (EMA slope), 15M close vs EMA(struct_ema), 5M pullback to EMA +- ATR5, 1M breakout trigger,
  stop below the 5M pullback low, target reward_to_risk (default 1.5), short time limit.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from trading.mtf.features import atr, edge, ema, rolling_max_prev, rolling_min, structure_trend
from trading.mtf.frames import Frame, align, take

CANDIDATE_COLUMNS = ("time_ns", "side", "entry_ref", "stop", "rr", "min_net_rr", "max_hold_minutes", "atr", "tag")


@dataclass(frozen=True)
class SelectiveParams:
    dir_ema: int = 50
    dir_slope_bars: int = 3
    struct_k: int = 2
    struct_ema: int = 20
    pull_ema: int = 20
    pull_bars: int = 4
    pull_atr: float = 0.25
    hold_ema: int = 50
    atr_n: int = 14
    confirm_bars: int = 6
    confirm_ema: int = 20
    stop_buffer_atr: float = 0.10
    min_stop_atr: float = 0.5
    reward_to_risk: float = 2.0
    min_net_rr: float = 2.0
    max_hold_minutes: int = 720


@dataclass(frozen=True)
class ScalpParams:
    dir_ema: int = 50
    dir_slope_bars: int = 3
    struct_ema: int = 20
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


def _direction(f: Frame, n: int, slope_bars: int) -> np.ndarray:
    c = f.df["close"].to_numpy()
    e = ema(c, n)
    prev = np.concatenate([np.full(slope_bars, np.nan), e[:-slope_bars]])
    return np.where((c > e) & (e > prev), 1, np.where((c < e) & (e < prev), -1, 0))


def _pullback(f: Frame, ema_n: int, bars: int, depth_atr: float, atr_n: int, buffer_atr: float):
    h, low, c = (f.df[k].to_numpy() for k in ("high", "low", "close"))
    e, a = ema(c, ema_n), atr(h, low, c, atr_n)
    lo, hi = rolling_min(low, bars), -rolling_min(-h, bars)
    pull_long = lo <= e + depth_atr * a
    pull_short = hi >= e - depth_atr * a
    return pull_long, pull_short, lo - buffer_atr * a, hi + buffer_atr * a, a


def _candidates(trig_frame: Frame, long_mask, short_mask, stop_long, stop_short, atr_v, params, tag, min_stop_atr,
                allow_short) -> pd.DataFrame:
    c = trig_frame.df["close"].to_numpy()
    rows = []
    for side, mask, stop in ((1, long_mask, stop_long), (-1, short_mask, stop_short)):
        if side == -1 and not allow_short:
            continue
        dist = (c - stop) * side
        ok = mask & np.isfinite(stop) & np.isfinite(atr_v) & (dist >= min_stop_atr * atr_v) & (dist > 0)
        idx = np.flatnonzero(ok)
        rows.append(pd.DataFrame({"time_ns": trig_frame.close_ns[idx], "side": side, "entry_ref": c[idx],
                                  "stop": stop[idx], "rr": params.reward_to_risk, "min_net_rr": params.min_net_rr,
                                  "max_hold_minutes": params.max_hold_minutes, "atr": atr_v[idx], "tag": tag}))
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=CANDIDATE_COLUMNS)
    return out.sort_values("time_ns", kind="stable").reset_index(drop=True)


def selective_candidates(frames: dict[str, Frame], p: SelectiveParams, *, allow_short: bool) -> pd.DataFrame:
    f4, f1, f15, f5 = frames["4h"], frames["1h"], frames["15m"], frames["5m"]
    dir4 = _direction(f4, p.dir_ema, p.dir_slope_bars)
    c1 = f1.df["close"].to_numpy()
    st1 = structure_trend(f1.df["high"].to_numpy(), f1.df["low"].to_numpy(), p.struct_k)
    e1 = ema(c1, p.struct_ema)
    s1 = np.where((st1 == 1) & (c1 > e1), 1, np.where((st1 == -1) & (c1 < e1), -1, 0))
    pl, ps, stop_l, stop_s, a15 = _pullback(f15, p.pull_ema, p.pull_bars, p.pull_atr, p.atr_n, p.stop_buffer_atr)
    c15 = f15.df["close"].to_numpy()
    hold = ema(c15, p.hold_ema)
    i4, i1, i15 = align(f5, f4), align(f5, f1), align(f5, f15)
    d4, s1a = take(dir4.astype(float), i4, 0), take(s1.astype(float), i1, 0)
    c5, h5, l5 = (f5.df[k].to_numpy() for k in ("close", "high", "low"))
    e5 = ema(c5, p.confirm_ema)
    up = edge((c5 > rolling_max_prev(h5, p.confirm_bars)) & (c5 > e5))
    down = edge((c5 < -rolling_max_prev(-l5, p.confirm_bars)) & (c5 < e5))
    long_mask = up & (d4 == 1) & (s1a == 1) & (take(pl.astype(float), i15, 0) == 1) & (take(c15 - hold, i15) > 0)
    short_mask = down & (d4 == -1) & (s1a == -1) & (take(ps.astype(float), i15, 0) == 1) & (take(c15 - hold, i15) < 0)
    return _candidates(f5, long_mask, short_mask, take(stop_l, i15), take(stop_s, i15), take(a15, i15), p, "A",
                       p.min_stop_atr, allow_short)


def scalp_candidates(frames: dict[str, Frame], p: ScalpParams, *, allow_short: bool) -> pd.DataFrame:
    f1h, f15, f5, f1 = frames["1h"], frames["15m"], frames["5m"], frames["1m"]
    dir1h = _direction(f1h, p.dir_ema, p.dir_slope_bars)
    c15 = f15.df["close"].to_numpy()
    s15 = np.sign(c15 - ema(c15, p.struct_ema))
    pl, ps, stop_l, stop_s, a5 = _pullback(f5, p.pull_ema, p.pull_bars, p.pull_atr, p.atr_n, p.stop_buffer_atr)
    ih, i15, i5 = align(f1, f1h), align(f1, f15), align(f1, f5)
    d1h, s15a = take(dir1h.astype(float), ih, 0), take(s15, i15, 0)
    c1, h1, l1 = (f1.df[k].to_numpy() for k in ("close", "high", "low"))
    e1 = ema(c1, p.trigger_ema)
    up = edge((c1 > rolling_max_prev(h1, p.trigger_bars)) & (c1 > e1))
    down = edge((c1 < -rolling_max_prev(-l1, p.trigger_bars)) & (c1 < e1))
    long_mask = up & (d1h == 1) & (s15a == 1) & (take(pl.astype(float), i5, 0) == 1)
    short_mask = down & (d1h == -1) & (s15a == -1) & (take(ps.astype(float), i5, 0) == 1)
    return _candidates(f1, long_mask, short_mask, take(stop_l, i5), take(stop_s, i5), take(a5, i5), p, "B",
                       p.min_stop_atr, allow_short)


# ---------------------------------------------------------------------------- baselines

def trend_baseline_candidates(frames: dict[str, Frame], *, allow_short: bool, fast: int = 50, slow: int = 200,
                              atr_n: int = 14, stop_atr: float = 3.0, rr: float = 2.0,
                              max_hold_minutes: int = 7 * 1440) -> pd.DataFrame:
    """Simple non-AI trend rule: 1H EMA fast/slow cross, ATR stop, R target (one signal per cross)."""
    f = frames["1h"]
    h, low, c = (f.df[k].to_numpy() for k in ("high", "low", "close"))
    diff = ema(c, fast) - ema(c, slow)
    a = atr(h, low, c, atr_n)
    prev = np.concatenate([[np.nan], diff[:-1]])
    up, down = (prev <= 0) & (diff > 0), (prev >= 0) & (diff < 0)

    class P:
        reward_to_risk, min_net_rr = rr, 0.0
    P.max_hold_minutes = max_hold_minutes
    return _candidates(f, up, down, c - stop_atr * a, c + stop_atr * a, a, P, "TREND", 0.0, allow_short)


def random_candidates(reference: pd.DataFrame, frame: Frame, atr_v: np.ndarray, *, seed: int, start_ns: int,
                      end_ns: int, allow_short: bool) -> pd.DataFrame:
    """Matched random entries: same frame, same trade frequency, same long share, stop distance = the reference's
    median stop distance in ATR units, same reward-to-risk and time limit."""
    ref = reference[(reference.time_ns >= start_ns) & (reference.time_ns < end_ns)]
    times = frame.close_ns
    window = np.flatnonzero((times >= start_ns) & (times < end_ns) & np.isfinite(atr_v))
    if ref.empty or window.size == 0:
        return pd.DataFrame(columns=CANDIDATE_COLUMNS)
    rng = np.random.default_rng(seed)
    pick = np.sort(rng.choice(window, size=min(len(ref), window.size), replace=False))
    long_share = float((ref.side == 1).mean()) if allow_short else 1.0
    sides = np.where(rng.random(pick.size) < long_share, 1, -1)
    mult = float(np.median((ref.entry_ref - ref.stop).abs() / ref.atr))
    c = frame.df["close"].to_numpy()[pick]
    a = atr_v[pick]
    return pd.DataFrame({"time_ns": times[pick], "side": sides, "entry_ref": c, "stop": c - sides * mult * a,
                         "rr": float(ref.rr.iloc[0]), "min_net_rr": float(ref.min_net_rr.iloc[0]),
                         "max_hold_minutes": int(ref.max_hold_minutes.iloc[0]), "atr": a, "tag": "RANDOM"})


def params_dict(p) -> dict:
    return asdict(p)
