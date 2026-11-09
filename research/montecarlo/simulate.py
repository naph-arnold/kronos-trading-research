"""Resampling methods, research sizing policies and path metrics (vectorised over a batch of scenarios).

Units and definitions (also in docs/monte-carlo-validation.md):
  * A path is a sequence of trade outcomes in R (net of recorded costs, optionally plus adverse costs).
  * Equity starts at 1.0. Taking trade j: pnl = R_j x f_j x equity, where f_j is the intended risk fraction
    decided *before* the trade from information available then (idealised: no lot rounding).
  * Drawdown at a step = 1 - equity / running peak. Max drawdown is its maximum over the path.
  * Losing streak = consecutive taken trades with R < 0.
  * Underwater spell = consecutive trade steps with equity below its running peak. It is *recovered* when equity
    regains that peak. Spells still open at the end of the path are *censored*: reported separately, never as
    recovered. Durations are counted in trades.
  * Time underwater = share of taken trades after which equity was below its peak.
  * Daily-loss threshold crossing (simulated) = after any trade, the loss since that day's start equity is
    >= 1.5%. After a crossing, the rest of that day's trades are skipped (mirrors the halt rule; it is NOT the
    operational circuit breaker, which also acts on open positions, quotes and other books).
  * Equity-loss threshold = equity falls to <= 1 - x of the starting equity at any step.
"""
from __future__ import annotations

import numpy as np

MAX_RISK_FRACTION = 0.005      # operational ceiling: no policy may exceed it
DAILY_LOSS_HALT = 0.015
METHODS = ("iid", "trade_block", "calendar_block", "regime_stratified", "regime_conditional")
POLICIES = ("fixed_0.5pct", "drawdown_step", "loss_streak", "volatility_regime")


# --------------------------------------------------------------------------- sampling (indices into the trade list)

def sample_iid(n: int, batch: int, rng: np.random.Generator) -> np.ndarray:
    return rng.integers(0, n, size=(batch, n))


def sample_trade_blocks(n: int, batch: int, rng: np.random.Generator, block_len: int) -> np.ndarray:
    """Circular moving-block bootstrap over the chronological trade sequence (keeps serial dependence within
    blocks of `block_len` consecutive trades)."""
    if block_len < 1:
        raise ValueError("block_len must be >= 1")
    k = -(-n // block_len)
    starts = rng.integers(0, n, size=(batch, k))
    idx = (starts[:, :, None] + np.arange(block_len)[None, None, :]) % n
    return idx.reshape(batch, k * block_len)[:, :n]


def sample_calendar_blocks(periods: list[np.ndarray], batch: int, rng: np.random.Generator) -> np.ndarray:
    """Resample whole calendar periods (weeks or months, INCLUDING periods without trades) with replacement, so
    the calendar horizon is preserved and the number of trades per path varies. Padding = -1."""
    p = len(periods)
    lengths = np.array([len(x) for x in periods])
    picks = rng.integers(0, p, size=(batch, p))
    width = int(lengths.max(initial=0)) * p
    out = np.full((batch, max(width, 1)), -1, dtype=np.int64)
    for b in range(batch):
        seq = [periods[i] for i in picks[b] if lengths[i]]
        if seq:
            flat = np.concatenate(seq)
            out[b, :len(flat)] = flat
    return out, picks


def sample_regime_stratified(regimes: np.ndarray, batch: int, rng: np.random.Generator) -> np.ndarray:
    """Each position keeps the observed regime of that position; the trade is drawn from that regime's trades.
    Preserves the observed regime mix and sequence of regimes."""
    n = len(regimes)
    out = np.empty((batch, n), dtype=np.int64)
    for label in np.unique(regimes):
        pool = np.flatnonzero(regimes == label)
        pos = np.flatnonzero(regimes == label)
        out[:, pos] = pool[rng.integers(0, len(pool), size=(batch, len(pos)))]
    return out


def sample_regime_conditional(regimes: np.ndarray, label: str, n: int, batch: int, rng: np.random.Generator) -> np.ndarray:
    """Counterfactual 'every trade comes from regime `label`': iid draws from that regime's trades only."""
    pool = np.flatnonzero(regimes == label)
    if len(pool) == 0:
        raise ValueError(f"no trades in regime {label}")
    return pool[rng.integers(0, len(pool), size=(batch, n))]


# --------------------------------------------------------------------------- path simulation

def simulate_paths(r: np.ndarray, day: np.ndarray, highvol: np.ndarray | None, policy: str, *,
                   max_fraction: float = MAX_RISK_FRACTION, daily_halt: float = DAILY_LOSS_HALT,
                   dd_trigger: float = 0.03, streak_trigger: int = 3, reduced_fraction: float = 0.0025,
                   equity_loss_levels=(0.05, 0.10), dd_levels=(0.02, 0.03, 0.05, 0.08, 0.10)) -> dict:
    """r, day: (B, L) arrays; NaN in r marks 'no trade' padding. Returns per-scenario metrics (length-B arrays)."""
    if max_fraction > MAX_RISK_FRACTION + 1e-12 or reduced_fraction > max_fraction:
        raise ValueError("risk fraction above the 0.5% operational ceiling")
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy}")
    if policy == "volatility_regime" and highvol is None:
        raise ValueError("volatility_regime needs verified regime labels")
    B, L = r.shape
    eq = np.ones(B)
    peak = np.ones(B)
    min_eq = np.ones(B)
    max_dd = np.zeros(B)
    streak = np.zeros(B, dtype=np.int64)
    longest_streak = np.zeros(B, dtype=np.int64)
    under = np.zeros(B, dtype=np.int64)          # current underwater spell length (trades)
    longest_recovered = np.zeros(B, dtype=np.int64)
    underwater_steps = np.zeros(B, dtype=np.int64)
    taken = np.zeros(B, dtype=np.int64)
    skipped = np.zeros(B, dtype=np.int64)
    day_start = np.ones(B)
    cur_day = np.full(B, -(2 ** 62), dtype=np.int64)
    halted = np.zeros(B, dtype=bool)
    crossed_daily = np.zeros(B, dtype=bool)
    max_f = 0.0
    for j in range(L):
        rj = r[:, j]
        valid = ~np.isnan(rj)
        new_day = valid & (day[:, j] != cur_day)
        cur_day = np.where(new_day, day[:, j], cur_day)
        day_start = np.where(new_day, eq, day_start)
        halted = np.where(new_day, False, halted)
        dd_now = 1.0 - eq / peak
        if policy == "fixed_0.5pct":
            f = np.full(B, max_fraction)
        elif policy == "drawdown_step":
            f = np.where(dd_now >= dd_trigger, reduced_fraction, max_fraction)
        elif policy == "loss_streak":
            f = np.where(streak >= streak_trigger, reduced_fraction, max_fraction)
        else:
            f = np.where(highvol[:, j], reduced_fraction, max_fraction)
        max_f = max(max_f, float(f.max(initial=0.0)))
        take = valid & ~halted
        skipped += (valid & halted)
        pnl = np.where(take, np.nan_to_num(rj) * f * eq, 0.0)
        eq = eq + pnl
        taken += take
        loss = take & (rj < 0)
        win = take & (rj >= 0)
        streak = np.where(loss, streak + 1, np.where(win, 0, streak))
        longest_streak = np.maximum(longest_streak, streak)
        new_peak = take & (eq >= peak)
        # an underwater spell ends (recovered) when a taken trade brings equity back to the peak
        ended = new_peak & (under > 0)
        longest_recovered = np.where(ended, np.maximum(longest_recovered, under), longest_recovered)
        under = np.where(new_peak, 0, np.where(take & (eq < peak), under + 1, under))
        peak = np.maximum(peak, eq)
        underwater_steps += take & (eq < peak)
        min_eq = np.minimum(min_eq, eq)
        max_dd = np.maximum(max_dd, 1.0 - eq / peak)
        day_loss = 1.0 - eq / day_start
        cross = take & (day_loss >= daily_halt - 1e-12)
        crossed_daily |= cross
        halted |= cross
    out = {
        "ending_equity": eq, "net_return_pct": (eq - 1.0) * 100, "max_drawdown_pct": max_dd * 100,
        "longest_losing_streak": longest_streak, "longest_recovered_spell": longest_recovered,
        "censored": under > 0, "censored_spell": under,
        "time_underwater_pct": np.where(taken > 0, 100.0 * underwater_steps / np.maximum(taken, 1), 0.0),
        "daily_loss_crossed": crossed_daily, "trades_taken": taken, "trades_skipped_by_halt": skipped,
        "min_equity": min_eq, "max_fraction_used": max_f,
    }
    for x in dd_levels:
        out[f"dd_ge_{x:g}"] = max_dd >= x - 1e-12
    for x in equity_loss_levels:
        out[f"equity_loss_ge_{x:g}"] = min_eq <= 1.0 - x + 1e-12
    return out
