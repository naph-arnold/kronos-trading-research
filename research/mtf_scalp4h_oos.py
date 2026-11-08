"""Frozen out-of-sample evaluation of XAUUSD scalping variants on 2019-2020 (research only; no orders).

Implements research/forward_test/xau_scalp4h_oos_2019_2020_preregistration.json exactly:
three frozen variants (H1-led B, 4H-only, 4H+1H), identical costs and evaluation window, no parameter selection.

    python -m research.mtf_scalp4h_oos [--workers 6]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from research import mtf_research as base
from research.mtf_scalp4h_research import _bootstrap_diff, _quality
from trading.common import Market
from trading.evaluation import param_grid
from trading.mtf.features import atr
from trading.mtf.frames import Frame, align, from_csv, take, validate_base
from trading.mtf.scalp4h import Scalp4HParams, scalp4h_candidates
from trading.mtf.simulator import AccountRules, MarketSpec, simulate, summarize
from trading.mtf.strategies import ScalpParams, random_candidates, scalp_candidates
from trading.risk import RiskLimits

PREREG = Path("research/forward_test/xau_scalp4h_oos_2019_2020_preregistration.json")
DATA = Path("data/raw/mt5_xauusd_1m_ticks_2019_2020.csv")
EVENTS = Path("data/processed/fomc_events_2019_2020.json")
OUT = Path("research/output/mtf/scalp4h_v1_oos_2019_2020")
START = pd.Timestamp("2019-02-01T00:00Z")
END = pd.Timestamp("2021-01-01T00:00Z")
GRID = {"reward_to_risk": [1.75, 2.0, 2.5], "pull_atr": [0.25, 0.5], "trigger_bars": [3, 5]}
_S: dict = {}


def frozen_variants(prereg: dict) -> dict:
    fv = prereg["frozen_variants"]
    return {"B": ScalpParams(**fv["B"]["params"]),
            "B4H_only": Scalp4HParams(h1_confirm=False, **fv["B4H_only"]["params"]),
            "B4H": Scalp4HParams(h1_confirm=True, **fv["B4H"]["params"])}


def _init() -> None:
    manifest = json.loads(DATA.with_suffix(".csv.json").read_text())
    if hashlib.sha256(DATA.read_bytes()).hexdigest() != manifest["sha256"]:
        raise ValueError("dataset checksum mismatch")
    if not json.loads(DATA.with_suffix(".csv.validation.json").read_text())["passed"]:
        raise ValueError("dataset did not pass validation")
    cols = {"open": "open", "high": "high", "low": "low", "close": "close", "volume": "tick_volume"}
    f1 = Frame("1m", from_csv(DATA, time_col="open_time_utc", columns=cols, spread_col="spread_points", spread_scale=0.01))
    validate_base(f1.df, "1m")
    from trading.marketcal.holidays import build_xauusd_calendar
    from trading.sessions import XAUUSD_NY_MARKET_HOURS
    cal, _ = build_xauusd_calendar(XAUUSD_NY_MARKET_HOURS, f1.df.index[0].date(), f1.df.index[-1].date() + pd.Timedelta(days=2))
    _S.update(base1=f1, frames=base.frames_for(f1), calendar=cal, cands={},
              hist_spread=manifest["spread_by_new_york_hour_p50_p90"], prereg=json.loads(PREREG.read_text()))
    a5 = atr(*(_S["frames"]["5m"].df[k].to_numpy() for k in ("high", "low", "close")), 14)
    _S["atr5_on_1m"] = take(a5, align(f1, _S["frames"]["5m"]))


def spec(commission: str = "0", slippage: str = "0.10", spread: str = "2026_p50") -> MarketSpec:
    """Same construction as research.mtf_research.market_spec('xauusd'), parameterised for the cost grid."""
    from trading.clock import EET_EU
    from trading.connectors.mt5 import SpecDiscovery
    from trading.marketcal.blackout import EventBlackout
    from trading.marketcal.events import EconomicEvent, EventStore
    from trading.shadow.costs import SwapSpec
    rec = json.loads(Path("data/processed/mt5_xauusd_discovery.json").read_text())["specification"]
    disc = SpecDiscovery(rec["symbol"], rec["retrieved_at"], rec["fields"], rec["missing"], rec["problems"])
    inst = disc.to_instrument_spec(commission_assumption=Decimal(commission))
    limits = RiskLimits(market=Market.XAUUSD_MT5, max_spread=Decimal("1.00"), slippage_allowance=Decimal(slippage))
    if spread.startswith("2026"):
        prof = json.loads(Path("data/processed/mt5_xauusd_tick_spreads.json").read_text())["by_new_york_hour_p50_p90"]
        default = [0.17, 0.45]
    else:
        prof, default = _S["hist_spread"], [0.27, 0.45]
    q = 0 if spread.endswith("p50") else 1
    table = np.array([prof.get(str(h), default)[q] for h in range(24)], dtype=float)

    def spread_fn(t):
        hours = pd.DatetimeIndex(pd.to_datetime(t, utc=True)).tz_convert("America/New_York").hour
        return table[np.asarray(hours)]
    store = EventStore()
    store.upsert([EconomicEvent.from_json(e) for e in json.loads(EVENTS.read_text())])
    swap = SwapSpec(Decimal("-12.6"), Decimal("-4.6"), Decimal("1"), "Wednesday", EET_EU,
                    "mt5.symbol_info 2026-11-07 (verified)", True)

    def day_key(ns):
        return (pd.Timestamp(ns, tz="UTC").tz_convert("America/New_York") + pd.Timedelta(hours=7)).date()
    return MarketSpec(Market.XAUUSD_MT5, inst, limits, spread_fn, "bid", day_key, base._session, calendar=_S["calendar"],
                      event_guard=EventBlackout(store=store, market=Market.XAUUSD_MT5, max_calendar_age=None), swap=swap)


def _cands(params) -> pd.DataFrame:
    key = (type(params).__name__, json.dumps(asdict(params), sort_keys=True))
    if key not in _S["cands"]:
        fn = scalp_candidates if isinstance(params, ScalpParams) else scalp4h_candidates
        _S["cands"][key] = fn(_S["frames"], params, allow_short=True)
    return _S["cands"][key]


def _task(t: dict) -> dict:
    ref = _cands(t["params"])
    if t.get("seed") is not None:
        cands = random_candidates(ref[(ref.time_ns >= START.value) & (ref.time_ns < END.value)], _S["frames"]["1m"],
                                  _S["atr5_on_1m"], seed=t["seed"], start_ns=START.value, end_ns=END.value, allow_short=True)
    else:
        cands = ref
    res = simulate(cands, _S["base1"], spec(*t.get("cost", ("0", "0.10", "2026_p50"))), t.get("rules", AccountRules()),
                   start_ns=START.value, end_ns=END.value)
    return {"id": t["id"], **{k: res[k] for k in ("trades", "rejections", "final_equity", "initial_equity", "candidates")}}


def drawdown_analysis(result: dict) -> dict:
    trades = sorted(result["trades"], key=lambda x: x["closed_ns"])
    if not trades:
        return {}
    eq = np.array([result["initial_equity"]] + [t["equity_after"] for t in trades])
    times = [pd.Timestamp(trades[0]["opened_ns"], tz="UTC")] + [pd.Timestamp(t["closed_ns"], tz="UTC") for t in trades]
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / peak
    i = int(dd.argmax())
    start = int(eq[: i + 1].argmax())
    rec = next((k for k in range(i, len(eq)) if eq[k] >= peak[i]), None)
    longest, cur, durations, last_peak = 0, 0, [], times[0]
    for k, t in enumerate(trades):
        cur = cur + 1 if t["pnl"] <= 0 else 0
        longest = max(longest, cur)
    for k in range(1, len(eq)):
        if eq[k] >= peak[k - 1]:
            durations.append((times[k] - last_peak).days)
            last_peak = times[k]
    durations.append((times[-1] - last_peak).days)
    monthly = defaultdict(float)
    for t in trades:
        monthly[pd.Timestamp(t["closed_ns"], tz="UTC").strftime("%Y-%m")] += t["pnl"]
    worst = min(monthly.items(), key=lambda kv: kv[1])
    return {"max_drawdown_pct": round(float(dd.max() * 100), 3), "peak_at": str(times[start]), "trough_at": str(times[i]),
            "recovered_at": str(times[rec]) if rec is not None else "not recovered",
            "longest_underwater_days": int(max(durations)), "longest_losing_streak": longest,
            "worst_month": [worst[0], round(worst[1], 2)], "profitable_months": f"{sum(v > 0 for v in monthly.values())}/{len(monthly)}",
            "monthly_pnl": {k: round(v, 2) for k, v in sorted(monthly.items())}}


def run(workers: int) -> dict:
    _init()
    prereg = _S["prereg"]
    variants = frozen_variants(prereg)
    stress = [(c, s, "2026_p50") for c in ("0", "7", "14") for s in ("0.10", "0.30", "0.50")]
    hist = [("0", "0.10", "hist_p50"), ("0", "0.10", "hist_p90"), ("7", "0.30", "hist_p50")]
    tasks = []
    for name, p in variants.items():
        tasks += [{"id": f"{name}|base", "params": p}]
        tasks += [{"id": f"{name}|cost|{c}|{s}|{sp}", "params": p, "cost": (c, s, sp)} for c, s, sp in stress + hist
                  if (c, s, sp) != ("0", "0.10", "2026_p50")]
        tasks += [{"id": f"{name}|rand|{k}", "params": p, "seed": k} for k in range(1, 21)]
        tasks += [{"id": f"{name}|3k", "params": p, "rules": AccountRules(initial_equity=3000.0)}]
        tasks += [{"id": f"{name}|halt|{h}", "params": p, "rules": r} for h, r in
                  (("weekly_3pct", AccountRules(weekly_loss_halt=0.03)), ("consecutive_3", AccountRules(consecutive_loss_halt=3)),
                   ("session_cap_2", AccountRules(session_entry_cap=2)))]
        proto = ScalpParams() if name == "B" else Scalp4HParams(h1_confirm=(name == "B4H"))
        tasks += [{"id": f"{name}|grid|{k}", "params": g} for k, g in enumerate(param_grid(proto, GRID))]
    with ProcessPoolExecutor(workers, initializer=_init) as pool:
        results = {r["id"]: r for r in pool.map(_task, tasks)}
    regime = base._regime_fn(_S["frames"])
    report = {"preregistration": {"path": str(PREREG), "sha256": hashlib.sha256(PREREG.read_bytes()).hexdigest()},
              "data": {"path": str(DATA), "sha256": json.loads(DATA.with_suffix(".csv.json").read_text())["sha256"],
                       "evaluation_window": [str(START), str(END)]}, "variants": {}}
    for name, p in variants.items():
        b = results[f"{name}|base"]
        s = summarize(b)
        rm = sorted(m for m in (summarize(results[f"{name}|rand|{k}"])["mean_r"] for k in range(1, 21)) if m is not None)
        trades = b["trades"]
        by_q = defaultdict(list)
        for t in trades:
            ts = pd.Timestamp(t["opened_ns"], tz="UTC")
            by_q[f"{ts.year}Q{(ts.month - 1) // 3 + 1}"].append(t["r"])
        report["variants"][name] = {
            "params": asdict(p), "base": s, "quality": _quality(trades), "drawdown": drawdown_analysis(b),
            "random_mean_r": {"median": statistics.median(rm) if rm else None, "min": rm[0] if rm else None, "max": rm[-1] if rm else None},
            "beats_random": f"{sum(1 for m in rm if s['mean_r'] is not None and s['mean_r'] > m)}/{len(rm)}",
            "costs": {k.split("|", 2)[2]: {kk: v for kk, v in summarize(r).items() if kk in ("trades", "net_return_pct", "mean_r", "mean_r_ci95", "max_drawdown_pct", "cost_r")}
                      for k, r in results.items() if k.startswith(f"{name}|cost|")},
            "eligibility_3000": {"trades": len(results[f"{name}|3k"]["trades"]), "min_size_rejections": sum(
                v for k, v in results[f"{name}|3k"]["rejections"].items() if k.startswith(("Minimum size", "Notional")))},
            "research_halts": {k.rsplit("|", 1)[1]: {kk: summarize(r)[kk] for kk in ("trades", "net_return_pct", "max_drawdown_pct")}
                               for k, r in results.items() if k.startswith(f"{name}|halt|")},
            "breakdowns": {**base._breakdowns(trades, regime),
                           "quarter": {q: {"trades": len(v), "mean_r": round(statistics.fmean(v), 4)} for q, v in sorted(by_q.items())}},
            "rejections": b["rejections"],
            "diagnostic_grid_not_used_for_selection": [
                {"params": {k: asdict(g)[k] for k in GRID}, "trades": len(results[f"{name}|grid|{i}"]["trades"]),
                 "net_return_pct": summarize(results[f"{name}|grid|{i}"], resamples=10)["net_return_pct"],
                 "mean_r": summarize(results[f"{name}|grid|{i}"], resamples=10)["mean_r"]}
                for i, g in enumerate(param_grid(ScalpParams() if name == "B" else Scalp4HParams(h1_confirm=(name == "B4H")), GRID))],
            "_r": [t["r"] for t in trades]}
    v = report["variants"]
    report["differences"] = {"B4H_minus_B": _bootstrap_diff(v["B4H"]["_r"], v["B"]["_r"]),
                             "B4H_only_minus_B": _bootstrap_diff(v["B4H_only"]["_r"], v["B"]["_r"]),
                             "B4H_minus_B4H_only": _bootstrap_diff(v["B4H"]["_r"], v["B4H_only"]["_r"])}
    for x in v.values():
        x.pop("_r")
    b4 = v["B4H"]
    years = b4["breakdowns"]["year"]
    checks = {"base_mean_r_ci_lower_gt_0": bool(b4["base"]["mean_r_ci95"] and b4["base"]["mean_r_ci95"][0] > 0 and b4["base"]["mean_r"] > 0),
              "net_positive_commission7_slippage0.30": b4["costs"].get("7|0.30|2026_p50", {}).get("net_return_pct", -1) > 0,
              "beats_random_at_least_18_of_20": int(b4["beats_random"].split("/")[0]) >= 18,
              "positive_mean_r_2019_and_2020": all(years.get(y, {}).get("mean_r", -1) > 0 for y in ("2019", "2020"))}
    report["decision"] = {"checks": checks, "B4H_supported": all(checks.values()),
                          "improvement_over_B_supported": bool(report["differences"]["B4H_minus_B"]["ci95"]
                                                               and report["differences"]["B4H_minus_B"]["ci95"][0] > 0)}
    return report


def to_markdown(r: dict) -> str:
    lines = ["# XAUUSD scalping, frozen variants on unexamined 2019-2020 history", "",
             f"Pre-registration sha256 {r['preregistration']['sha256']}; data sha256 {r['data']['sha256']}", "",
             "| Variant | Trades | Net % | Mean R (95% CI) | Gross R | Cost R | Win % | PF | Max DD % | Underwater days | Beats random | Hist-spread net % | Comm 7 + slip 0.30 net % |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for n, e in r["variants"].items():
        o, d = e["base"], e["drawdown"]
        lines.append(f"| {n} | {o['trades']} | {o['net_return_pct']} | {o['mean_r']} {o['mean_r_ci95']} | {o['mean_r_gross']} | {o['cost_r']} | "
                     f"{o['win_rate_pct']} | {o['profit_factor']} | {o['max_drawdown_pct']} | {d.get('longest_underwater_days')} | {e['beats_random']} | "
                     f"{e['costs'].get('0|0.10|hist_p50', {}).get('net_return_pct')} | {e['costs'].get('7|0.30|2026_p50', {}).get('net_return_pct')} |")
    lines += ["", f"Differences: {json.dumps(r['differences'], default=str)}", "", f"Decision: {json.dumps(r['decision'], default=str)}"]
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workers", type=int, default=6)
    a = p.parse_args()
    report = run(a.workers)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (OUT / "report.md").write_text(to_markdown(report), encoding="utf-8")
    print(to_markdown(report))


if __name__ == "__main__":
    main()
