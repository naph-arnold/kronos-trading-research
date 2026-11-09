"""Monte Carlo runs: variant grid, seeded batches, bounded memory, checkpoint/cancel, versioned reports.

Reproducibility: every variant draws from numpy PCG64 seeded by SeedSequence([seed, crc32(variant key)]) and
spawned per batch, so results are identical for the same data, code, configuration and seed, regardless of the
number of workers or of a resume from a checkpoint.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sys
import time
import zlib
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from research.montecarlo import ALGORITHM_VERSION, SCHEMA_VERSION
from research.montecarlo.costs import adjusted_r
from research.montecarlo.evidence import calendar_periods, summarize
from research.montecarlo.records import TradeDataset
from research.montecarlo.simulate import (MAX_RISK_FRACTION, sample_calendar_blocks, sample_iid,
                                          sample_regime_conditional, sample_regime_stratified, sample_trade_blocks,
                                          simulate_paths)

DEFAULTS = {
    "seed": 20261109, "scenarios": 10000, "batch_size": 1000, "max_workers": 1, "min_free_memory_mb": 400,
    "min_trades": 30, "min_months_with_trades": 6, "min_regime_trades": 10, "block_len": 4,
    "block_lens": [2, 4, 8], "calendar_unit": "month", "low_priority": True,
    "policies": ["fixed_0.5pct", "drawdown_step", "loss_streak", "volatility_regime"],
    "cost_scenarios": {"recorded": {"kind": "recorded"}},
    "drawdown_levels": [0.02, 0.03, 0.05, 0.08, 0.10], "equity_loss_levels": [0.05, 0.10],
    "adaptive": {"dd_trigger": 0.03, "streak_trigger": 3, "reduced_fraction": 0.0025},
}
Q = (0.05, 0.25, 0.5, 0.75, 0.95)


def _fp(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def available_memory_mb() -> float | None:
    try:
        if sys.platform == "win32":
            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.ullAvailPhys / 2 ** 20
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable"):
                    return int(line.split()[1]) / 1024
    except Exception:  # noqa: BLE001
        return None
    return None


def lower_priority() -> None:
    """Keep simulations from competing with the market-monitoring runners."""
    try:
        if sys.platform == "win32":
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)
        else:
            os.nice(10)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- variants

def variant_grid(ds: TradeDataset, cfg: dict) -> list[dict]:
    has_regimes = bool(ds.regime_source and ds.regime_source.get("reproduces_report", True) and
                       all(t.regime for t in ds.trades))
    methods = [("iid", {}), ("trade_block", {"block_len": cfg["block_len"]}),
               ("calendar_block", {"unit": cfg["calendar_unit"]})]
    if has_regimes:
        methods.append(("regime_stratified", {}))
    policies = [p for p in cfg["policies"] if p != "volatility_regime" or has_regimes]
    grid = []
    for m, params in methods:
        for pol in policies:
            for cost in cfg["cost_scenarios"]:
                grid.append({"group": "main", "method": m, "params": params, "policy": pol, "cost": cost})
    base = {"policy": "fixed_0.5pct", "cost": "recorded"}
    for L in cfg["block_lens"]:
        if L != cfg["block_len"]:
            grid.append({"group": "sensitivity", "method": "trade_block", "params": {"block_len": L}, **base})
    other = "week" if cfg["calendar_unit"] == "month" else "month"
    grid.append({"group": "sensitivity", "method": "calendar_block", "params": {"unit": other}, **base})
    if has_regimes:
        counts: dict[str, int] = {}
        for t in ds.trades:
            counts[t.regime] = counts.get(t.regime, 0) + 1
        for label, k in sorted(counts.items()):
            if k >= cfg["min_regime_trades"]:
                grid.append({"group": "regime", "method": "regime_conditional", "params": {"regime": label}, **base})
    for v in grid:
        v["key"] = f"{v['method']}|{json.dumps(v['params'], sort_keys=True)}|{v['policy']}|{v['cost']}"
    return grid


def _day_numbers(ds: TradeDataset) -> np.ndarray:
    return np.array([datetime.fromisoformat(t.closed_at).date().toordinal() for t in ds.trades], dtype=np.int64)


def _batch(ds_arrays: dict, v: dict, cfg: dict, batch: int, rng: np.random.Generator) -> dict:
    r_all, days, regimes, n = ds_arrays["r"][v["cost"]], ds_arrays["days"], ds_arrays["regimes"], ds_arrays["n"]
    m = v["method"]
    if m == "calendar_block":
        idx, picks = sample_calendar_blocks(ds_arrays["periods"][v["params"]["unit"]], batch, rng)
        valid = idx >= 0
        safe = np.where(valid, idx, 0)
        r = np.where(valid, r_all[safe], np.nan)
        # day id: position of the period in the path, then the trade's own day inside that period
        lengths = np.array([len(p) for p in ds_arrays["periods"][v["params"]["unit"]]])
        slot = np.zeros_like(idx)
        for b in range(batch):
            reps = np.repeat(np.arange(picks.shape[1]), lengths[picks[b]])
            slot[b, :len(reps)] = reps
        day = slot * 1_000_000 + days[safe]
    else:
        if m == "iid":
            idx = sample_iid(n, batch, rng)
        elif m == "trade_block":
            idx = sample_trade_blocks(n, batch, rng, v["params"]["block_len"])
        elif m == "regime_stratified":
            idx = sample_regime_stratified(regimes, batch, rng)
        else:
            idx = sample_regime_conditional(regimes, v["params"]["regime"], n, batch, rng)
        r = r_all[idx]
        day = np.broadcast_to(days, (batch, n))  # resampled trades occupy the observed trade slots (dates)
        valid = np.ones_like(idx, dtype=bool)
    highvol = None
    if regimes is not None:
        hv = np.char.endswith(regimes.astype(str), "highvol")
        highvol = np.where(valid, hv[np.where(valid, idx, 0)], False)
    a = cfg["adaptive"]
    return simulate_paths(r, day, highvol, v["policy"], dd_trigger=a["dd_trigger"], streak_trigger=a["streak_trigger"],
                          reduced_fraction=a["reduced_fraction"], dd_levels=cfg["drawdown_levels"],
                          equity_loss_levels=cfg["equity_loss_levels"])


def _qs(x: np.ndarray) -> dict:
    return {f"p{int(q * 100)}": float(np.quantile(x, q)) for q in Q} if len(x) else None


def _hist(x: np.ndarray, bins: int = 30) -> dict:
    lo, hi = float(np.min(x)), float(np.max(x))
    if hi <= lo:
        hi = lo + 1e-9
    counts, edges = np.histogram(x, bins=bins, range=(lo, hi))
    return {"edges": [round(float(e), 6) for e in edges], "counts": counts.tolist()}


def run_variant(ds_arrays: dict, v: dict, cfg: dict, cancel_file: Path | None = None) -> dict | None:
    n_batches = -(-cfg["scenarios"] // cfg["batch_size"])
    seeds = np.random.SeedSequence([cfg["seed"], zlib.crc32(v["key"].encode())]).spawn(n_batches)
    parts = []
    batch_q = {"net_return_pct": [], "max_drawdown_pct": []}
    for b, ss in enumerate(seeds):
        if cancel_file is not None and cancel_file.exists():
            return None
        size = min(cfg["batch_size"], cfg["scenarios"] - b * cfg["batch_size"])
        res = _batch(ds_arrays, v, cfg, size, np.random.Generator(np.random.PCG64(ss)))
        if res["max_fraction_used"] > MAX_RISK_FRACTION + 1e-12:
            raise AssertionError("risk cap violated")
        res.pop("max_fraction_used")
        for k in batch_q:
            batch_q[k].append([float(np.quantile(res[k], q)) for q in (0.5, 0.95, 0.05)])
        parts.append(res)
    allm = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
    N = len(allm["net_return_pct"])
    equity0 = ds_arrays["equity0"]
    prob = lambda m: float(np.mean(m))  # noqa: E731
    se = lambda p: float(np.sqrt(p * (1 - p) / N))  # noqa: E731
    probs = {k: {"p": prob(allm[k]), "mc_se": se(prob(allm[k]))} for k in allm if k.startswith(("dd_ge_", "equity_loss_ge_"))}
    probs["daily_loss_crossed"] = {"p": prob(allm["daily_loss_crossed"]), "mc_se": se(prob(allm["daily_loss_crossed"]))}
    probs["ends_underwater_censored"] = {"p": prob(allm["censored"]), "mc_se": se(prob(allm["censored"]))}
    bq = {k: np.array(v_) for k, v_ in batch_q.items()}
    mc_se = {k: {"p50": float(bq[k][:, 0].std(ddof=1) / np.sqrt(len(bq[k]))) if len(bq[k]) > 1 else None,
                 "p95": float(bq[k][:, 1].std(ddof=1) / np.sqrt(len(bq[k]))) if len(bq[k]) > 1 else None,
                 "p5": float(bq[k][:, 2].std(ddof=1) / np.sqrt(len(bq[k]))) if len(bq[k]) > 1 else None}
             for k in bq}
    cens = allm["censored_spell"][allm["censored"]]
    return {
        **{k: v[k] for k in ("key", "group", "method", "params", "policy", "cost")},
        "scenarios": N, "batches": n_batches,
        "ending_equity_usd": _qs(allm["ending_equity"] * equity0) if equity0 else None,
        "net_return_pct": _qs(allm["net_return_pct"]), "max_drawdown_pct": _qs(allm["max_drawdown_pct"]),
        "longest_losing_streak": _qs(allm["longest_losing_streak"].astype(float)),
        "longest_recovered_spell_trades": _qs(allm["longest_recovered_spell"].astype(float)),
        "censored_spell_trades": _qs(cens.astype(float)) if len(cens) else None,
        "time_underwater_pct": _qs(allm["time_underwater_pct"]),
        "trades_per_path": _qs(allm["trades_taken"].astype(float)),
        "trades_skipped_by_halt_mean": float(allm["trades_skipped_by_halt"].mean()),
        "probabilities": probs, "mc_standard_error": mc_se,
        "hist": {"net_return_pct": _hist(allm["net_return_pct"]), "max_drawdown_pct": _hist(allm["max_drawdown_pct"]),
                 "losing_streak": np.bincount(allm["longest_losing_streak"]).tolist(),
                 "recovered_spell": _hist(allm["longest_recovered_spell"].astype(float), 20)},
    }


def _arrays(ds: TradeDataset, cfg: dict) -> dict:
    regimes = (np.array([t.regime for t in ds.trades]) if ds.regime_source and all(t.regime for t in ds.trades)
               else None)
    return {"n": len(ds.trades), "days": _day_numbers(ds), "regimes": regimes,
            "r": {name: adjusted_r(ds, sc) for name, sc in cfg["cost_scenarios"].items()},
            "periods": {u: calendar_periods(ds, u)[0] for u in ("week", "month")},
            "equity0": ds.book.get("initial_equity")}


def observed_path(ds: TradeDataset, cfg: dict) -> dict:
    """The recorded chronological path under fixed 0.5% sizing (a single scenario, for reference)."""
    r = np.array([[t.r_net for t in ds.trades]])
    m = simulate_paths(r, _day_numbers(ds)[None, :], None, "fixed_0.5pct", dd_levels=cfg["drawdown_levels"],
                       equity_loss_levels=cfg["equity_loss_levels"])
    return {k: (float(v[0]) if hasattr(v, "__len__") else v) for k, v in m.items() if k != "max_fraction_used"}


def run(ds: TradeDataset, config: dict | None = None, *, checkpoint_dir: Path | None = None,
        cancel_file: Path | None = None, progress=None, allow_fixture: bool = False) -> dict:
    cfg = {**DEFAULTS, **(config or {})}
    if ds.partition == "synthetic_fixture" and not allow_fixture:
        raise ValueError("synthetic fixtures are for software tests only")
    cfg_fp, ds_fp = _fp({k: v for k, v in cfg.items() if k not in ("max_workers", "low_priority")}), ds.fingerprint()
    report = {"schema_version": SCHEMA_VERSION, "algorithm": ALGORITHM_VERSION, "kind": "monte_carlo_research",
              "market": ds.market, "strategy": ds.strategy, "partition": ds.partition,
              "dataset": {k: v for k, v in ds.to_json().items() if k != "trades"},
              "dataset_fingerprint": ds_fp, "config": cfg, "config_fingerprint": cfg_fp,
              "risk_sizing": {"operational_ceiling": MAX_RISK_FRACTION, "daily_loss_halt": 0.015,
                              "aggregate_open_risk": 0.01, "note": "fixed = current operational policy; adaptive "
                              "policies are research-only, never above 0.5%, no martingale, causal inputs only"},
              "evidence": summarize(ds, seed=cfg["seed"], comparisons=max(1, int(ds.selection.get("strategies_in_report") or 1)))}
    ev = report["evidence"]
    if ev["closed_trades"] < cfg["min_trades"] or ev["months_with_trades"] < cfg["min_months_with_trades"]:
        report.update(status="insufficient_evidence", variants=[], reason=(
            f"{ev['closed_trades']} closed trades in {ev['months_with_trades']} month(s); at least {cfg['min_trades']} "
            f"trades over {cfg['min_months_with_trades']} months are required. No distribution is produced."))
        report["result_sha256"] = _fp(report["variants"])
        return report
    need = cfg["batch_size"] * max(len(ds.trades), 1) * 8 * 24 / 2 ** 20 + 50
    avail = available_memory_mb()
    report["resources"] = {"available_memory_mb_at_start": None if avail is None else round(avail),
                           "estimated_peak_mb": round(need, 1), "max_workers": cfg["max_workers"]}
    if avail is not None and avail < max(cfg["min_free_memory_mb"], need):
        report.update(status="deferred_insufficient_resources", variants=[],
                      reason=f"{avail:.0f} MB available < required {max(cfg['min_free_memory_mb'], need):.0f} MB")
        return report
    if cfg["low_priority"]:
        lower_priority()
    arrays = _arrays(ds, cfg)
    grid = variant_grid(ds, cfg)
    ck = None
    done: dict[str, dict] = {}
    if checkpoint_dir is not None:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        ck = checkpoint_dir / f"{ds.market}_{ds.strategy}_{ds_fp[:10]}_{cfg_fp[:10]}.json"
        if ck.exists():
            done = json.loads(ck.read_text(encoding="utf-8"))["variants"]
    started = time.monotonic()
    todo = [v for v in grid if v["key"] not in done]
    if cfg["max_workers"] > 1 and len(todo) > 1:
        with ProcessPoolExecutor(max_workers=cfg["max_workers"]) as pool:
            futs = [(v, pool.submit(run_variant, arrays, v, cfg, cancel_file)) for v in todo]
            for i, (v, f) in enumerate(futs):
                res = f.result()
                if res is None:
                    break
                done[v["key"]] = res
                _save(ck, done)
                if progress:
                    progress(len(done), len(grid), v["key"])
    else:
        for v in todo:
            res = run_variant(arrays, v, cfg, cancel_file)
            if res is None:
                break
            done[v["key"]] = res
            _save(ck, done)
            if progress:
                progress(len(done), len(grid), v["key"])
    complete = all(v["key"] in done for v in grid)
    report["variants"] = [done[v["key"]] for v in grid if v["key"] in done]
    report["status"] = "complete" if complete else "cancelled"
    report["observed_path_fixed"] = observed_path(ds, cfg)
    report["comparisons"] = comparisons(report["variants"])
    report["limitations"] = limitations(ds, report)
    report["runtime_s"] = round(time.monotonic() - started, 2)
    report["result_sha256"] = _fp(report["variants"])
    return report


def _save(ck: Path | None, done: dict) -> None:
    if ck is None:
        return
    tmp = ck.with_suffix(".tmp")
    tmp.write_text(json.dumps({"variants": done}), encoding="utf-8")
    tmp.replace(ck)


def comparisons(variants: list[dict]) -> dict:
    by = {(v["method"], json.dumps(v["params"], sort_keys=True), v["policy"], v["cost"]): v for v in variants}

    def row(v):
        return {"p50_return_pct": v["net_return_pct"]["p50"], "p5_return_pct": v["net_return_pct"]["p5"],
                "p95_max_dd_pct": v["max_drawdown_pct"]["p95"], "p50_max_dd_pct": v["max_drawdown_pct"]["p50"],
                "p_dd_ge_5pct": v["probabilities"].get("dd_ge_0.05", {}).get("p"),
                "p_daily_loss_crossed": v["probabilities"]["daily_loss_crossed"]["p"]}
    sizing, costs, methods = [], [], []
    for (m, p, pol, cost), v in by.items():
        if v["group"] != "main":
            continue
        if pol != "fixed_0.5pct" and cost == "recorded":
            base = by.get((m, p, "fixed_0.5pct", "recorded"))
            if base:
                sizing.append({"method": m, "policy": pol, "fixed": row(base), "adaptive": row(v)})
        if pol == "fixed_0.5pct" and cost != "recorded":
            base = by.get((m, p, "fixed_0.5pct", "recorded"))
            if base:
                costs.append({"method": m, "scenario": cost, "recorded": row(base), "adverse": row(v)})
    for v in variants:
        if v["policy"] == "fixed_0.5pct" and v["cost"] == "recorded" and v["method"] != "regime_conditional":
            methods.append({"method": v["method"], "params": v["params"], **row(v)})
    regimes = [{"regime": v["params"]["regime"], **row(v)} for v in variants if v["method"] == "regime_conditional"]
    return {"fixed_vs_adaptive": sizing, "recorded_vs_adverse_costs": costs, "method_sensitivity": methods,
            "regime_conditional": regimes}


def limitations(ds: TradeDataset, report: dict) -> list[str]:
    ev = report["evidence"]
    out = [
        "Monte Carlo error (mc_se) measures simulation noise only; it says nothing about how the strategy will "
        "perform. Strategy uncertainty is the bootstrap CI of mean R and the effective sample size.",
        "Resampling recorded trades assumes the future resembles this sample; it cannot create evidence the sample "
        "does not contain and cannot override a failed historical validation.",
        "Sizing is idealised (risk = fraction x equity exactly; no lot rounding, minimum-lot rejections or "
        "concurrent-position limits), so absolute figures differ from the shadow books.",
        "Costs are the source's modelled costs; adverse scenarios are stated assumptions, not observations.",
    ]
    if ds.partition == "historical_holdout":
        out.append("Historical holdout: these trades were examined during research and informed later "
                   "pre-registrations, so they are not untouched evidence.")
    if ev.get("closed_trades", 0) < 100:
        out.append(f"Small sample ({ev.get('closed_trades')} trades): tail percentiles are driven by few trades.")
    dep = ev.get("dependence") or {}
    if dep.get("overlapping_trades"):
        out.append(f"{dep['overlapping_trades']} trades overlapped in time; resampling treats them sequentially.")
    mt = ev.get("multiple_testing") or {}
    if mt.get("comparisons", 1) > 1:
        out.append(f"Selection: the source compared {mt['comparisons']} strategies "
                   f"({ds.selection.get('configurations_tried')} configurations for this one); see the "
                   "Bonferroni-adjusted CI.")
    if not ds.regime_source or ds.regime_source.get("used") is False:
        out.append("No verified regime labels: regime-conditional sampling and the volatility policy are omitted.")
    return out


def write_report(report: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{report['market']}__{report['strategy']}__{report['partition']}.json"
    path = out_dir / name
    report = {**report, "created_at": datetime.now(timezone.utc).isoformat()}
    path.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    return path
