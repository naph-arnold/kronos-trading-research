"""Phase 2C — XAUUSD strategy validation and robustness (research only, no orders).

Subcommands:
  ticks    Read-only MT5 tick sample -> verified spread distribution and the ratio between real tick
           spreads and the MT5 M15 bar `spread` field (a bar minimum). Writes the tick_spreads JSON.
  run      Training-period analyses in parallel: diagnostics (3,000 and 25,000 USD), training-only
           parameter sensitivity, non-AI baselines, cost stress, minimum-lot eligibility.
           Writes research/output/phase2c_xauusd.json and a Markdown summary.

Data discipline: everything in `run` uses ONLY the training partition (first 80% of bars). The Phase 2A
holdout was seen once and is not used for any new choice. Strategy rules and the 0.5% risk fraction are
unchanged; strategies use the parameters already selected in Phase 2A.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from research.phase2_xauusd import MARKET, load_mt5_csv, server_tz_from_args
from trading.audit import to_jsonable
from trading.clock import NEW_YORK
from trading.config import load_demo_environment
from trading.connectors.mt5 import SpecDiscovery, spread_model
from trading.evaluation import chronological_split, metrics, param_grid, regime_labels
from trading.indicators import ATR
from trading.marketcal.blackout import EventBlackout
from trading.marketcal.events import EconomicEvent, EventStore
from trading.marketcal.holidays import build_xauusd_calendar
from trading.replay import run_replay
from trading.risk import RiskLimits
from trading.sessions import XAUUSD_NY_MARKET_HOURS
from trading.strategies.baselines import (H1TrendBaseline, H1TrendBaselineParams, RandomEntryBaseline,
                                          RandomEntryParams)
from trading.strategies.xauusd import XAU_STRATEGIES

STRATEGY_KEYS = {"goldpilot_v1": "goldpilot", "order_block_retest": "orderblock", "liquidity_sweep_mss": "sweep",
                 "orb_london": "orb", "orb_newyork": "orb"}


# --------------------------------------------------------------------------- ticks (read-only MT5)

def cmd_ticks(args) -> dict:
    from trading.connectors.mt5 import Mt5ReadOnlyConnector, read_ticks
    tz = server_tz_from_args(args.server_tz)
    conn = Mt5ReadOnlyConnector(server_tz=tz)
    conn.connect()
    try:
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        end -= timedelta(minutes=end.minute % 15)
        start = end - timedelta(days=args.days)
        ticks = read_ticks(conn, "XAUUSD", start, end)
        bars = conn.history("XAUUSD", "15m", start, end)
    finally:
        conn.shutdown()
    spreads = sorted(a - b for _, b, a in ticks)
    q = lambda xs, p: xs[min(len(xs) - 1, int(p * (len(xs) - 1)))]  # noqa: E731
    by_hour = defaultdict(list)
    per_bar = defaultdict(list)
    for t, b, a in ticks:
        by_hour[NEW_YORK.to_server(t).hour].append(a - b)
        per_bar[t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)].append(a - b)
    ratios, rows = [], []
    for bar in bars:
        sample = per_bar.get(bar.bar.open_time)
        if sample and bar.spread_points > 0:
            mean_tick = statistics.fmean(sample)
            reported = bar.spread_points * 0.01
            ratios.append(mean_tick / reported)
            rows.append((bar.bar.open_time.isoformat(), reported, round(mean_tick, 4), round(max(sample), 2)))
    ratios.sort()
    result = {
        "source": "MT5 copy_ticks_range (read-only), MetaQuotes-Demo XAUUSD", "retrieved_at":
            datetime.now(timezone.utc).isoformat(), "window_utc": [start.isoformat(), end.isoformat()],
        "ticks": len(ticks), "bars_compared": len(rows),
        "tick_spread_usd": {"p10": round(q(spreads, .1), 3), "p50": round(q(spreads, .5), 3),
                            "p90": round(q(spreads, .9), 3), "p99": round(q(spreads, .99), 3),
                            "max": round(spreads[-1], 3), "mean": round(statistics.fmean(spreads), 4)},
        "by_new_york_hour_p50_p90": {h: [round(q(sorted(v), .5), 3), round(q(sorted(v), .9), 3)]
                                     for h, v in sorted(by_hour.items())},
        "mean_tick_spread_over_bar_field": {"p25": round(q(ratios, .25), 3), "p50": round(q(ratios, .5), 3),
                                            "p75": round(q(ratios, .75), 3)} if ratios else None,
        "note": "MT5 bar `spread` is the bar's minimum spread; mean tick spread within the bar is higher.",
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


# --------------------------------------------------------------------------- worker state

_S: dict = {}


def _init(config_path: str) -> None:
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    mt5_bars, manifest = load_mt5_csv(Path(config["dataset"]["csv"]))
    bars = [b.bar for b in mt5_bars]
    research_end, _ = chronological_split(len(bars), holdout_fraction=config["holdout_fraction"])
    train_mt5, train = mt5_bars[:research_end], bars[:research_end]
    rec = json.loads(Path(config["specification"]).read_text(encoding="utf-8"))["specification"]
    discovery = SpecDiscovery(rec["symbol"], rec["retrieved_at"], rec["fields"], rec["missing"], rec["problems"])
    server_tz = server_tz_from_args(config["server_tz"])
    calendar, _ = build_xauusd_calendar(XAUUSD_NY_MARKET_HOURS, train[0].open_time.date(),
                                        train[-1].close_time.date() + timedelta(1))
    store = EventStore()
    store.upsert([EconomicEvent.from_json(e) for e in json.loads(Path(config["events_file"]).read_text())])
    ticks = json.loads(Path(config["tick_spreads"]).read_text(encoding="utf-8"))
    atr, atr_pct = ATR(14), {}
    for b in train:
        value = atr.update(float(b.high), float(b.low), float(b.close))
        if value is not None:
            atr_pct[b.close_time] = value / float(b.close)
    cuts = sorted(atr_pct.values())
    _S.update(config=config, server_tz=server_tz, mt5_bars=train_mt5, bars=train, discovery=discovery, calendar=calendar,
              guard=EventBlackout(store=store, market=MARKET, max_calendar_age=None), manifest=manifest,
              tick_ratio=Decimal(str(ticks["mean_tick_spread_over_bar_field"]["p50"])),
              tick_floor=Decimal(str(ticks["tick_spread_usd"]["p50"])),
              tick_mean=Decimal(str(round(ticks["tick_spread_usd"]["mean"], 2))),
              index={b.open_time: i for i, b in enumerate(train)}, labels=regime_labels(train),
              atr_pct=atr_pct, atr_terciles=(cuts[len(cuts) // 3], cuts[2 * len(cuts) // 3]),
              environment=load_demo_environment({"EXECUTION_MODE": "demo"}))


def _spread(kind: str):
    instrument_point = Decimal("0.01")
    if kind == "bar_floor_0.15":
        return spread_model(_S["mt5_bars"], instrument_point, floor=Decimal("0.15"))
    base = spread_model(_S["mt5_bars"], instrument_point)
    ratio = _S["tick_ratio"]
    floor = _S["tick_mean"] if kind == "tick_calibrated_mean" else _S["tick_floor"]
    return lambda bar: max((base(bar) * ratio).quantize(Decimal("0.01")), floor)


def _session(t: datetime) -> str:
    h = NEW_YORK.to_server(t).hour
    return "asia" if h >= 18 or h < 3 else "london" if h < 8 else "ny_am" if h < 12 else "ny_pm"


def swap_usd(trade: dict, swap: dict, server_tz) -> tuple[float, int]:
    """Post-hoc overnight financing: rollovers = server midnights crossed while open (Wednesday counts x3)."""
    opened, closed = server_tz.to_server(trade["opened_at"]), server_tz.to_server(trade["closed_at"])
    nights, day = 0, opened.date()
    while day < closed.date():
        day = day + timedelta(days=1)
        nights += 3 if (day - timedelta(days=1)).strftime("%A") == swap["triple_weekday_server"] else 1
    points = swap["long_points_per_lot_night"] if trade["side"] == "long" else swap["short_points_per_lot_night"]
    return float(trade["size"]) * points * swap["usd_per_point_per_lot"] * nights, nights


def _enrich(trade: dict) -> dict:
    bars, idx = _S["bars"], _S["index"]
    long = trade["side"] == "long"
    entry, stop = float(trade["entry"]), float(trade["stop"])
    risk_px = abs(entry - stop) or 1e-9
    i0 = idx.get(trade["opened_at"])
    i1 = idx.get(trade["closed_at"]) or idx.get(trade["closed_at"] - timedelta(minutes=15))
    mfe = mae = 0.0
    if i0 is not None and i1 is not None:
        span = bars[i0:i1 + 1]
        hi, lo = max(float(b.high) for b in span), min(float(b.low) for b in span)
        mfe, mae = ((hi - entry) if long else (entry - lo)), ((entry - lo) if long else (hi - entry))
    signal_close = trade["opened_at"]  # signal bar closes when the entry bar opens
    atr_pct = _S["atr_pct"].get(signal_close)
    lo_cut, hi_cut = _S["atr_terciles"]
    vol = None if atr_pct is None else "low" if atr_pct <= lo_cut else "mid" if atr_pct <= hi_cut else "high"
    swap, nights = swap_usd(trade, _S["config"]["swap"], _S["server_tz"]) if "swap" in _S["config"] else (0.0, 0)
    return {"opened_at": trade["opened_at"].isoformat(), "side": trade["side"], "exit_reason": trade["exit_reason"],
            "pnl": float(trade["pnl"]), "swap": round(swap, 4), "nights": nights, "gross": float(trade["gross_pnl"]), "costs": float(trade["costs"]),
            "r": float(trade["r_multiple"]) if trade["r_multiple"] is not None else None,
            "stop_distance": round(risk_px, 2), "size": float(trade["size"]), "bars_held": trade["bars_held"],
            "mfe_r": round(mfe / risk_px, 3), "mae_r": round(mae / risk_px, 3),
            "regime": _S["labels"].get(signal_close, "unknown"), "volatility": vol,
            "session": _session(trade["opened_at"]), "year": trade["opened_at"].year,
            "weekday": trade["opened_at"].strftime("%a"),
            "cost_r": round(float(trade["costs"]) / float(trade["planned_risk"]), 4) if trade["planned_risk"] else None}


def _strategy(task: dict):
    tick = Decimal("0.01")
    if task["kind"] == "random":
        return RandomEntryBaseline(RandomEntryParams(**task["params"]), tick_size=tick)
    if task["kind"] == "h1_trend":
        return H1TrendBaseline(H1TrendBaselineParams(**task["params"]), tick_size=tick)
    cls, params_cls = XAU_STRATEGIES[STRATEGY_KEYS[task["strategy"]]]
    return cls(params_cls(**task["params"]), tick_size=tick)


def _run(task: dict) -> dict:
    cfg = _S["config"]
    cost = cfg["costs"][task["cost"]]
    instrument = _S["discovery"].to_instrument_spec(commission_assumption=Decimal(cost["commission"]))
    limits = RiskLimits(market=MARKET, max_spread=Decimal(cfg["risk"]["max_spread"]),
                        slippage_allowance=Decimal(cost["slippage"]), risk_fraction=Decimal(cfg["risk"]["risk_fraction"]))
    result = run_replay(_S["bars"], _strategy(task), instrument=instrument, limits=limits,
                        environment=_S["environment"], initial_equity=Decimal(task["equity"]),
                        spread=_spread(cost["spread"]), session_calendar=_S["calendar"], allow_gaps=True,
                        event_guard=_S["guard"], close_at_end=True, max_holding_bars=task.get("max_holding_bars"))
    rejections = Counter()
    for reason, n in result["rejection_reasons"].items():
        rejections["minimum_lot" if reason.startswith("Minimum size") else reason.split(" (")[0][:48]] += n
    out = {**{k: v for k, v in task.items() if k != "params"}, "params": task["params"],
           "metrics": metrics(result, interval="15m"), "rejections": dict(rejections)}
    if task.get("keep_trades"):
        out["trades"] = [_enrich(t) for t in result["trade_ledger"]]
    else:
        by_year = defaultdict(float)
        for t in result["trade_ledger"]:
            by_year[t["opened_at"].year] += float(t["pnl"])
        out["pnl_by_year"] = {str(y): round(v, 2) for y, v in sorted(by_year.items())}
    return out


# --------------------------------------------------------------------------- statistics

def _bootstrap_mean(values: list[float], resamples: int, seed: int) -> tuple[float, float]:
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(resamples))
    return round(means[int(0.025 * resamples)], 4), round(means[int(0.975 * resamples)], 4)


def trade_stats(trades: list[dict], *, resamples: int, seed: int, equity: float) -> dict:
    rs = [t["r"] for t in trades if t["r"] is not None]
    if not rs:
        return {"trades": 0}
    pnl = sorted((t["pnl"] for t in trades), reverse=True)
    streak = worst = 0
    for t in trades:
        streak = streak + 1 if t["pnl"] <= 0 else 0
        worst = max(worst, streak)
    sd = statistics.stdev(rs) if len(rs) > 1 else 0.0
    months = max(1, len({t["opened_at"][:7] for t in trades}))
    return {"trades": len(rs), "trades_per_month": round(len(rs) / months, 2), "mean_r": round(statistics.fmean(rs), 4),
            "mean_r_ci95": _bootstrap_mean(rs, resamples, seed), "t_stat": round(statistics.fmean(rs) / (sd / math.sqrt(len(rs))), 2) if sd else None,
            "win_rate": round(sum(r > 0 for r in rs) / len(rs), 4),
            "net_pnl": round(sum(t["pnl"] for t in trades), 2), "gross_pnl": round(sum(t["gross"] for t in trades), 2),
            "costs": round(sum(t["costs"] for t in trades), 2),
            "mean_cost_r": round(statistics.fmean(t["cost_r"] for t in trades if t["cost_r"] is not None), 4),
            "swap_usd_estimated": round(sum(t.get("swap", 0.0) for t in trades), 2),
            "net_pnl_after_swap": round(sum(t["pnl"] + t.get("swap", 0.0) for t in trades), 2),
            "share_held_overnight": round(sum(t.get("nights", 0) > 0 for t in trades) / len(trades), 3),
            "net_pnl_ex_top5": round(sum(pnl[5:]), 2), "top5_share_of_gross_wins": round(sum(pnl[:5]) / sum(p for p in pnl if p > 0), 3) if any(p > 0 for p in pnl) else None,
            "longest_losing_streak": worst,
            "exit_reasons": dict(Counter(t["exit_reason"] for t in trades)),
            "median_mfe_r": round(statistics.median(t["mfe_r"] for t in trades), 3),
            "median_mae_r": round(statistics.median(t["mae_r"] for t in trades), 3),
            "share_reaching_1r_mfe": round(sum(t["mfe_r"] >= 1 for t in trades) / len(trades), 3),
            "median_stop_distance_usd": round(statistics.median(t["stop_distance"] for t in trades), 2),
            "net_return_pct_of_equity": round(100 * sum(t["pnl"] for t in trades) / equity, 3)}


def breakdown(trades: list[dict], key: str) -> dict:
    groups = defaultdict(list)
    for t in trades:
        groups[str(t[key])].append(t)
    return {k: {"trades": len(v), "net_pnl": round(sum(t["pnl"] for t in v), 2),
                "mean_r": round(statistics.fmean(t["r"] for t in v if t["r"] is not None), 3),
                "win_rate": round(sum(t["pnl"] > 0 for t in v) / len(v), 3)} for k, v in sorted(groups.items())}


def stop_buckets(trades: list[dict]) -> dict:
    edges = [0, 5, 10, 15, 20, 30, 50, 1e9]
    out = {}
    for lo, hi in zip(edges, edges[1:]):
        sel = [t for t in trades if lo <= t["stop_distance"] < hi]
        if sel:
            out[f"{lo}-{hi if hi < 1e9 else 'inf'}"] = {
                "trades": len(sel), "net_pnl": round(sum(t["pnl"] for t in sel), 2),
                "mean_r": round(statistics.fmean(t["r"] for t in sel if t["r"] is not None), 3)}
    return out


def eligibility(trades: list[dict], balances: list[str], *, slippage: float = 0.10, spread: float = 0.13,
                commission: float = 0.0, min_lot: float = 0.01, value_per_usd_per_lot: float = 100.0) -> dict:
    """Max tradable stop distance under 0.5% risk with a 0.01-lot minimum, and the share/quality of the
    unconstrained (25k) signals that would be eligible at each balance."""
    rows = {}
    for balance in balances:
        budget = float(balance) * 0.005
        loss_per_usd = min_lot * value_per_usd_per_lot  # USD lost per 1.00 USD of stop distance at min lot
        max_stop = (budget - min_lot * commission) / loss_per_usd - (2 * slippage + spread)
        sel = [t for t in trades if t["stop_distance"] <= max_stop]
        rows[balance] = {"risk_budget_usd": round(budget, 2), "max_stop_distance_usd": round(max(max_stop, 0), 2),
                         "eligible_share": round(len(sel) / len(trades), 3) if trades else None,
                         "eligible_mean_r": round(statistics.fmean(t["r"] for t in sel), 3) if sel else None,
                         "eligible_trades": len(sel)}
    return rows


# --------------------------------------------------------------------------- orchestration

def build_tasks(config: dict) -> list[dict]:
    phase2a = json.loads(Path(config["phase2a_report"]).read_text(encoding="utf-8"))
    selected = {k: v["selected_params"] for k, v in phase2a["strategies"].items()}
    holding = config["max_holding_bars"]
    unconstrained = config["unconstrained_equity"]
    tasks = []
    for name, params in selected.items():
        for equity in config["equities"]:
            tasks.append({"group": "diagnostics", "strategy": name, "kind": "strategy", "params": params,
                          "equity": equity, "cost": "base", "keep_trades": True,
                          "max_holding_bars": holding.get(name)})
        for cost in ("phase2a", "stress"):
            tasks.append({"group": "costs", "strategy": name, "kind": "strategy", "params": params,
                          "equity": unconstrained, "cost": cost, "max_holding_bars": holding.get(name)})
        cls, params_cls = XAU_STRATEGIES[STRATEGY_KEYS[name]]
        base = params_cls(**params)
        for candidate in param_grid(base, config["sensitivity_grids"][name]):
            tasks.append({"group": "sensitivity", "strategy": name, "kind": "strategy", "params": asdict(candidate),
                          "equity": unconstrained, "cost": "base", "max_holding_bars": holding.get(name)})
    for family, spec in config["baselines"]["random_families"].items():
        params = {k: v for k, v in spec.items() if k != "max_holding_bars"}
        for seed in range(1, config["baselines"]["random_seeds"] + 1):
            tasks.append({"group": "baseline_random", "strategy": family, "kind": "random",
                          "params": {**params, "seed": seed}, "equity": unconstrained, "cost": "base",
                          "max_holding_bars": spec.get("max_holding_bars")})
    tasks.append({"group": "baseline_trend", "strategy": "h1_ema_cross", "kind": "h1_trend",
                  "params": config["baselines"]["h1_trend"], "equity": unconstrained, "cost": "base",
                  "keep_trades": True})
    return tasks


def summarize(config: dict, results: list[dict]) -> dict:
    boot = config["bootstrap"]
    out = {"research_only": True, "data_partition": config["data_partition"], "strategies": {}, "baselines": {},
           "sensitivity": {}, "costs": {}}
    by_group = defaultdict(list)
    for r in results:
        by_group[r["group"]].append(r)
    for r in by_group["diagnostics"]:
        equity = float(r["equity"])
        entry = out["strategies"].setdefault(r["strategy"], {"params": r["params"]})
        trades = r["trades"]
        entry[f"equity_{r['equity']}"] = {
            "metrics": r["metrics"], "rejections": r["rejections"],
            "trade_stats": trade_stats(trades, resamples=boot["resamples"], seed=boot["seed"], equity=equity),
            "by_regime": breakdown(trades, "regime"), "by_volatility": breakdown(trades, "volatility"),
            "by_session": breakdown(trades, "session"), "by_year": breakdown(trades, "year"),
            "by_weekday": breakdown(trades, "weekday"), "by_side": breakdown(trades, "side"),
            "by_stop_distance_usd": stop_buckets(trades)}
        if r["equity"] == config["unconstrained_equity"]:
            entry["min_lot_eligibility"] = eligibility(trades, config["eligibility_balances"])
    for r in by_group["costs"]:
        out["costs"].setdefault(r["strategy"], {})[r["cost"]] = r["metrics"]
    for name, rows in defaultdict(list, {k: [r for r in by_group["sensitivity"] if r["strategy"] == k]
                                         for k in {r["strategy"] for r in by_group["sensitivity"]}}).items():
        nets = [r["metrics"]["net_return_pct"] for r in rows]
        years = sorted({y for r in rows for y in r["pnl_by_year"]})
        out["sensitivity"][name] = {
            "configurations": len(rows), "profitable": sum(n > 0 for n in nets),
            "median_net_return_pct": round(statistics.median(nets), 3), "best": max(nets), "worst": min(nets),
            "years_profitable_share_median": round(statistics.median(
                [sum(r["pnl_by_year"].get(y, 0) > 0 for y in years) / len(years) for r in rows]), 3),
            "rows": [{"params": {k: r["params"][k] for k in config["sensitivity_grids"][name]},
                      "net_return_pct": r["metrics"]["net_return_pct"], "trades": r["metrics"]["trades"],
                      "max_dd_pct": r["metrics"]["max_drawdown_pct"], "pf": r["metrics"]["profit_factor"],
                      "pnl_by_year": r["pnl_by_year"]} for r in sorted(rows, key=lambda x: -x["metrics"]["net_return_pct"])]}
    for family in {r["strategy"] for r in by_group["baseline_random"]}:
        nets = sorted(r["metrics"]["net_return_pct"] for r in by_group["baseline_random"] if r["strategy"] == family)
        avg_r = sorted(r["metrics"]["avg_r_multiple"] for r in by_group["baseline_random"] if r["strategy"] == family)
        out["baselines"][family] = {"seeds": len(nets), "median_net_return_pct": round(statistics.median(nets), 3),
                                    "median_avg_r": round(statistics.median(avg_r), 4), "avg_r_min": avg_r[0],
                                    "avg_r_max": avg_r[-1], "avg_r_by_seed": avg_r, "net_by_seed": nets,
                                    "p05": nets[int(0.05 * (len(nets) - 1))], "p95": nets[int(0.95 * (len(nets) - 1))],
                                    "min": nets[0], "max": nets[-1],
                                    "median_trades": statistics.median(r["metrics"]["trades"] for r in by_group["baseline_random"] if r["strategy"] == family)}
    for r in by_group["baseline_trend"]:
        out["baselines"]["h1_ema_cross"] = {"metrics": r["metrics"], "trade_stats": trade_stats(
            r["trades"], resamples=boot["resamples"], seed=boot["seed"], equity=float(r["equity"]))}
    return out


def cmd_run(args) -> dict:
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    tasks = build_tasks(config)
    if args.groups:
        tasks = [t for t in tasks if t["group"] in args.groups]
    print(f"{len(tasks)} replay tasks on the training partition; {config['workers']} workers")
    with ProcessPoolExecutor(max_workers=config["workers"], initializer=_init, initargs=(args.config,)) as pool:
        results = list(pool.map(_run, tasks, chunksize=1))
    _init(args.config)  # for buy-and-hold over the same training bars
    bars = _S["bars"]
    summary = summarize(config, results)
    first, last = bars[0], bars[-1]
    summary["baselines"]["buy_and_hold_unsized"] = {
        "net_return_pct": round(100 * (float(last.close) - 0.13 - float(first.open) - 0.13) / float(first.open), 2),
        "note": "Fully invested 1x notional, not risk-sized; shows the 2022-2025 gold uptrend."}
    summary["training_period"] = [first.open_time.isoformat(), last.close_time.isoformat()]
    summary["training_bars"] = len(bars)
    summary["tick_calibration"] = {"ratio_mean_tick_to_bar_field": str(_S["tick_ratio"]),
                                   "base_floor_tick_p50": str(_S["tick_floor"]), "stress_floor_tick_mean": str(_S["tick_mean"])}
    summary["config_sha256"] = __import__("trading.evaluation", fromlist=["config_hash"]).config_hash(config)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(to_jsonable(summary), indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 2C XAUUSD robustness research (no orders)")
    sub = parser.add_subparsers(dest="command", required=True)
    t = sub.add_parser("ticks")
    t.add_argument("--server-tz", default="eet_eu")
    t.add_argument("--days", type=int, default=7)
    t.add_argument("--output", default="data/processed/mt5_xauusd_tick_spreads.json")
    r = sub.add_parser("run")
    r.add_argument("--config", default="research/configs/phase2c_xauusd.json")
    r.add_argument("--output", default="research/output/phase2c_xauusd.json")
    r.add_argument("--groups", nargs="*", default=None, help="subset of task groups (e.g. baseline_random)")
    args = parser.parse_args()
    load_demo_environment()
    result = cmd_ticks(args) if args.command == "ticks" else cmd_run(args)
    print(json.dumps(to_jsonable({k: v for k, v in result.items() if k in (
        "ticks", "bars_compared", "tick_spread_usd", "mean_tick_spread_over_bar_field", "training_period",
        "training_bars", "baselines", "sensitivity")} if args.command == "run" else result), indent=1)[:6000])


if __name__ == "__main__":
    main()
