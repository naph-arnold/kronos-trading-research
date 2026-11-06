"""Backtest evaluation: chronological splits, walk-forward, metrics and regime breakdown.

Protocol (both pipelines):
1. The final `holdout_fraction` of bars is reserved and never used for any
   parameter choice. It is evaluated exactly once with parameters selected on
   data before it.
2. The remaining "research" bars are evaluated walk-forward: for each window,
   parameters are chosen on the training segment only (grid search, minimum
   trade count) and applied to the following test segment.
3. Indicators warm up on bars *before* each test segment, but signals are only
   accepted inside it (`TradingWindow`), so no test-period trade depends on a
   parameter fitted to that period.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Sequence

from trading.indicators import ATR, EMA
from trading.marketdata import INTERVALS, Bar


class TradingWindow:
    """Wraps a strategy so that it warms up on all bars but may only signal inside [start, end)."""

    def __init__(self, strategy, start: datetime, end: datetime):
        self.strategy, self.start, self.end = strategy, start, end

    def __call__(self, history):
        signal = self.strategy(history)
        bar = history[-1]
        if signal is None or not (self.start <= bar.close_time < self.end):
            return None
        return signal


@dataclass(frozen=True)
class Split:
    train: tuple[int, int]
    test: tuple[int, int]


def chronological_split(n: int, *, holdout_fraction: float) -> tuple[int, int]:
    """Return (research_end, n): bars [0, research_end) for research, [research_end, n) holdout."""
    if not 0 < holdout_fraction < 0.5 or n < 10:
        raise ValueError("holdout_fraction must be in (0, 0.5) and data non-trivial")
    return int(n * (1 - holdout_fraction)), n


def walk_forward_splits(n: int, *, train: int, test: int, step: int | None = None,
                        anchored: bool = False) -> list[Split]:
    if min(train, test) < 1:
        raise ValueError("train/test must be positive")
    step = step or test
    splits, start = [], 0
    while start + train + test <= n:
        train_start = 0 if anchored else start
        splits.append(Split((train_start, start + train), (start + train, start + train + test)))
        start += step
    if not splits:
        raise ValueError("Not enough bars for one walk-forward window")
    return splits


def param_grid(base, grid: dict[str, Sequence]) -> list:
    keys = sorted(grid)
    return [replace(base, **dict(zip(keys, values))) for values in itertools.product(*(grid[k] for k in keys))]


def _floats(curve: list[tuple[datetime, Decimal]]) -> list[float]:
    return [float(v) for _, v in curve]


def metrics(result: dict, *, interval: str) -> dict:
    """Summary statistics for a replay/benchmark result (net of all modelled costs)."""
    curve = result["equity_curve"]
    values = _floats(curve)
    start, end = curve[0][0], curve[-1][0]
    span_days = (end - start).total_seconds() / 86400
    initial, final = float(result["initial_equity"]), float(result["final_equity"])
    total = final / initial - 1
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1] > 0]
    bars_per_year = timedelta(days=365) / INTERVALS[interval]
    mean = sum(rets) / len(rets) if rets else 0.0
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
    downside = [min(r, 0.0) for r in rets]
    dvar = sum(r * r for r in downside) / len(downside) if downside else 0.0
    trades = result.get("trade_ledger", [])
    pnl = [float(t["pnl"]) for t in trades]
    gross = [float(t["gross_pnl"]) for t in trades]
    wins, losses = [x for x in pnl if x > 0], [-x for x in pnl if x <= 0]
    max_dd = float(result["max_drawdown_pct"])
    annualized = (1 + total) ** (365 / span_days) - 1 if span_days >= 90 and total > -1 else None
    return {
        "start": start.isoformat(), "end": end.isoformat(), "span_days": round(span_days, 2),
        "net_return_pct": round(100 * total, 4),
        "gross_return_pct": round(100 * sum(gross) / initial, 4) if trades else None,
        "annualized_return_pct": None if annualized is None else round(100 * annualized, 4),
        "max_drawdown_pct": round(max_dd, 4),
        "trades": len(trades) if trades else int(result.get("trades", 0)),
        "win_rate_pct": round(100 * len(wins) / len(pnl), 2) if pnl else None,
        "profit_factor": round(sum(wins) / sum(losses), 4) if losses and sum(losses) > 0 else None,
        "avg_trade_pnl": round(sum(pnl) / len(pnl), 4) if pnl else None,
        "avg_trade_return_pct": round(100 * sum(pnl) / len(pnl) / initial, 5) if pnl else None,
        "avg_r_multiple": (round(sum(float(t["r_multiple"]) for t in trades if t.get("r_multiple") is not None)
                                 / len(trades), 4) if trades else None),
        "total_costs": round(float(result.get("total_costs", 0)), 4),
        "exposure_pct": round(float(result.get("exposure_pct", 0)), 2),
        "sharpe_annualized": round(mean / math.sqrt(var) * math.sqrt(bars_per_year), 4) if var > 0 else None,
        "sortino_annualized": round(mean / math.sqrt(dvar) * math.sqrt(bars_per_year), 4) if dvar > 0 else None,
        "calmar": round(annualized / (max_dd / 100), 4) if annualized is not None and max_dd > 0 else None,
    }


def regime_labels(bars: Sequence[Bar], *, ema_period: int = 200, atr_period: int = 14,
                  vol_window: int = 500) -> dict[datetime, str]:
    """Causal market-regime label per bar close: trend (up/down vs EMA) x volatility (ATR% vs its
    rolling median). Label at bar i uses bars <= i only; keyed by bar close time."""
    ema, atr = EMA(ema_period), ATR(atr_period)
    history: list[float] = []
    labels = {}
    for bar in bars:
        close = float(bar.close)
        e = ema.update(close)
        a = atr.update(float(bar.high), float(bar.low), close)
        if e is None or a is None:
            labels[bar.close_time] = "warmup"
            continue
        pct = a / close
        history.append(pct)
        window = sorted(history[-vol_window:])
        median = window[len(window) // 2]
        labels[bar.close_time] = f"{'up' if close > e else 'down'}-{'highvol' if pct > median else 'lowvol'}"
    return labels


def by_regime(trades: list[dict], labels: dict[datetime, str]) -> dict:
    """Group trades by the regime known at signal time (the bar closing at the trade's open)."""
    groups: dict[str, list[float]] = {}
    for trade in trades:
        label = labels.get(trade["opened_at"], "unknown")
        groups.setdefault(label, []).append(float(trade["pnl"]))
    return {k: {"trades": len(v), "net_pnl": round(sum(v), 4),
                "win_rate_pct": round(100 * sum(x > 0 for x in v) / len(v), 2)} for k, v in sorted(groups.items())}


def objective(m: dict, *, min_trades: int) -> float:
    """Training objective: net return, but configurations with too few trades are ineligible."""
    if m["trades"] < min_trades:
        return float("-inf")
    return m["net_return_pct"]


def walk_forward(
    bars: Sequence[Bar],
    *,
    make_strategy: Callable[[object], object],
    candidates: list,
    run: Callable[[Sequence[Bar], object], dict],
    interval: str,
    splits: list[Split],
    warmup_bars: int,
    min_trades: int,
) -> dict:
    """Select parameters on each training segment, evaluate on the following test segment."""
    windows = []
    for split in splits:
        (a, b), (c, d) = split.train, split.test
        best, best_score, scored = None, float("-inf"), 0
        for params in candidates:
            result = run(bars[a:b], make_strategy(params))
            score = objective(metrics(result, interval=interval), min_trades=min_trades)
            scored += 1
            if score > best_score:
                best, best_score = params, score
        if best is None:
            windows.append({"train": [bars[a].open_time.isoformat(), bars[b - 1].close_time.isoformat()],
                            "test": [bars[c].open_time.isoformat(), bars[d - 1].close_time.isoformat()],
                            "selected": None, "reason": f"No configuration reached {min_trades} training trades"})
            continue
        warm = max(0, c - warmup_bars)
        strategy = TradingWindow(make_strategy(best), bars[c].open_time, bars[d - 1].close_time + timedelta(0))
        test_result = run(bars[warm:d], strategy)
        train_metrics = metrics(run(bars[a:b], make_strategy(best)), interval=interval)
        windows.append({
            "train": [bars[a].open_time.isoformat(), bars[b - 1].close_time.isoformat()],
            "test": [bars[c].open_time.isoformat(), bars[d - 1].close_time.isoformat()],
            "configurations_tried": scored, "selected": best, "train_metrics": train_metrics,
            "test_metrics": metrics(test_result, interval=interval), "test_result": test_result,
        })
    tested = [w for w in windows if w.get("selected") is not None]
    compounded = 1.0
    for w in tested:
        compounded *= 1 + w["test_metrics"]["net_return_pct"] / 100
    return {
        "windows": windows,
        "oos_compounded_return_pct": round(100 * (compounded - 1), 4) if tested else None,
        "oos_trades": sum(w["test_metrics"]["trades"] for w in tested),
        "oos_windows_profitable": sum(w["test_metrics"]["net_return_pct"] > 0 for w in tested),
        "oos_windows": len(tested),
        "avg_train_minus_test_return_pct": (round(sum(w["train_metrics"]["net_return_pct"]
                                                      - w["test_metrics"]["net_return_pct"] for w in tested)
                                                  / len(tested), 4) if tested else None),
    }


def config_hash(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()


def classify(holdout: dict | None, wf: dict, *, broker_spec_verified: bool) -> str:
    """Research classification. Never 'execution-ready' in Phase 2."""
    if not broker_spec_verified:
        return "experimental (venue specification unverified)"
    if holdout is None or wf.get("oos_windows", 0) == 0:
        return "experimental (insufficient out-of-sample evidence)"
    positive = (holdout["net_return_pct"] > 0 and (wf["oos_compounded_return_pct"] or 0) > 0
                and wf["oos_windows_profitable"] * 2 > wf["oos_windows"] and holdout["trades"] >= 30)
    return "candidate for Phase 3 review (not authorized)" if positive else "experimental (no robust OOS edge)"
