"""Supplementary analysis: does account size (minimum-lot rejections) drive the XAUUSD holdout results?

Re-runs the HOLDOUT ONLY with the parameters already selected in the main report (no re-optimisation)
at several account equities. The 0.5% risk fraction and every other risk rule are unchanged.

    python -m research.phase2_xauusd_equity_sensitivity --config research/configs/phase2_xauusd_metaquotes_demo.json \
        --report research/output/phase2_xauusd_metaquotes_demo.json --equities 3000 10000 25000 \
        --output research/output/phase2_xauusd_equity_sensitivity.json
"""
from __future__ import annotations

import argparse
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from research.phase2_xauusd import MARKET, load_mt5_csv, server_tz_from_args
from trading.audit import to_jsonable
from trading.config import load_demo_environment
from trading.connectors.mt5 import SpecDiscovery, spread_model
from trading.evaluation import TradingWindow, chronological_split, metrics
from trading.marketcal.blackout import EventBlackout
from trading.marketcal.events import EconomicEvent, EventStore
from trading.marketcal.holidays import build_xauusd_calendar
from trading.replay import run_replay
from trading.risk import RiskLimits
from trading.sessions import XAUUSD_NY_MARKET_HOURS
from trading.strategies.xauusd import XAU_STRATEGIES


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--equities", nargs="+", default=["3000", "10000", "25000"])
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    mt5_bars, manifest = load_mt5_csv(Path(config["dataset"]["csv"]))
    if manifest["sha256"] != report["dataset"]["sha256"]:
        raise SystemExit("Report and dataset differ")
    bars = [b.bar for b in mt5_bars]
    rec = json.loads(Path(config["specification"]).read_text(encoding="utf-8"))["specification"]
    discovery = SpecDiscovery(rec["symbol"], rec["retrieved_at"], rec["fields"], rec["missing"], rec["problems"])
    instrument = discovery.to_instrument_spec(commission_assumption=Decimal(config["commission_scenarios"][0]))
    server_tz_from_args(config["server_tz"])
    calendar, _ = build_xauusd_calendar(XAUUSD_NY_MARKET_HOURS, bars[0].open_time.date(),
                                        bars[-1].close_time.date() + timedelta(1))
    spread = spread_model(mt5_bars, instrument.point, floor=Decimal(config["costs"]["min_spread"]))
    limits = RiskLimits(market=MARKET, max_spread=Decimal(config["risk"]["max_spread"]),
                        slippage_allowance=Decimal(config["costs"]["slippage_allowance"]),
                        risk_fraction=Decimal(config["risk"]["risk_fraction"]))
    store = EventStore()
    store.upsert([EconomicEvent.from_json(e) for e in json.loads(Path(config["events_file"]).read_text())])
    guard = EventBlackout(store=store, market=MARKET, max_calendar_age=None)
    environment = load_demo_environment({"EXECUTION_MODE": "demo"})
    research_end, _ = chronological_split(len(bars), holdout_fraction=config["holdout_fraction"])
    start, end = bars[research_end].open_time, bars[-1].close_time + timedelta(minutes=1)
    segment = bars[max(0, research_end - config["warmup_bars"]):]
    out = {"research_only": True, "note": "Holdout only; parameters from the main report; risk_fraction unchanged.",
           "risk_fraction": str(limits.risk_fraction), "results": {}}
    for key, settings in config["strategies"].items():
        cls, params_cls = XAU_STRATEGIES[settings["strategy"]]
        params = params_cls(**report["strategies"][key]["selected_params"])
        rows = {}
        for equity in args.equities:
            res = run_replay(segment, TradingWindow(cls(params, tick_size=instrument.tick_size), start, end),
                             instrument=instrument, limits=limits, environment=environment,
                             initial_equity=Decimal(equity), spread=spread, session_calendar=calendar,
                             allow_gaps=True, event_guard=guard, close_at_end=True,
                             max_holding_bars=settings.get("max_holding_bars"))
            m = metrics(res, interval=manifest["interval"])
            min_lot = sum(n for r, n in res["rejection_reasons"].items() if r.startswith("Minimum size"))
            rows[equity] = {**{k: m[k] for k in ("net_return_pct", "gross_return_pct", "max_drawdown_pct", "trades",
                                                  "win_rate_pct", "profit_factor", "avg_r_multiple",
                                                  "sharpe_annualized")},
                            "min_lot_rejections": min_lot}
            print(key, equity, rows[equity])
        out["results"][key] = rows
    Path(args.output).write_text(json.dumps(to_jsonable(out), indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
