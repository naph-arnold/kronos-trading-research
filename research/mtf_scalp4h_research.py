"""4H-led scalping (scalp4h-v1) vs the existing H1-led scalper: independent walk-forward backtests (research only).

    python -m research.mtf_scalp4h_research --market btcusdt|xauusd [--workers 6]

Variants (same 15M/5M/1M rules, stops, targets, grid, windows, costs and risk engine; only the direction differs):
  B         existing H1-led scalper, unchanged (re-run here; must reproduce research/output/mtf results exactly)
  B4H_only  4H bias mandatory, 1H level skipped (isolates the 4H filter)
  B4H       4H bias mandatory + 1H structure/momentum alignment (the proposed hierarchy)
Configuration per market: research/configs/mtf_scalp4h_v1_<market>.json. Outputs go to
research/output/mtf/scalp4h_v1/ and never overwrite the original multi-timeframe results.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from research import mtf_research as base
from trading.evaluation import param_grid
from trading.mtf.features import atr
from trading.mtf.frames import align, take
from trading.mtf.scalp4h import STRATEGY_VERSION, Scalp4HParams, scalp4h_candidates
from trading.mtf.simulator import AccountRules, summarize
from trading.mtf.strategies import ScalpParams, random_candidates

OUT = Path("research/output/mtf/scalp4h_v1")


def load_config(market: str) -> dict:
    cfg = json.loads(Path(f"research/configs/mtf_scalp4h_v1_{market}.json").read_text())
    if cfg["strategy_version"] != STRATEGY_VERSION or cfg["market"] != market:
        raise ValueError("config does not match the strategy version / market")
    if cfg["risk"]["max_intended_risk_per_trade"] != 0.005:
        raise ValueError("risk per trade must stay 0.5%")
    return cfg


def _cands(kind: str, params) -> pd.DataFrame:
    if kind == "B":
        return base._cands("B", params, "1m")
    key = ("B4H", json.dumps(asdict(params), sort_keys=True))
    if key not in base._S["cands"]:
        base._S["cands"][key] = scalp4h_candidates(base._S["frames1"], params, allow_short=base._S["allow_short"])
    return base._S["cands"][key]


def _select_task(task: dict) -> dict:
    best, best_score = None, -np.inf
    for params in task["grid"]:
        s = summarize(base._sim(_cands(task["kind"], params), "1m", *task["train"]), resamples=10)
        score = s["net_return_pct"] if s["trades"] >= task["min_trades"] else -np.inf
        if score > best_score:
            best, best_score = params, score
    return {"window": task["window"], "selected": best}


def _run_task(task: dict) -> dict:
    if task["kind"] == "random":
        ref = _cands(task["ref_kind"], task["params"])
        frames = base._S["frames1"]
        a = atr(*(frames["5m"].df[k].to_numpy() for k in ("high", "low", "close")), 14)
        atr_v = take(a, align(frames["1m"], frames["5m"]))
        cands = pd.concat([random_candidates(ref[(ref.time_ns >= s) & (ref.time_ns < e)], frames["1m"], atr_v,
                                             seed=task["seed"], start_ns=s, end_ns=e, allow_short=base._S["allow_short"])
                           for s, e in task["spans"]], ignore_index=True)
    else:
        cands = _cands(task["kind"], task["params"])
    res = base._sim(cands, "1m", task["span"][0], task["span"][1], task.get("scenario", "base"),
                    task.get("rules", AccountRules()))
    return {"id": task["id"], "trades": res["trades"], "rejections": res["rejections"],
            "final_equity": res["final_equity"], "initial_equity": res["initial_equity"], "candidates": res["candidates"]}


def _grid(kind: str, cfg: dict, h1_confirm: bool = True) -> list:
    proto = ScalpParams() if kind == "B" else Scalp4HParams(h1_confirm=h1_confirm)
    return param_grid(proto, cfg["grid"])


def _bootstrap_diff(a: list[float], b: list[float], n: int = 5000, seed: int = 11) -> dict:
    """Mean R difference (a - b) with an independent-samples bootstrap 95% interval."""
    if not a or not b:
        return {"diff": None, "ci95": None, "p_a_le_b": None}
    rng = np.random.default_rng(seed)
    xa, xb = np.asarray(a), np.asarray(b)
    d = rng.choice(xa, (n, xa.size)).mean(1) - rng.choice(xb, (n, xb.size)).mean(1)
    return {"diff": round(float(xa.mean() - xb.mean()), 4),
            "ci95": (round(float(np.percentile(d, 2.5)), 4), round(float(np.percentile(d, 97.5)), 4)),
            "p_a_le_b": round(float((d <= 0).mean()), 4)}


def _quality(trades: list[dict]) -> dict:
    if not trades:
        return {}
    reasons = defaultdict(int)
    for t in trades:
        reasons[t.get("exit_reason", "?")] += 1
    hold = [(t["closed_ns"] - t["opened_ns"]) / 60e9 for t in trades]
    return {"median_hold_min": round(statistics.median(hold), 1), "exit_reasons": dict(reasons),
            "share_cost_over_quarter_r": round(sum(1 for t in trades if t["r_gross"] - t["r"] > 0.25) / len(trades), 3)}


def evaluate(pool, kind: str, grid: list, wins: list, cfg: dict, label: str) -> dict:
    sel = list(pool.map(_select_task, [{"kind": kind, "grid": grid, "train": w["train"], "min_trades": cfg["min_train_trades"],
                                        "window": k} for k, w in enumerate(wins)]))
    chosen = {s["window"]: s["selected"] for s in sel}
    tests = [{"id": f"{label}-oos-{k}", "kind": kind, "params": chosen[k], "span": w["test"]}
             for k, w in enumerate(wins) if chosen[k] is not None]
    oos = list(pool.map(_run_task, tests))
    spans = [t["span"] for t in tests]
    oos_span = (min(s[0] for s in spans), max(s[1] for s in spans)) if spans else None
    most = statistics.mode([json.dumps(asdict(p), sort_keys=True) for p in chosen.values() if p]) if tests else None
    ref = type(grid[0])(**json.loads(most)) if most else grid[0]
    rand = list(pool.map(_run_task, [{"id": f"{label}-rand-{s}", "kind": "random", "ref_kind": kind, "params": ref,
                                      "seed": s, "span": oos_span, "spans": spans}
                                     for s in range(1, cfg["random_seeds"] + 1)])) if tests else []
    full = (wins[0]["train"][0], wins[-1]["test"][1])
    sens = list(pool.map(_run_task, [{"id": f"{label}-grid-{k}", "kind": kind, "params": p, "span": full}
                                     for k, p in enumerate(grid)]))
    scen = {sc: base._chain(list(pool.map(_run_task, [{**t, "id": f"{t['id']}-{sc}", "scenario": sc} for t in tests])), 10_000.0)
            for sc in cfg["cost_scenarios"] if sc != "base"}
    small = base._chain(list(pool.map(_run_task, [{**t, "rules": AccountRules(initial_equity=3000.0)} for t in tests])), 3000.0)
    halts = {h: summarize(base._chain(list(pool.map(_run_task, [{**t, "rules": r} for t in tests])), 10_000.0))
             for h, r in (("weekly_3pct", AccountRules(weekly_loss_halt=0.03)),
                          ("consecutive_3", AccountRules(consecutive_loss_halt=3)),
                          ("session_cap_2", AccountRules(session_entry_cap=2)))}
    chain = base._chain(oos, 10_000.0)
    s = summarize(chain)
    rmeans = sorted(m for m in (summarize(r)["mean_r"] for r in rand) if m is not None)
    keys = tuple(cfg["grid"])
    per_window = [{"test": wins[int(t["id"].rsplit("-", 1)[1])]["label"], "trades": len(r["trades"]),
                   "net_return_pct": round((r["final_equity"] / r["initial_equity"] - 1) * 100, 3)}
                  for t, r in zip(tests, oos)]
    return {"oos": s, "trade_r": [t["r"] for t in chain["trades"]], "trade_keys": [(t["signal_ns"], t["side"]) for t in chain["trades"]],
            "quality": _quality(chain["trades"]), "windows_tested": len(tests), "per_window": per_window,
            "profitable_windows": f"{sum(1 for w in per_window if w['net_return_pct'] > 0)}/{len(per_window)}",
            "random_mean_r": {"median": statistics.median(rmeans) if rmeans else None,
                              "min": rmeans[0] if rmeans else None, "max": rmeans[-1] if rmeans else None},
            "beats_random": f"{sum(1 for m in rmeans if s['mean_r'] is not None and s['mean_r'] > m)}/{len(rmeans)}",
            "grid": [{"params": {k: asdict(p)[k] for k in keys}, "trades": len(r["trades"]),
                      "net_return_pct": round((r["final_equity"] / r["initial_equity"] - 1) * 100, 3)}
                     for p, r in zip(grid, sens)],
            "grid_profitable": f"{sum(1 for r in sens if r['final_equity'] > r['initial_equity'])}/{len(sens)}",
            "most_selected": {k: asdict(ref)[k] for k in keys},
            "scenarios": {sc: summarize(c) for sc, c in scen.items()},
            "eligibility_3000": {"trades": len(small["trades"]), "min_size_rejections":
                                 sum(v for k, v in small["rejections"].items() if k.startswith(("Minimum size", "Notional")))},
            "research_halts": halts,
            "breakdowns": base._breakdowns(chain["trades"], base._regime_fn(base._S["frames1"]))}


def run(market: str, workers: int) -> dict:
    cfg = load_config(market)
    base._init(market)
    b1 = base._S["base1"].df
    end = base.BTC_END if market == "btcusdt" else b1.index[-1]
    wins = base.windows(b1.index[0], end, cfg["walk_forward_months"]["train"], cfg["walk_forward_months"]["test"])
    report = {"market": market, "strategy_version": STRATEGY_VERSION, "research_only": True, "config": cfg,
              "data_1m": [str(b1.index[0]), str(b1.index[-1]), len(b1)], "windows": [w["label"] for w in wins],
              "variants": {}}
    grids = {"B": _grid("B", cfg), "B4H_only": _grid("B4H", cfg, h1_confirm=False), "B4H": _grid("B4H", cfg)}
    with ProcessPoolExecutor(workers, initializer=base._init, initargs=(market,)) as pool:
        for name, grid in grids.items():
            kind = "B" if name == "B" else "B4H"
            ev = evaluate(pool, kind, grid, wins, cfg, f"{market[:3]}-{name}")
            report["variants"][name] = ev
            o = ev["oos"]
            print(name, o["trades"], o["net_return_pct"], o["mean_r"], o["mean_r_ci95"], o["max_drawdown_pct"],
                  "rand", ev["beats_random"], flush=True)
    v = report["variants"]
    base_keys = set(map(tuple, v["B"]["trade_keys"]))
    report["comparison"] = {
        name: {"mean_r_minus_B": _bootstrap_diff(v[name]["trade_r"], v["B"]["trade_r"]),
               "trades_shared_with_B": sum(1 for k in v[name]["trade_keys"] if tuple(k) in base_keys)}
        for name in ("B4H_only", "B4H")}
    report["comparison"]["B4H_minus_B4H_only"] = _bootstrap_diff(v["B4H"]["trade_r"], v["B4H_only"]["trade_r"])
    original = Path(f"research/output/mtf/{market}_mtf_research.json")
    if original.exists():
        key = "B" if market == "btcusdt" else "B_explore"
        orig = json.loads(original.read_text())["strategies"][key]["oos"]
        report["baseline_reproduction"] = {k: (orig[k], v["B"]["oos"][k]) for k in ("trades", "net_return_pct", "mean_r")}
    return report


def to_markdown(r: dict) -> str:
    lines = [f"# 4H-led scalping ({r['strategy_version']}) vs H1-led baseline - {r['market'].upper()}", "",
             f"Evidence status: {r['config']['evidence_status']}", f"1m data: {r['data_1m']}; windows: {len(r['windows'])}", "",
             "| Variant | Trades | Net % | Mean R (95% CI) | Gross R | Cost R | Win % | PF | Max DD % | Sharpe | Profitable windows | Beats random | Grid profitable | Stress net % |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, e in r["variants"].items():
        o = e["oos"]
        lines.append(f"| {name} | {o['trades']} | {o['net_return_pct']} | {o['mean_r']} {o['mean_r_ci95']} | {o['mean_r_gross']} | "
                     f"{o['cost_r']} | {o['win_rate_pct']} | {o['profit_factor']} | {o['max_drawdown_pct']} | "
                     f"{o['sharpe_daily_annualized']} | {e['profitable_windows']} | {e['beats_random']} | {e['grid_profitable']} | "
                     f"{e['scenarios'].get('stress', {}).get('net_return_pct')} |")
    lines += ["", f"Comparison: {json.dumps(r['comparison'])}", "", f"Baseline reproduction (original, rerun): {r.get('baseline_reproduction')}"]
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--market", choices=("btcusdt", "xauusd"), required=True)
    p.add_argument("--workers", type=int, default=6)
    a = p.parse_args()
    report = run(a.market, a.workers)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{a.market}.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (OUT / f"{a.market}.md").write_text(to_markdown(report), encoding="utf-8")
    print(to_markdown(report))


if __name__ == "__main__":
    main()
