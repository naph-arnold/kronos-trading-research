"""How much evidence the recorded trades actually contain (separate from Monte Carlo estimation error)."""
from __future__ import annotations

from datetime import datetime

import numpy as np

from research.montecarlo.records import TradeDataset


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def calendar_periods(ds: TradeDataset, unit: str) -> tuple[list[np.ndarray], list[str]]:
    """Trade indices per calendar period (ISO week or month of the close), INCLUDING empty periods between the
    first and last trade so that resampled paths keep the observed calendar horizon."""
    if not ds.trades:
        return [], []
    keys = []
    for t in ds.trades:
        c = _ts(t.closed_at)
        keys.append((c.isocalendar()[0], c.isocalendar()[1]) if unit == "week" else (c.year, c.month))
    first, last = keys[0], keys[-1]
    seq = []
    if unit == "month":
        y, m = first
        while (y, m) <= last:
            seq.append((y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    else:
        from datetime import date, timedelta
        d = date.fromisocalendar(first[0], first[1], 1)
        end = date.fromisocalendar(last[0], last[1], 1)
        while d <= end:
            iso = d.isocalendar()
            seq.append((iso[0], iso[1]))
            d += timedelta(days=7)
    pos = {k: i for i, k in enumerate(seq)}
    buckets: list[list[int]] = [[] for _ in seq]
    for i, k in enumerate(keys):
        buckets[pos[k]].append(i)
    return [np.array(b, dtype=np.int64) for b in buckets], [f"{k[0]}-{k[1]:02d}" for k in seq]


def effective_sample_size(r: np.ndarray) -> dict:
    """n_eff from lag-1 autocorrelation (AR(1) approximation): n (1 - rho) / (1 + rho), capped at n."""
    n = len(r)
    if n < 3 or np.std(r) == 0:
        return {"n": n, "lag1_autocorrelation": None, "n_eff": float(n)}
    rho = float(np.corrcoef(r[:-1], r[1:])[0, 1])
    n_eff = n if rho <= 0 else n * (1 - rho) / (1 + rho)
    return {"n": n, "lag1_autocorrelation": round(rho, 4), "n_eff": round(float(min(n, max(1.0, n_eff))), 2)}


def bootstrap_mean_ci(r: np.ndarray, *, seed: int, resamples: int = 10000, levels=(0.95,)) -> dict:
    rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence([seed, 7919])))
    means = r[rng.integers(0, len(r), size=(resamples, len(r)))].mean(axis=1)
    return {f"{lvl:.4g}": [float(np.quantile(means, (1 - lvl) / 2)), float(np.quantile(means, 1 - (1 - lvl) / 2))]
            for lvl in levels}


def summarize(ds: TradeDataset, *, seed: int, comparisons: int) -> dict:
    """Evidence about the strategy itself (sampling uncertainty of the recorded trades)."""
    r = np.array([t.r_net for t in ds.trades], dtype=float)
    n = len(r)
    weeks, _ = calendar_periods(ds, "week")
    months, month_keys = calendar_periods(ds, "month")
    out = {"closed_trades": n, "weeks_spanned": len(weeks), "weeks_with_trades": sum(1 for w in weeks if len(w)),
           "months_spanned": len(months), "months_with_trades": sum(1 for m in months if len(m)),
           "first_trade": ds.trades[0].opened_at if n else None, "last_trade": ds.trades[-1].closed_at if n else None}
    if n == 0:
        return out
    overlaps = sum(1 for a, b in zip(ds.trades, ds.trades[1:]) if _ts(b.opened_at) < _ts(a.closed_at))
    alpha_adj = 0.05 / max(1, comparisons)
    ci = bootstrap_mean_ci(r, seed=seed, levels=(0.95, 1 - alpha_adj)) if n >= 2 else {}
    pos = np.sort(r[r > 0])[::-1]
    total = float(r.sum())
    month_net = [float(r[m].sum()) for m in months if len(m)]
    regimes: dict[str, int] = {}
    for t in ds.trades:
        if t.regime:
            regimes[t.regime] = regimes.get(t.regime, 0) + 1
    out.update({
        "mean_r": float(r.mean()), "median_r": float(np.median(r)), "sd_r": float(r.std(ddof=1)) if n > 1 else None,
        "win_rate_pct": float(100 * (r > 0).mean()), "net_r_total": total,
        "mean_r_ci95": ci.get("0.95"),
        "multiple_testing": {"comparisons": comparisons, "bonferroni_level": round(1 - alpha_adj, 6),
                             "mean_r_ci_adjusted": ci.get(f"{1 - alpha_adj:.4g}")},
        "dependence": {**effective_sample_size(r), "overlapping_trades": overlaps},
        "concentration": {
            "top1_share_of_positive_r": float(pos[:1].sum() / pos.sum()) if len(pos) else None,
            "top5_share_of_positive_r": float(pos[:5].sum() / pos.sum()) if len(pos) else None,
            "net_r_without_best_3": float(total - pos[:3].sum()),
            "best_month_net_r": max(month_net) if month_net else None,
            "net_r_without_best_month": total - max(month_net) if month_net else None},
        "regime_coverage": regimes or None,
        "sessions": {s: sum(1 for t in ds.trades if t.session == s) for s in sorted({t.session for t in ds.trades})},
    })
    return out
