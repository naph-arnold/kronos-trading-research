"""Multi-timeframe hybrid research: Strategy A (selective), B (scalping), C (hybrid) on BTC/USDT and XAUUSD.
Research only; no orders. Each market is evaluated independently with its own account, costs and sessions.

    python -m research.mtf_research --market btcusdt|xauusd [--workers 8]

Data discipline:
* BTC/USDT: research span ends 2026-05-24T20:00Z (the Phase 2B holdout and everything later are excluded).
  A: 5m execution 2019-01 -> 2026-05; B and C: 1m execution 2023-01 -> 2026-05 (1m download starts 2023).
* XAUUSD: MetaQuotes-Demo history is capped at 100,000 bars per timeframe (M5 from 2025-06-02, M1 from
  2026-07-27). Development data = before 2025-12-30 (Phase 2A training boundary). Everything from 2025-12-30 was
  examined in Phase 2A and is reported only as EXPLORATORY (not fresh evidence). B and C exist only there.
Selection: rolling walk-forward (parameters chosen on each training window, evaluated once on the next window).
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

from trading.common import Market
from trading.evaluation import param_grid
from trading.mtf.features import atr, ema
from trading.mtf.frames import Frame, from_csv, resample, validate_base
from trading.mtf.simulator import AccountRules, MarketSpec, simulate, summarize
from trading.mtf.strategies import (ScalpParams, SelectiveParams, random_candidates, scalp_candidates,
                                    selective_candidates, trend_baseline_candidates)
from trading.risk import RiskLimits

NS_DAY = 86_400_000_000_000
BTC_END = pd.Timestamp("2026-05-24T20:00Z")
XAU_DEV_END = pd.Timestamp("2025-12-30T00:00Z")
GRID_A = {"reward_to_risk": [2.5, 3.0, 4.0], "pull_atr": [0.25, 0.5], "confirm_bars": [3, 6]}
GRID_B = {"reward_to_risk": [1.75, 2.0, 2.5], "pull_atr": [0.25, 0.5], "trigger_bars": [3, 5]}
_S: dict = {}


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _session(ns: int) -> str:
    h = (ns // 3_600_000_000_000) % 24
    return "asia" if h < 7 else "london" if h < 13 else "new_york" if h < 21 else "late"


# ---------------------------------------------------------------------------- data and market specs

def load_market(market: str) -> dict:
    if market == "btcusdt":
        from research.phase2e_btc_history import load_rows  # verifies checksums and recorded gaps
        frames = {}
        for iv, path in (("5m", "data/raw/binance_btcusdt_5m_2019-01_2026-11.csv"),
                         ("1m", "data/raw/binance_btcusdt_1m_2023-01_2026-11.csv")):
            load_rows(Path(path))
            df = from_csv(path, time_col="timestamps", columns={k: k for k in ("open", "high", "low", "close", "volume")})
            df = df[df.index < BTC_END]
            validate_base(df, iv)
            frames[iv] = Frame(iv, df)
        return {"market": Market.BTCUSDT_BINANCE_SPOT, "base5": frames["5m"], "base1": frames["1m"],
                "allow_short": False, "end": BTC_END}
    for iv in ("5m", "1m"):
        manifest = json.loads(Path(f"data/raw/mt5_xauusd_{iv}.csv.json").read_text())
        if _sha(Path(f"data/raw/mt5_xauusd_{iv}.csv")) != manifest["sha256"]:
            raise ValueError(f"checksum mismatch for XAUUSD {iv}")
    cols = {"open": "open", "high": "high", "low": "low", "close": "close", "volume": "tick_volume"}
    f5 = Frame("5m", from_csv("data/raw/mt5_xauusd_5m.csv", time_col="open_time_utc", columns=cols,
                              spread_col="spread_points", spread_scale=0.01))
    f1 = Frame("1m", from_csv("data/raw/mt5_xauusd_1m.csv", time_col="open_time_utc", columns=cols,
                              spread_col="spread_points", spread_scale=0.01))
    validate_base(f5.df, "5m")
    validate_base(f1.df, "1m")
    return {"market": Market.XAUUSD_MT5, "base5": f5, "base1": f1, "allow_short": True,
            "end": pd.Timestamp(f5.df.index[-1])}


def frames_for(base: Frame) -> dict:
    out = {base.interval: base}
    for iv in ("5m", "15m", "1h", "4h"):
        if iv != base.interval:
            out[iv] = resample(base, iv)
    return out


def market_spec(market: str, scenario: str, *, equity_rules=None) -> MarketSpec:
    if market == "btcusdt":
        from trading.connectors.binance import parse_symbol
        cfg = {"base": ("0.001", 0.01, "2"), "stress": ("0.001", None, "10"), "low_fee": ("0.00075", 0.01, "2")}[scenario]
        snap = json.loads(Path("data/processed/binance_btcusdt_exchangeinfo_public.json").read_text())
        inst = parse_symbol(snap["symbol"], source="binance_public_market_data", taker_fee=Decimal(cfg[0]),
                            fee_source="assumed").to_instrument_spec(allow_assumed_fee=True)
        limits = RiskLimits(market=Market.BTCUSDT_BINANCE_SPOT, max_spread=Decimal("100"),
                            slippage_allowance=Decimal(cfg[2]), max_entry_deviation_fraction=Decimal("0.005"))
        if cfg[1] is None:  # stress: 1 bp of price (Phase 2B/2E convention)
            closes = _S["bps_ref"]
            spread_fn = lambda t: np.interp(t, closes[0], closes[1]) * 0.0001  # noqa: E731
        else:
            spread_fn = lambda t, s=cfg[1]: np.full(len(t), s)  # noqa: E731
        return MarketSpec(Market.BTCUSDT_BINANCE_SPOT, inst, limits, spread_fn, "mid",
                          lambda ns: ns // NS_DAY, _session)
    from trading.clock import EET_EU
    from trading.connectors.mt5 import SpecDiscovery
    from trading.marketcal.blackout import EventBlackout
    from trading.marketcal.events import EconomicEvent, EventStore
    from trading.shadow.costs import SwapSpec
    rec = json.loads(Path("data/processed/mt5_xauusd_discovery.json").read_text())["specification"]
    disc = SpecDiscovery(rec["symbol"], rec["retrieved_at"], rec["fields"], rec["missing"], rec["problems"])
    commission, slip, quantile = {"base": ("0", "0.10", 0), "stress": ("7", "0.30", 1)}[scenario]
    inst = disc.to_instrument_spec(commission_assumption=Decimal(commission))
    limits = RiskLimits(market=Market.XAUUSD_MT5, max_spread=Decimal("1.00"), slippage_allowance=Decimal(slip))
    prof = json.loads(Path("data/processed/mt5_xauusd_tick_spreads.json").read_text())["by_new_york_hour_p50_p90"]
    table = np.array([prof.get(str(h), prof.get(h, [0.17, 0.45]))[quantile] for h in range(24)], dtype=float)

    def spread_fn(t):
        hours = pd.DatetimeIndex(pd.to_datetime(t, utc=True)).tz_convert("America/New_York").hour
        return table[np.asarray(hours)]
    store = EventStore()
    store.upsert([EconomicEvent.from_json(e) for e in json.loads(Path("data/processed/fomc_events.json").read_text())])
    swap = SwapSpec(Decimal("-12.6"), Decimal("-4.6"), Decimal("1"), "Wednesday", EET_EU,
                    "mt5.symbol_info 2026-11-07 (verified)", True)

    def day_key(ns):  # risk day = New York date of (time + 7 h): rolls at 17:00 New York (gold daily break)
        return (pd.Timestamp(ns, tz="UTC").tz_convert("America/New_York") + pd.Timedelta(hours=7)).date()
    return MarketSpec(Market.XAUUSD_MT5, inst, limits, spread_fn, "bid", day_key, _session,
                      calendar=_S["xau_calendar"], event_guard=EventBlackout(store=store, market=Market.XAUUSD_MT5,
                                                                             max_calendar_age=None), swap=swap)


def _init(market: str) -> None:
    data = load_market(market)
    _S.update(data, market_name=market)
    _S["frames5"] = frames_for(data["base5"])
    _S["frames1"] = frames_for(data["base1"])
    b = data["base5"].df
    _S["bps_ref"] = (data["base5"].open_time, b["close"].to_numpy())
    if market == "xauusd":
        from trading.marketcal.holidays import build_xauusd_calendar
        from trading.sessions import XAUUSD_NY_MARKET_HOURS
        cal, _ = build_xauusd_calendar(XAUUSD_NY_MARKET_HOURS, b.index[0].date(), b.index[-1].date() + pd.Timedelta(days=2))
        _S["xau_calendar"] = cal
    _S["cands"] = {}


def _cands(kind: str, params, exec_tf: str) -> pd.DataFrame:
    key = (kind, json.dumps(asdict(params), sort_keys=True), exec_tf)
    if key not in _S["cands"]:
        frames = _S["frames5"] if exec_tf == "5m" else _S["frames1"]
        if kind == "A":
            c = selective_candidates(frames, params, allow_short=_S["allow_short"])
        else:
            c = scalp_candidates(frames, params, allow_short=_S["allow_short"])
        _S["cands"][key] = c
    return _S["cands"][key]


def _base(exec_tf: str) -> Frame:
    return _S["base5"] if exec_tf == "5m" else _S["base1"]


def _sim(cands, exec_tf, start_ns, end_ns, scenario="base", rules=AccountRules()):
    spec = market_spec(_S["market_name"], scenario)
    return simulate(cands, _base(exec_tf), spec, rules, start_ns=start_ns, end_ns=end_ns)


# ---------------------------------------------------------------------------- tasks (run in workers)

def _select_task(task: dict) -> dict:
    """Pick the best grid configuration on one training window (objective: net return with >= min trades)."""
    best, best_score = None, -np.inf
    for params in task["grid"]:
        res = _sim(_cands(task["kind"], params, task["exec_tf"]), task["exec_tf"], task["train"][0], task["train"][1])
        s = summarize(res, resamples=10)
        score = s["net_return_pct"] if s["trades"] >= task["min_trades"] else -np.inf
        if score > best_score:
            best, best_score = params, score
    return {"window": task["window"], "selected": best}


def _run_task(task: dict) -> dict:
    """Simulate a candidate set (strategy / hybrid / baseline) on one span and scenario; return trades."""
    kind = task["kind"]
    if kind == "hybrid":
        a = _cands("A", task["params_a"], "1m")
        b = _cands("B", task["params_b"], "1m")
        cands = pd.concat([a, b], ignore_index=True)
    elif kind in ("A", "B"):
        cands = _cands(kind, task["params"], task["exec_tf"])
    elif kind == "trend":
        cands = trend_baseline_candidates(_S["frames5"] if task["exec_tf"] == "5m" else _S["frames1"],
                                          allow_short=_S["allow_short"])
    else:  # random, matched to a reference candidate set
        ref = _cands(task["ref_kind"], task["params"], task["exec_tf"])
        frames = _S["frames5"] if task["exec_tf"] == "5m" else _S["frames1"]
        frame = frames["5m"] if task["exec_tf"] == "5m" else frames["1m"]
        atr_tf = frames["15m"] if task["ref_kind"] == "A" else frames["5m"]
        from trading.mtf.frames import align, take
        a = atr(*(atr_tf.df[k].to_numpy() for k in ("high", "low", "close")), 14)
        atr_v = take(a, align(frame, atr_tf))
        cands = pd.concat([random_candidates(ref[(ref.time_ns >= s) & (ref.time_ns < e)], frame, atr_v, seed=task["seed"],
                                             start_ns=s, end_ns=e, allow_short=_S["allow_short"])
                           for s, e in task["spans"]], ignore_index=True)
    res = _sim(cands, task["exec_tf"], task["span"][0], task["span"][1], task.get("scenario", "base"),
               task.get("rules", AccountRules()))
    return {"id": task["id"], "trades": res["trades"], "rejections": res["rejections"],
            "final_equity": res["final_equity"], "initial_equity": res["initial_equity"], "candidates": res["candidates"]}


# ---------------------------------------------------------------------------- orchestration helpers

def windows(start: pd.Timestamp, end: pd.Timestamp, train_months: int, test_months: int) -> list:
    out, s = [], start
    while s + pd.DateOffset(months=train_months + test_months) <= end + pd.Timedelta(days=1):
        tr_end = s + pd.DateOffset(months=train_months)
        te_end = tr_end + pd.DateOffset(months=test_months)
        out.append({"train": (s.value, tr_end.value), "test": (tr_end.value, min(te_end, end).value),
                    "label": f"{tr_end:%Y-%m-%d}..{min(te_end, end):%Y-%m-%d}"})
        s = s + pd.DateOffset(months=test_months)
    return out


def _chain(results: list[dict], initial: float) -> dict:
    """Concatenate per-window results into one OOS record (each window re-starts at the same equity)."""
    trades = [t for r in results for t in r["trades"]]
    rej = defaultdict(int)
    for r in results:
        for k, v in r["rejections"].items():
            rej[k] += v
    growth = np.prod([r["final_equity"] / r["initial_equity"] for r in results]) if results else 1.0
    eq, adj = initial, []
    for t in sorted(trades, key=lambda x: x["closed_ns"]):
        eq += t["pnl"] * eq / initial
        adj.append({**t, "equity_after": eq})
    return {"trades": adj, "rejections": dict(rej), "final_equity": initial * growth, "initial_equity": initial,
            "candidates": sum(r["candidates"] for r in results)}


def _breakdowns(trades: list[dict], regime_of) -> dict:
    out = {}
    for key, fn in (("session", lambda t: t.get("session")), ("year", lambda t: str(pd.Timestamp(t["opened_ns"]).year)),
                    ("side", lambda t: "long" if t["side"] == 1 else "short"), ("regime", lambda t: regime_of(t["signal_ns"]))):
        groups = defaultdict(list)
        for t in trades:
            groups[fn(t)].append(t)
        out[key] = {k: {"trades": len(v), "mean_r": round(statistics.fmean(x["r"] for x in v), 4),
                        "net_pnl": round(sum(x["pnl"] for x in v), 2)} for k, v in sorted(groups.items())}
    return out


def _regime_fn(frames: dict):
    f = frames["1h"]
    h, low, c = (f.df[k].to_numpy() for k in ("high", "low", "close"))
    e200, a = ema(c, 200), atr(h, low, c, 14)
    vol = a / c
    med = pd.Series(vol).rolling(500, min_periods=100).median().to_numpy()
    close_ns = f.close_ns

    def regime(ns):
        i = int(np.searchsorted(close_ns, ns, side="right")) - 1
        if i < 0 or not np.isfinite(e200[i]) or not np.isfinite(med[i]):
            return "unknown"
        return ("up" if c[i] > e200[i] else "down") + ("-highvol" if vol[i] > med[i] else "-lowvol")
    return regime


def evaluate_strategy(pool, kind: str, grid: list, exec_tf: str, span: tuple, train_m: int, test_m: int,
                      min_trades: int, seeds: int, label: str) -> dict:
    wins = windows(pd.Timestamp(span[0]), pd.Timestamp(span[1]), train_m, test_m)
    sel = list(pool.map(_select_task, [{"kind": kind, "grid": grid, "exec_tf": exec_tf, "train": w["train"],
                                        "min_trades": min_trades, "window": k} for k, w in enumerate(wins)]))
    chosen = {s["window"]: s["selected"] for s in sel}
    tests = [{"id": f"{label}-oos-{k}", "kind": kind, "params": chosen[k] or grid[0], "exec_tf": exec_tf,
              "span": w["test"]} for k, w in enumerate(wins) if chosen[k] is not None]
    oos = list(pool.map(_run_task, tests))
    spans = [t["span"] for t in tests]
    oos_span = (min(s[0] for s in spans), max(s[1] for s in spans)) if spans else span
    most = statistics.mode([json.dumps(asdict(chosen[k]), sort_keys=True) for k in chosen if chosen[k]]) if tests else None
    ref_params = type(grid[0])(**json.loads(most)) if most else grid[0]
    rand = list(pool.map(_run_task, [{"id": f"{label}-rand-{s}", "kind": "random", "ref_kind": kind,
                                      "params": ref_params, "exec_tf": exec_tf, "seed": s, "span": oos_span,
                                      "spans": spans} for s in range(1, seeds + 1)]))
    sens = list(pool.map(_run_task, [{"id": f"{label}-grid-{k}", "kind": kind, "params": p, "exec_tf": exec_tf,
                                      "span": span} for k, p in enumerate(grid)]))
    scen = list(pool.map(_run_task, [{**t, "id": f"{t['id']}-{sc}", "scenario": sc} for sc in
                                     (("stress", "low_fee") if _S_market_is_btc(label) else ("stress",)) for t in tests]))
    return {"windows": wins, "chosen": chosen, "tests": tests, "oos": oos, "random": rand, "sensitivity": sens,
            "scenarios": scen, "ref_params": ref_params, "oos_span": oos_span}


def _S_market_is_btc(label: str) -> bool:
    return label.startswith("btc")


def run(market: str, workers: int) -> dict:
    _init(market)
    report: dict = {"market": market, "research_only": True, "data": {}, "strategies": {}}
    b5, b1 = _S["base5"].df, _S["base1"].df
    report["data"] = {"exec_5m": [str(b5.index[0]), str(b5.index[-1]), len(b5)],
                      "exec_1m": [str(b1.index[0]), str(b1.index[-1]), len(b1)]}
    grid_a = param_grid(SelectiveParams(), GRID_A)
    grid_b = param_grid(ScalpParams(), GRID_B)
    regime5, regime1 = _regime_fn(_S["frames5"]), _regime_fn(_S["frames1"])
    with ProcessPoolExecutor(workers, initializer=_init, initargs=(market,)) as pool:
        if market == "btcusdt":
            plan = {"A": ("A", grid_a, "5m", (b5.index[0], BTC_END), 6, 2, 10),
                    "A_1m": ("A", grid_a, "1m", (b1.index[0], BTC_END), 6, 2, 5),
                    "B": ("B", grid_b, "1m", (b1.index[0], BTC_END), 6, 2, 20)}
        else:
            dev = (b5.index[0], XAU_DEV_END)
            plan = {"A": ("A", grid_a, "5m", dev, 3, 1, 5),
                    "A_explore": ("A", grid_a, "5m", (XAU_DEV_END, b5.index[-1]), 3, 1, 5),
                    "B_explore": ("B", grid_b, "1m", (b1.index[0], b1.index[-1]), 1, 1, 10),
                    "A_1m_explore": ("A", grid_a, "1m", (b1.index[0], b1.index[-1]), 1, 1, 3)}
        results = {}
        for name, (kind, grid, tf, span, tr, te, mt) in plan.items():
            ev = evaluate_strategy(pool, kind, grid, tf, (span[0].value, span[1].value), tr, te, mt, 20,
                                   f"{market[:3]}-{name}")
            results[name] = ev
            oos = _chain(ev["oos"], 10_000.0)
            s = summarize(oos)
            rmeans = sorted(m for m in (summarize(r)["mean_r"] for r in ev["random"]) if m is not None)
            entry = {"execution_tf": tf, "span": [str(span[0]), str(span[1])], "windows": len(ev["windows"]),
                     "windows_tested": len(ev["tests"]), "oos": s,
                     "profitable_windows": sum(1 for r in ev["oos"] if r["final_equity"] > r["initial_equity"]),
                     "random_mean_r": {"median": statistics.median(rmeans) if rmeans else None,
                                       "min": rmeans[0] if rmeans else None, "max": rmeans[-1] if rmeans else None},
                     "beats_random": f"{sum(1 for m in rmeans if s['mean_r'] is not None and s['mean_r'] > m)}/{len(rmeans)}",
                     "grid_profitable": f"{sum(1 for r in ev['sensitivity'] if r['final_equity'] > r['initial_equity'])}/{len(ev['sensitivity'])}",
                     "grid": [{"params": {k: v for k, v in asdict(p).items() if k in (GRID_A if kind == 'A' else GRID_B)},
                               "net_return_pct": summarize(r, resamples=10)["net_return_pct"],
                               "trades": len(r["trades"])} for p, r in zip(grid, ev["sensitivity"])],
                     "most_selected": {k: v for k, v in asdict(ev["ref_params"]).items() if k in (GRID_A if kind == "A" else GRID_B)},
                     "scenarios": {}, "breakdowns": _breakdowns(oos["trades"], regime5 if tf == "5m" else regime1)}
            scen = defaultdict(list)
            for r in ev["scenarios"]:
                scen[r["id"].rsplit("-", 1)[1]].append(r)
            for sc, rs in scen.items():
                entry["scenarios"][sc] = summarize(_chain(rs, 10_000.0))
            # minimum-order eligibility: same OOS candidates at 3,000 account equity
            small = list(pool.map(_run_task, [{**t, "id": t["id"] + "-3k", "rules": AccountRules(initial_equity=3000.0)}
                                              for t in ev["tests"]]))
            sm = _chain(small, 3000.0)
            entry["eligibility_3000"] = {"trades": len(sm["trades"]), "min_size_rejections":
                                         sum(v for k, v in sm["rejections"].items() if k.startswith(("Minimum size", "Notional")))}
            # research halts (only add restrictions)
            halts = {}
            for hname, rules in (("weekly_3pct", AccountRules(weekly_loss_halt=0.03)),
                                 ("consecutive_3", AccountRules(consecutive_loss_halt=3)),
                                 ("session_cap_2", AccountRules(session_entry_cap=2))):
                hr = list(pool.map(_run_task, [{**t, "id": t["id"] + "-" + hname, "rules": rules} for t in ev["tests"]]))
                halts[hname] = summarize(_chain(hr, 10_000.0))
            entry["research_halts"] = halts
            report["strategies"][name] = entry
            print(name, json.dumps({k: entry["oos"][k] for k in ("trades", "net_return_pct", "mean_r", "mean_r_ci95",
                                                                   "max_drawdown_pct", "cost_r")}),
                  "beats random", entry["beats_random"], flush=True)
        if market == "xauusd":
            # Stricter check: development-selected A parameters, FROZEN, on the later period (no re-selection).
            # That period was examined in Phase 2A for other strategies, so it is exploratory, not fresh evidence.
            frozen = results["A"]["ref_params"]
            span = (XAU_DEV_END.value, pd.Timestamp(b5.index[-1]).value)
            base_r = _run_task({"id": "xau-A-frozen", "kind": "A", "params": frozen, "exec_tf": "5m", "span": span})
            stress_r = _run_task({"id": "xau-A-frozen-stress", "kind": "A", "params": frozen, "exec_tf": "5m",
                                  "span": span, "scenario": "stress"})
            rand = list(pool.map(_run_task, [{"id": f"xau-A-frozen-rand-{s}", "kind": "random", "ref_kind": "A",
                                              "params": frozen, "exec_tf": "5m", "seed": s, "span": span,
                                              "spans": [span]} for s in range(1, 21)]))
            fs = summarize(base_r)
            rm = sorted(m for m in (summarize(r)["mean_r"] for r in rand) if m is not None)
            report["xau_A_frozen_dev_params_on_2026"] = {
                "params": {k: v for k, v in asdict(frozen).items() if k in GRID_A}, "result": fs,
                "stress": summarize(stress_r), "random_mean_r_median": statistics.median(rm) if rm else None,
                "beats_random": f"{sum(1 for m in rm if fs['mean_r'] is not None and fs['mean_r'] > m)}/{len(rm)}",
                "breakdowns": _breakdowns(base_r["trades"], regime5),
                "note": "exploratory: 2025-12-30 onward was examined in Phase 2A (other strategies)"}
            print("A frozen on 2026:", json.dumps({k: fs[k] for k in ("trades", "net_return_pct", "mean_r", "mean_r_ci95")}),
                  "beats random", report["xau_A_frozen_dev_params_on_2026"]["beats_random"], flush=True)
        # trend baseline on the A execution span (5m) and B span (1m)
        trend = {}
        for name, tf, span in (("5m", "5m", report["strategies"]["A"]["span"]), ("1m", "1m",
                                                                                 [str(b1.index[0]), str(b1.index[-1])])):
            r = _run_task({"id": f"trend-{name}", "kind": "trend", "exec_tf": tf,
                           "span": (pd.Timestamp(span[0]).value, pd.Timestamp(span[1]).value)})
            trend[name] = summarize(r)
        report["trend_baseline"] = trend
        # Hybrid C on the 1m execution timeframe, same OOS windows as B; A and B params from their own selections
        key_b = "B" if market == "btcusdt" else "B_explore"
        key_a = "A_1m" if market == "btcusdt" else "A_1m_explore"
        evb, eva = results[key_b], results[key_a]
        hybrid = {}
        for variant, rules in (("hybrid_2pos", AccountRules(max_open_positions=2)),
                               ("hybrid_2pos_scalpcap40", AccountRules(max_open_positions=2, max_scalp_share=0.4)),
                               ("hybrid_1pos", AccountRules(max_open_positions=1))):
            tasks = []
            for k, w in enumerate(evb["windows"]):
                pa = eva["chosen"].get(k) if k < len(eva["windows"]) else None
                pb = evb["chosen"].get(k)
                if pa is None or pb is None:
                    continue
                tasks.append({"id": f"hyb-{variant}-{k}", "kind": "hybrid", "params_a": pa, "params_b": pb,
                              "exec_tf": "1m", "span": w["test"], "rules": rules})
            hr = list(pool.map(_run_task, tasks))
            ch = _chain(hr, 10_000.0)
            hybrid[variant] = {**summarize(ch), "by_strategy": {tag: summarize({**ch, "trades": [t for t in ch["trades"] if t["tag"] == tag]})
                                                                for tag in ("A", "B")}, "windows": len(tasks)}
        report["hybrid"] = hybrid
        report["comparison_1m_same_windows"] = {"A_only": report["strategies"][key_a]["oos"],
                                                "B_only": report["strategies"][key_b]["oos"],
                                                **{k: {kk: v[kk] for kk in ("trades", "net_return_pct", "mean_r", "max_drawdown_pct",
                                                                            "sharpe_daily_annualized")} for k, v in hybrid.items()}}
    return report


def to_markdown(report: dict) -> str:
    lines = [f"# Multi-timeframe research - {report['market'].upper()}", "", f"Data: {report['data']}", "",
             "| Run | Exec TF | OOS trades | Net % | Mean R (95% CI) | Gross R | Cost R | Max DD % | Beats random | Grid profitable | Stress net % |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, e in report["strategies"].items():
        o = e["oos"]
        stress = e["scenarios"].get("stress", {}).get("net_return_pct")
        lines.append(f"| {name} | {e['execution_tf']} | {o['trades']} | {o['net_return_pct']} | {o['mean_r']} {o['mean_r_ci95']} | "
                     f"{o['mean_r_gross']} | {o['cost_r']} | {o['max_drawdown_pct']} | {e['beats_random']} | {e['grid_profitable']} | {stress} |")
    lines += ["", f"Trend baseline: {report['trend_baseline']}", "", "## Hybrid (1m execution, same windows)", ""]
    for k, v in report["comparison_1m_same_windows"].items():
        lines.append(f"- {k}: trades {v['trades']}, net {v['net_return_pct']}%, mean R {v['mean_r']}, max DD {v['max_drawdown_pct']}%")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--market", choices=("btcusdt", "xauusd"), required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    report = run(args.market, args.workers)
    out = Path(f"research/output/mtf/{args.market}_mtf_research.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    out.with_suffix(".md").write_text(to_markdown(report), encoding="utf-8")
    print(to_markdown(report))


if __name__ == "__main__":
    main()
