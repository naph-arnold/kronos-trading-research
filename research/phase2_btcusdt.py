"""Phase 2B: BTCUSDT strategy research (Kronos vs non-AI baselines vs buy-and-hold).

Research only. Uses verified Binance public candles, a saved public exchangeInfo
snapshot, the deterministic risk engine and pessimistic replay. No orders.

    python -m research.phase2_btcusdt --config research/configs/phase2_btcusdt.json \
        --output research/output/phase2_btcusdt.json [--fetch-exchange-info] [--fetch-fomc]
"""
from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from research.datasets import load_verified
from research.market_data import DataValidationError
from trading.audit import to_jsonable
from trading.common import Market
from trading.config import load_demo_environment
from trading.connectors.binance import BinanceReadOnlyClient, parse_symbol
from trading.evaluation import (TradingWindow, by_regime, chronological_split, classify, config_hash, metrics,
                                param_grid, regime_labels, walk_forward, walk_forward_splits)
from trading.marketcal.blackout import EventBlackout
from trading.marketcal.events import EventStore
from trading.marketcal.fomc import fetch_fomc_calendar
from trading.marketdata import bar_from_research_row
from trading.replay import run_replay
from trading.risk import RiskLimits
from trading.strategies.crypto import (Breakout, BreakoutParams, KronosFilteredTrend, KronosFilteredTrendParams,
                                       KronosForecastStrategy, KronosParams, TrendFollowing, TrendParams,
                                       buy_and_hold, forecasts_by_target)

MARKET = Market.BTCUSDT_BINANCE_SPOT


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _strip(result: dict) -> dict:
    """Drop bulky per-bar data from stored results (summary + trades stay)."""
    return {k: v for k, v in result.items() if k not in ("equity_curve", "decisions")}


def run(config: dict, *, fetch_exchange_info: bool, fetch_fomc: bool) -> dict:
    ds = config["dataset"]
    csv_path = Path(ds["csv"])
    manifest = json.loads(csv_path.with_suffix(csv_path.suffix + ".json").read_text(encoding="utf-8"))
    if manifest["sha256"] != ds["expected_sha256"]:
        raise DataValidationError("Dataset SHA-256 differs from the research configuration")
    rows = load_verified(csv_path, symbol=ds["symbol"], interval=ds["interval"], start_ms=manifest["start_ms"],
                         source=ds["source"])
    bars = [bar_from_research_row(r, symbol="BTCUSDT", interval=ds["interval"]) for r in rows]

    # Exchange specification (public filters; fee assumed unless verified on testnet).
    snapshot = Path(config["exchange_info_snapshot"])
    if fetch_exchange_info or not snapshot.exists():
        info = BinanceReadOnlyClient.public().exchange_info("BTCUSDT")
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(json.dumps({"retrieved_at": datetime.now(timezone.utc).isoformat(), "symbol": info},
                                       indent=2), encoding="utf-8")
    snap = json.loads(snapshot.read_text(encoding="utf-8"))
    costs = config["costs"]
    discovery = parse_symbol(snap["symbol"], source="binance_public_market_data",
                             taker_fee=Decimal(costs["taker_fee_rate"]), fee_source="assumed")
    discovery.fee_source = "assumed"
    instrument = discovery.to_instrument_spec(allow_assumed_fee=True)

    risk = config["risk"]
    limits = RiskLimits(market=MARKET, max_spread=Decimal(risk["max_spread_usdt"]),
                        slippage_allowance=Decimal(costs["slippage_allowance_usdt"]),
                        risk_fraction=Decimal(risk["risk_fraction"]),
                        max_entry_deviation_fraction=Decimal(risk["max_entry_deviation_fraction"]))
    environment = load_demo_environment({"EXECUTION_MODE": "demo"})
    spread_bps = Decimal(costs["spread_bps"]) / Decimal(10_000)

    def spread(bar):
        return (bar.open * spread_bps).quantize(instrument.tick_size)

    # Economic-event blackout (official FOMC calendar).
    store = EventStore(":memory:")
    guard = None
    fomc_count = 0
    if config["event_blackout"]["enabled"]:
        cache = Path("data/processed/fomc_events.json")
        if fetch_fomc or not cache.exists():
            events = fetch_fomc_calendar()
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps([e.to_json() for e in events], indent=2), encoding="utf-8")
        from trading.marketcal.events import EconomicEvent
        events = [EconomicEvent.from_json(e) for e in json.loads(cache.read_text(encoding="utf-8"))]
        store.upsert(events)
        fomc_count = len(events)
        guard = EventBlackout(store=store, market=MARKET, max_calendar_age=None)

    initial = Decimal(config["initial_equity"])

    def replay(segment, strategy, **kw):
        return run_replay(segment, strategy, instrument=instrument, limits=limits, environment=environment,
                          initial_equity=initial, spread=spread, event_guard=guard, close_at_end=True, **kw)

    research_end, n = chronological_split(len(bars), holdout_fraction=config["holdout_fraction"])
    wf_cfg = config["walk_forward"]
    splits = walk_forward_splits(research_end, train=wf_cfg["train_bars"], test=wf_cfg["test_bars"],
                                 anchored=wf_cfg["anchored"])
    warmup = config["warmup_bars"]
    holdout_start, holdout_end = bars[research_end].open_time, bars[-1].close_time + timedelta(hours=1)
    holdout_bars = bars[max(0, research_end - warmup):]
    labels = regime_labels(bars)
    report: dict = {"research_only": True, "config_sha256": config_hash(config), "git_commit": _git_commit(),
                    "generated_at": datetime.now(timezone.utc).isoformat(), "dataset_sha256": manifest["sha256"],
                    "bars": len(bars), "research_bars": research_end, "holdout_bars": n - research_end,
                    "holdout_period": [holdout_start.isoformat(), bars[-1].close_time.isoformat()],
                    "instrument": discovery.to_record(), "fomc_events_loaded": fomc_count, "strategies": {}}

    def evaluate_tuned(name, cls, base_params, grid):
        candidates = param_grid(base_params, grid)
        make = lambda p: cls(p, tick_size=instrument.tick_size, dataset_id=manifest["sha256"][:16])  # noqa: E731
        wf = walk_forward(bars[:research_end], make_strategy=make, candidates=candidates,
                          run=lambda seg, strat: replay(seg, strat), interval="1h", splits=splits,
                          warmup_bars=warmup, min_trades=wf_cfg["min_train_trades"])
        # Final selection on all research data, evaluated once on the untouched holdout.
        best, best_score = None, float("-inf")
        for params in candidates:
            m = metrics(replay(bars[:research_end], make(params)), interval="1h")
            score = m["net_return_pct"] if m["trades"] >= wf_cfg["min_train_trades"] else float("-inf")
            if score > best_score:
                best, best_score = params, score
        holdout = replay(holdout_bars, TradingWindow(make(best), holdout_start, holdout_end))
        hm = metrics(holdout, interval="1h")
        for w in wf["windows"]:
            w.pop("test_result", None)
        report["strategies"][name] = {
            "selected_params": asdict(best), "configurations": len(candidates),
            "walk_forward": to_jsonable(wf), "holdout": hm, "holdout_by_regime": by_regime(holdout["trade_ledger"],
                                                                                          labels),
            "holdout_rejections": holdout["rejection_reasons"],
            "classification": classify(hm, wf, broker_spec_verified=discovery.verified),
            "holdout_trades": to_jsonable(holdout["trade_ledger"]),
        }
        return best

    best_trend = evaluate_tuned("trend_following", TrendFollowing, TrendParams(),
                                config["strategies"]["trend"]["grid"])
    evaluate_tuned("breakout", Breakout, BreakoutParams(), config["strategies"]["breakout"]["grid"])

    # Kronos: pre-registered parameters, holdout only (forecasts exist only there).
    kronos_report = json.loads(Path(config["kronos_forecasts"]).read_text(encoding="utf-8"))
    if kronos_report.get("dataset_sha256") != manifest["sha256"]:
        raise DataValidationError("Kronos forecasts belong to a different dataset")
    forecasts = forecasts_by_target(kronos_report)
    first_target = min(forecasts)
    if first_target < holdout_start:
        raise DataValidationError("Kronos forecasts overlap the research period")
    kcfg = config["strategies"]["kronos"]
    kparams = KronosParams(**kcfg["fixed_params"])
    kstrat = KronosForecastStrategy(kparams, forecasts=forecasts, tick_size=instrument.tick_size)
    kres = replay(holdout_bars, TradingWindow(kstrat, holdout_start, holdout_end),
                  max_holding_bars=kcfg["max_holding_bars"])
    km = metrics(kres, interval="1h")
    report["strategies"]["kronos_forecast"] = {
        "params": asdict(kparams), "holdout": km, "holdout_by_regime": by_regime(kres["trade_ledger"], labels),
        "holdout_rejections": kres["rejection_reasons"],
        "forecast_quality": {k: kronos_report[k] for k in ("model", "evaluations", "lookback", "model_mae",
                                                           "baseline_mae", "model_better_than_baseline")},
        "classification": "experimental (holdout-only evaluation; no walk-forward forecasts)",
        "holdout_trades": to_jsonable(kres["trade_ledger"]),
    }
    fparams = KronosFilteredTrendParams(trend=best_trend,
                                        min_edge_bps=config["strategies"]["kronos_filtered_trend"]["min_edge_bps"])
    fstrat = KronosFilteredTrend(fparams, forecasts=forecasts, tick_size=instrument.tick_size)
    fres = replay(holdout_bars, TradingWindow(fstrat, holdout_start, holdout_end))
    report["strategies"]["trend_with_kronos_filter"] = {
        "params": to_jsonable(asdict(fparams)), "holdout": metrics(fres, interval="1h"),
        "filtered_out_signals": fstrat.filtered_out, "holdout_rejections": fres["rejection_reasons"],
        "classification": "experimental (holdout-only evaluation)",
    }

    bh = buy_and_hold(bars[research_end:], initial_equity=initial, fee_rate=instrument.taker_fee_rate,
                      half_spread=spread(bars[research_end]) / 2, slippage=limits.slippage_allowance)
    bh_metrics = metrics({**bh, "trade_ledger": []}, interval="1h")
    bh_metrics["gross_return_pct"] = round(float(bh["gross_return_pct"]), 4)
    report["strategies"]["buy_and_hold"] = {"holdout": bh_metrics, "note": bh["note"]}
    return report


def summary_table(report: dict) -> str:
    lines = ["| Strategy | Net % | Gross % | Max DD % | Trades | Win % | PF | Sharpe | Exposure % | Costs | Class |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, item in report["strategies"].items():
        h = item["holdout"]
        lines.append(f"| {name} | {h['net_return_pct']} | {h.get('gross_return_pct')} | {h['max_drawdown_pct']} | "
                     f"{h['trades']} | {h.get('win_rate_pct')} | {h.get('profit_factor')} | "
                     f"{h.get('sharpe_annualized')} | {h.get('exposure_pct')} | {h.get('total_costs')} | "
                     f"{item.get('classification', 'benchmark')} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 2B BTCUSDT research (no orders)")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fetch-exchange-info", action="store_true")
    parser.add_argument("--fetch-fomc", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    report = run(config, fetch_exchange_info=args.fetch_exchange_info, fetch_fomc=args.fetch_fomc)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(to_jsonable(report), indent=2) + "\n", encoding="utf-8")
    print(summary_table(report))
    for name, item in report["strategies"].items():
        wf = item.get("walk_forward")
        if wf:
            print(f"{name}: walk-forward OOS {wf['oos_compounded_return_pct']}% over {wf['oos_windows']} windows "
                  f"({wf['oos_windows_profitable']} profitable, {wf['oos_trades']} trades); "
                  f"train-test gap {wf['avg_train_minus_test_return_pct']}%")


if __name__ == "__main__":
    main()
