"""Phase 2E — BTC/USDT candidate research (read-only data; research only; no orders).

Candidates: Donchian breakout, EMA trend following (Phase 2B `TrendFollowing`), VWAP mean reversion, Kronos
forecast (KR-05 parameters, never tuned). Controls: long-only random entries with each candidate's exit
structure and trade frequency (20 seeds), plus buy-and-hold for context.

Data discipline: everything uses only 2019-01-01 -> 2026-05-24T20:00Z. 2019-01..2023-10 has never been examined
in this project; the Phase 2B holdout (2026-05-24 onward) is excluded entirely. Parameters are chosen per
rolling training window and evaluated on the following window (chronological out-of-sample); the frozen
forward-test parameters are selected on the full research span by the same rule. Fresh validation = forward
shadow data after pre-registration.

    python -m research.phase2e_btcusdt_research --config research/configs/phase2e_btcusdt_research.json
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from research.phase2e_btc_history import load_forecasts, load_rows
from trading.audit import to_jsonable
from trading.common import Market
from trading.config import load_demo_environment
from trading.connectors.binance import parse_symbol
from trading.evaluation import TradingWindow, metrics, objective, param_grid, walk_forward_splits
from trading.marketdata import bar_from_research_row
from trading.replay import run_replay
from trading.risk import RiskLimits
from trading.shadow.report import bootstrap_ci
from trading.strategies.baselines import RandomEntryParams
from trading.strategies.crypto import (BtcRandomEntry, DonchianBreakout, DonchianParams, KronosForecastStrategy,
                                       KronosParams, TrendFollowing, TrendParams, VwapMeanReversion,
                                       VwapReversionParams, buy_and_hold)

MARKET = Market.BTCUSDT_BINANCE_SPOT
CANDIDATES = {"donchian": (DonchianBreakout, DonchianParams), "ema_trend": (TrendFollowing, TrendParams),
              "vwap_reversion": (VwapMeanReversion, VwapReversionParams), "kronos": (KronosForecastStrategy, KronosParams)}
_S: dict = {}


def _init(config_path: str) -> None:
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    rows = []
    for path in config["datasets"]:
        rows += load_rows(Path(path))[0]
    end = datetime.fromisoformat(config["research_end"])
    bars = [b for b in (bar_from_research_row(r, symbol="BTCUSDT", interval="1h") for r in rows) if b.open_time < end]
    snap = json.loads(Path(config["exchange_info_snapshot"]).read_text(encoding="utf-8"))
    costs, risk = config["costs"], config["risk"]
    instrument = parse_symbol(snap["symbol"], source="binance_public_market_data",
                              taker_fee=Decimal(costs["taker_fee_rate"]), fee_source="assumed"
                              ).to_instrument_spec(allow_assumed_fee=True)
    limits = RiskLimits(market=MARKET, max_spread=Decimal(risk["max_spread_usdt"]),
                        slippage_allowance=Decimal(costs["slippage_allowance_usdt"]),
                        risk_fraction=Decimal(risk["risk_fraction"]),
                        max_entry_deviation_fraction=Decimal(risk["max_entry_deviation_fraction"]))
    bps = Decimal(costs["spread_bps"]) / Decimal(10_000)
    _S.update(config=config, bars=bars, instrument=instrument, limits=limits, bps=bps,
              environment=load_demo_environment({"EXECUTION_MODE": "demo"}),
              forecasts=load_forecasts(Path(config["kronos_forecasts"])))


def _spread(bar):
    return (bar.open * _S["bps"]).quantize(_S["instrument"].tick_size)


def _make(name: str, params):
    cls, _ = CANDIDATES.get(name, (None, None))
    tick = _S["instrument"].tick_size
    if name == "kronos":
        return KronosForecastStrategy(params, forecasts=_S["forecasts"], tick_size=tick)
    if name.startswith("random"):
        return BtcRandomEntry(params, tick_size=tick)
    return cls(params, tick_size=tick)


def _replay(bars, strategy, max_hold):
    return run_replay(bars, strategy, instrument=_S["instrument"], limits=_S["limits"],
                      environment=_S["environment"], initial_equity=Decimal(_S["config"]["initial_equity"]),
                      spread=_spread, allow_gaps=True, close_at_end=True, max_holding_bars=max_hold)


def _trades(result) -> list[dict]:
    return [{"opened_at": t["opened_at"].isoformat(), "r": float(t["r_multiple"]), "pnl": float(t["pnl"]),
             "gross": float(t["gross_pnl"]), "costs": float(t["costs"]), "exit": t["exit_reason"]}
            for t in result["trade_ledger"] if t["r_multiple"] is not None]


def _candidates(name: str) -> list:
    spec = _S["config"]["candidates"][name]
    _, params_cls = CANDIDATES[name]
    if "fixed" in spec:
        return [params_cls(**spec["fixed"])]
    return param_grid(params_cls(), spec["grid"])


def _window_task(task: dict) -> dict:
    """Select on the training window, evaluate once on the following test window."""
    bars, cfg = _S["bars"], _S["config"]["walk_forward"]
    name, (a, b), (c, d) = task["name"], task["train"], task["test"]
    hold = _S["config"]["candidates"][name]["max_holding_bars"]
    best, best_score = None, float("-inf")
    for params in _candidates(name):
        score = objective(metrics(_replay(bars[a:b], _make(name, params), hold), interval="1h"),
                          min_trades=cfg["min_train_trades"])
        if score > best_score:
            best, best_score = params, score
    out = {"name": name, "train": [bars[a].open_time.isoformat(), bars[b - 1].close_time.isoformat()],
           "test": [bars[c].open_time.isoformat(), bars[d - 1].close_time.isoformat()], "bars": d - c}
    if best is None:
        return {**out, "selected": None, "trades": [], "test_metrics": None}
    window = TradingWindow(_make(name, best), bars[c].open_time, bars[d - 1].close_time)
    result = _replay(bars[max(0, c - cfg["warmup_bars"]):d], window, hold)
    return {**out, "selected": asdict(best), "trades": _trades(result),
            "test_metrics": metrics(result, interval="1h")}


def _full_task(task: dict) -> dict:
    """Full research-span replay for one configuration (final selection, random controls, Kronos)."""
    bars = _S["bars"]
    name, params, hold = task["name"], task["params"], task["max_holding_bars"]
    strategy = _make(name, params)
    if task.get("window"):
        start, end = (datetime.fromisoformat(x) for x in task["window"])
        strategy = TradingWindow(strategy, start, end)
    result = _replay(bars, strategy, hold)
    return {**{k: v for k, v in task.items() if k != "params"}, "params": asdict(params),
            "metrics": metrics(result, interval="1h"), "trades": _trades(result)}


def _mean_r(trades):
    return statistics.fmean(t["r"] for t in trades) if trades else None


def _random_params(name: str, final: dict, rate: float, seed: int) -> tuple[RandomEntryParams, int | None]:
    hold = _S["config"]["candidates"][name]["max_holding_bars"]
    if name == "vwap_reversion":
        stop, rr = final["atr_stop_mult"], final["entry_atr"] / final["atr_stop_mult"]
    elif name == "kronos":
        stop, rr = final["atr_stop_mult"], None
    else:
        stop, rr = final["atr_stop_mult"], final["reward_to_risk"]
    return RandomEntryParams(seed=seed, entry_probability=round(rate, 5), atr_stop_mult=stop, reward_to_risk=rr,
                             long_probability=1.0), hold


def run(config_path: str) -> dict:
    _init(config_path)
    config, bars = _S["config"], _S["bars"]
    wf = config["walk_forward"]
    splits = walk_forward_splits(len(bars), train=wf["train_bars"], test=wf["test_bars"])
    oos_start, oos_end = bars[splits[0].test[0]].open_time, bars[splits[-1].test[1] - 1].close_time
    oos_bars = splits[-1].test[1] - splits[0].test[0]
    report = {"research_only": True, "generated_at": datetime.now().astimezone().isoformat(),
              "bars": len(bars), "span": [bars[0].open_time.isoformat(), bars[-1].close_time.isoformat()],
              "oos_span": [oos_start.isoformat(), oos_end.isoformat()], "walk_forward_windows": len(splits),
              "instrument": {"tick_size": str(_S["instrument"].tick_size), "step_size": str(_S["instrument"].step_size),
                             "min_notional": str(_S["instrument"].min_notional), "taker_fee": "0.001 (assumed)"},
              "candidates": {}}
    window_tasks = [{"name": n, "train": s.train, "test": s.test} for n in ("donchian", "ema_trend", "vwap_reversion")
                    for s in splits]
    final_tasks = [{"name": n, "params": p, "max_holding_bars": config["candidates"][n]["max_holding_bars"]}
                   for n in ("donchian", "ema_trend", "vwap_reversion") for p in _candidates(n)]
    kronos_params = _candidates("kronos")[0]
    with ProcessPoolExecutor(config["workers"], initializer=_init, initargs=(config_path,)) as pool:
        windows = list(pool.map(_window_task, window_tasks, chunksize=1))
        finals = list(pool.map(_full_task, final_tasks, chunksize=1))
        kronos_full = _full_task_in(pool, {"name": "kronos", "params": kronos_params, "max_holding_bars": 1})
        kronos_oos = _full_task_in(pool, {"name": "kronos", "params": kronos_params, "max_holding_bars": 1,
                                          "window": [oos_start.isoformat(), oos_end.isoformat()]})
        frozen = {}
        for name in ("donchian", "ema_trend", "vwap_reversion"):
            rows = [f for f in finals if f["name"] == name]
            best = max(rows, key=lambda f: objective(f["metrics"], min_trades=wf["min_train_trades"] * 8))
            frozen[name] = best
        frozen["kronos"] = kronos_full
        oos_trades = {n: [t for w in windows if w["name"] == n for t in w["trades"]]
                      for n in ("donchian", "ema_trend", "vwap_reversion")}
        oos_trades["kronos"] = kronos_oos["trades"]
        random_tasks = []
        for name, final in frozen.items():
            rate = max(len(oos_trades[name]), 1) / oos_bars
            for seed in range(1, config["random_seeds"] + 1):
                params, hold = _random_params(name, final["params"], rate, seed)
                random_tasks.append({"name": f"random_{name}", "control_for": name, "seed": seed, "params": params,
                                     "max_holding_bars": hold, "window": [oos_start.isoformat(), oos_end.isoformat()]})
        randoms = list(pool.map(_full_task, random_tasks, chunksize=1))
    boot = config["bootstrap"]
    for name in ("donchian", "ema_trend", "vwap_reversion", "kronos"):
        trades = oos_trades[name]
        r = [t["r"] for t in trades]
        rand = [x for x in randoms if x["control_for"] == name]
        rand_means = sorted(m for m in (_mean_r(x["trades"]) for x in rand) if m is not None)
        mean = _mean_r(trades)
        by_year = defaultdict(lambda: [0, 0.0])
        for t in trades:
            by_year[t["opened_at"][:4]][0] += 1
            by_year[t["opened_at"][:4]][1] += t["pnl"]
        win = [w for w in windows if w["name"] == name and w["selected"] is not None]
        compounded = 1.0
        for w in win:
            compounded *= 1 + w["test_metrics"]["net_return_pct"] / 100
        entry = {
            "oos_trades": len(trades), "oos_mean_r": None if mean is None else round(mean, 4),
            "oos_mean_r_ci95": bootstrap_ci(r, resamples=boot["resamples"], seed=boot["seed"]),
            "oos_mean_r_before_costs": round(statistics.fmean((t["gross"]) / (abs(t["pnl"] / t["r"]) or 1)
                                                              for t in trades if t["r"]), 4) if trades else None,
            "oos_net_pnl": round(sum(t["pnl"] for t in trades), 2),
            "oos_costs": round(sum(t["costs"] for t in trades), 2),
            "oos_by_year": {y: {"trades": v[0], "net_pnl": round(v[1], 2)} for y, v in sorted(by_year.items())},
            "random_control_mean_r": {"median": statistics.median(rand_means) if rand_means else None,
                                      "min": rand_means[0] if rand_means else None,
                                      "max": rand_means[-1] if rand_means else None},
            "beats_random_seeds": f"{sum(1 for m in rand_means if mean is not None and mean > m)}/{len(rand_means)}",
            "frozen_params": frozen[name]["params"], "frozen_full_span_metrics": frozen[name]["metrics"],
            "max_holding_bars": config["candidates"][name]["max_holding_bars"],
        }
        if name == "kronos":
            entry["walk_forward"] = "not applicable (fixed KR-05 parameters; every forecast is out-of-sample)"
            entry["full_span_metrics"] = kronos_full["metrics"]
        else:
            entry["walk_forward"] = {
                "windows": len(win), "profitable_windows": sum(1 for w in win if w["test_metrics"]["net_return_pct"] > 0),
                "oos_compounded_return_pct": round(100 * (compounded - 1), 3),
                "selected_params_frequency": _frequency([w["selected"] for w in win])}
            entry["configs_profitable_full_span"] = (
                f"{sum(1 for f in finals if f['name'] == name and f['metrics']['net_return_pct'] > 0)}/"
                f"{sum(1 for f in finals if f['name'] == name)}")
        rp = [x for x in randoms if x["control_for"] == name][0]["params"]
        entry["random_control_params"] = {k: v for k, v in rp.items() if k != "seed"}
        report["candidates"][name] = entry
    bh = buy_and_hold([b for b in bars if oos_start <= b.open_time < oos_end], initial_equity=Decimal("10000"),
                      fee_rate=_S["instrument"].taker_fee_rate, half_spread=Decimal("5"),
                      slippage=_S["limits"].slippage_allowance)
    report["buy_and_hold_oos_unsized"] = {"net_return_pct": float(bh["net_return_pct"]),
                                          "max_drawdown_pct": float(bh["max_drawdown_pct"])}
    return report


def _full_task_in(pool, task):
    return next(pool.map(_full_task, [task]))


def _frequency(selected: list[dict]) -> list:
    counts = defaultdict(int)
    for s in selected:
        counts[json.dumps(s, sort_keys=True)] += 1
    return sorted(([json.loads(k), v] for k, v in counts.items()), key=lambda x: -x[1])[:3]


def summary(report: dict) -> str:
    lines = [f"OOS span {report['oos_span'][0][:10]} -> {report['oos_span'][1][:10]} "
             f"({report['walk_forward_windows']} walk-forward windows)", "",
             "| Candidate | OOS trades | OOS mean R (95% CI) | OOS net PnL | WF compounded % | Profitable windows | "
             "Random median R | Beats random | Configs profitable |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, c in report["candidates"].items():
        wf = c["walk_forward"] if isinstance(c["walk_forward"], dict) else {}
        lines.append(f"| {name} | {c['oos_trades']} | {c['oos_mean_r']} {c['oos_mean_r_ci95']} | {c['oos_net_pnl']} | "
                     f"{wf.get('oos_compounded_return_pct', 'n/a')} | {wf.get('profitable_windows', 'n/a')}/"
                     f"{wf.get('windows', 'n/a')} | {c['random_control_mean_r']['median']} | {c['beats_random_seeds']} | "
                     f"{c.get('configs_profitable_full_span', 'fixed')} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="research/configs/phase2e_btcusdt_research.json")
    parser.add_argument("--output", type=Path, default=Path("research/output/phase2e_btcusdt_research.json"))
    args = parser.parse_args()
    report = run(args.config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(to_jsonable(report), indent=2), encoding="utf-8")
    text = summary(report)
    args.output.with_suffix(".md").write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
