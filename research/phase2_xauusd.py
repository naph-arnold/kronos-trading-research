"""Phase 2A: XAUUSD via a read-only MT5 *demo* terminal. Research only; no orders.

Subcommands (Windows with an MT5 demo terminal):
  discover  -> broker symbol, contract specification (verified/unverified), account summary,
               server-time verification evidence           (writes JSON)
  download  -> UTC-normalized M15 history with original server timestamps and spreads,
               session-structure check against the broker-aware calendar  (CSV + manifest)
  backtest  -> walk-forward + untouched holdout for the four XAUUSD strategies using the
               downloaded data, recorded spreads and the verified specification

Credentials come from MT5_DEMO_LOGIN / MT5_DEMO_PASSWORD / MT5_DEMO_SERVER (optional if the
terminal is already logged into a demo account) and are never written anywhere.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from trading.audit import to_jsonable
from trading.clock import NY_CLOSE, ServerTimeZone, fixed_offset
from trading.common import Market, ValidationError
from trading.config import load_demo_environment
from trading.connectors.mt5 import Mt5Bar, Mt5ReadOnlyConnector, SpecDiscovery, spread_model, spread_statistics
from trading.evaluation import (TradingWindow, by_regime, chronological_split, classify, config_hash, metrics,
                                param_grid, regime_labels, walk_forward, walk_forward_splits)
from trading.marketcal.blackout import EventBlackout
from trading.marketcal.events import EconomicEvent, EventStore
from trading.marketcal.holidays import build_xauusd_calendar, compare_with_bars, load_broker_closures
from trading.marketdata import Bar
from trading.replay import run_replay
from trading.risk import RiskLimits
from trading.sessions import XAUUSD_NY_CLOSE_TEMPLATE
from trading.strategies.xauusd import XAU_STRATEGIES

MARKET = Market.XAUUSD_MT5
FIELDS = ("open_time_utc", "server_time", "open", "high", "low", "close", "tick_volume", "spread_points")


def server_tz_from_args(name: str) -> ServerTimeZone:
    if name == "ny_close":
        return NY_CLOSE
    if name.startswith("fixed:"):
        return fixed_offset(float(name.split(":", 1)[1]))
    raise ValueError("--server-tz must be 'ny_close' or 'fixed:<hours>'")


def _connector(args) -> Mt5ReadOnlyConnector:
    commission = Decimal(args.commission_per_lot) if args.commission_per_lot is not None else None
    login = os.environ.get("MT5_DEMO_LOGIN")
    return Mt5ReadOnlyConnector(server_tz=server_tz_from_args(args.server_tz),
                                terminal_path=os.environ.get("MT5_TERMINAL_PATH"),
                                login=int(login) if login else None, password=os.environ.get("MT5_DEMO_PASSWORD"),
                                server=os.environ.get("MT5_DEMO_SERVER"), commission_per_lot_round_trip=commission)


def cmd_discover(args) -> dict:
    conn = _connector(args)
    summary = conn.connect()
    try:
        name = conn.discover_xauusd()
        discovery = conn.symbol_specification(name)
        try:
            clock = conn.verify_server_time(name)
        except (ValidationError, ValueError) as exc:
            clock = {"method": "fresh_tick", "verified": False, "error": str(exc),
                     "note": "Market closed or tick stale; verify later or rely on session-structure check"}
        return {"retrieved_at": datetime.now(timezone.utc).isoformat(), "account": summary,
                "specification": discovery.to_record(), "server_time": clock, "server_tz": conn.server_tz.name}
    finally:
        conn.shutdown()


def cmd_download(args) -> dict:
    conn = _connector(args)
    conn.connect()
    try:
        name = conn.discover_xauusd()
        start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
        end = datetime.fromisoformat(args.end.replace("Z", "+00:00"))
        bars = conn.history(name, args.interval, start, end)
    finally:
        conn.shutdown()
    if not bars:
        raise ValidationError("Broker returned no bars")
    calendar, provenance = build_xauusd_calendar(XAUUSD_NY_CLOSE_TEMPLATE, start.date(), end.date() + timedelta(1),
                                                 load_broker_closures(args.broker_closures)
                                                 if args.broker_closures else ())
    calendar = calendar.__class__(**{**calendar.__dict__, "server_tz": conn.server_tz})
    check = compare_with_bars([b.bar for b in bars], calendar)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(FIELDS)
        for b in bars:
            writer.writerow([b.bar.open_time.isoformat(), b.server_time, b.bar.open, b.bar.high, b.bar.low,
                             b.bar.close, b.tick_volume, b.spread_points])
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    manifest = {"schema_version": 1, "source": "mt5_demo", "broker_symbol": name, "interval": args.interval,
                "server_tz": conn.server_tz.name, "rows": len(bars), "sha256": digest,
                "start": bars[0].bar.open_time.isoformat(), "end": bars[-1].bar.close_time.isoformat(),
                "retrieved_at": datetime.now(timezone.utc).isoformat(), "calendar_check": {
                    "consistent": check["consistent"], "bars_in_closed_time": len(check["bars_in_closed_time"]),
                    "missing_in_open_time": len(check["missing_in_open_time"]),
                    "examples": check["bars_in_closed_time"][:10] + check["missing_in_open_time"][:10]},
                "calendar_provenance": provenance}
    out.with_suffix(out.suffix + ".json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def load_mt5_csv(path: Path) -> tuple[list[Mt5Bar], dict]:
    manifest = json.loads(path.with_suffix(path.suffix + ".json").read_text(encoding="utf-8"))
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["sha256"]:
        raise ValidationError("MT5 dataset SHA-256 mismatch")
    rows = []
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise ValidationError("Unexpected MT5 CSV schema")
        for r in reader:
            bar = Bar.create(symbol="XAUUSD", interval=manifest["interval"],
                             open_time=datetime.fromisoformat(r["open_time_utc"]), open=r["open"], high=r["high"],
                             low=r["low"], close=r["close"], volume=r["tick_volume"])
            rows.append(Mt5Bar(bar, int(r["server_time"]), int(r["spread_points"]), int(r["tick_volume"])))
    return rows, manifest


def cmd_backtest(args) -> dict:
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    mt5_bars, manifest = load_mt5_csv(Path(config["dataset"]["csv"]))
    bars = [b.bar for b in mt5_bars]
    discovery_record = json.loads(Path(config["specification"]).read_text(encoding="utf-8"))["specification"]
    discovery = SpecDiscovery(discovery_record["symbol"], discovery_record["retrieved_at"],
                              discovery_record["fields"], discovery_record["missing"], discovery_record["problems"])
    instrument = discovery.to_instrument_spec()  # refuses unverified broker data
    tz = server_tz_from_args(config["server_tz"])
    calendar, provenance = build_xauusd_calendar(
        XAUUSD_NY_CLOSE_TEMPLATE, bars[0].open_time.date(), bars[-1].close_time.date() + timedelta(1),
        load_broker_closures(Path(config["broker_closures"])) if config.get("broker_closures") else ())
    calendar = calendar.__class__(**{**calendar.__dict__, "server_tz": tz})
    check = compare_with_bars(bars, calendar)
    if not check["consistent"] and not config.get("accept_calendar_differences", False):
        raise ValidationError(f"Broker history disagrees with calendar: {len(check['bars_in_closed_time'])} bars "
                              f"in closed time, {len(check['missing_in_open_time'])} missing; review first")
    point = instrument.point
    spread = spread_model(mt5_bars, point, floor=Decimal(config["costs"]["min_spread"]))
    risk = config["risk"]
    limits = RiskLimits(market=MARKET, max_spread=Decimal(risk["max_spread"]),
                        slippage_allowance=Decimal(config["costs"]["slippage_allowance"]),
                        risk_fraction=Decimal(risk["risk_fraction"]))
    environment = load_demo_environment({"EXECUTION_MODE": "demo"})
    store = EventStore()
    if config.get("events_file"):
        store.upsert([EconomicEvent.from_json(e) for e in json.loads(Path(config["events_file"]).read_text())])
    guard = EventBlackout(store=store, market=MARKET, max_calendar_age=None)
    initial = Decimal(config["initial_equity"])

    def replay(segment, strategy, **kw):
        return run_replay(segment, strategy, instrument=instrument, limits=limits, environment=environment,
                          initial_equity=initial, spread=spread, session_calendar=calendar, allow_gaps=True,
                          event_guard=guard, close_at_end=True, **kw)

    research_end, n = chronological_split(len(bars), holdout_fraction=config["holdout_fraction"])
    wf_cfg = config["walk_forward"]
    splits = walk_forward_splits(research_end, train=wf_cfg["train_bars"], test=wf_cfg["test_bars"])
    warmup = config["warmup_bars"]
    holdout_start, holdout_end = bars[research_end].open_time, bars[-1].close_time + timedelta(minutes=1)
    labels = regime_labels(bars)
    report = {"research_only": True, "config_sha256": config_hash(config), "dataset": manifest,
              "spread_statistics": spread_statistics(mt5_bars, point), "calendar": provenance,
              "specification": discovery.to_record(), "strategies": {}}
    for key, settings in config["strategies"].items():
        cls, params_cls = XAU_STRATEGIES[settings["strategy"]]
        base = params_cls(**settings.get("fixed", {}))
        candidates = param_grid(base, settings.get("grid", {}))
        make = lambda p, c=cls: c(p, tick_size=instrument.tick_size, dataset_id=manifest["sha256"][:16])  # noqa
        wf = walk_forward(bars[:research_end], make_strategy=make, candidates=candidates, run=replay,
                          interval=manifest["interval"], splits=splits, warmup_bars=warmup,
                          min_trades=wf_cfg["min_train_trades"])
        best, best_score = None, float("-inf")
        for p in candidates:
            m = metrics(replay(bars[:research_end], make(p)), interval=manifest["interval"])
            score = m["net_return_pct"] if m["trades"] >= wf_cfg["min_train_trades"] else float("-inf")
            if score > best_score:
                best, best_score = p, score
        if best is None:
            report["strategies"][key] = {"classification": "experimental (no eligible configuration)"}
            continue
        holdout = replay(bars[max(0, research_end - warmup):], TradingWindow(make(best), holdout_start, holdout_end),
                         max_holding_bars=settings.get("max_holding_bars"))
        hm = metrics(holdout, interval=manifest["interval"])
        for w in wf["windows"]:
            w.pop("test_result", None)
        report["strategies"][key] = {
            "selected_params": asdict(best), "configurations": len(candidates), "walk_forward": to_jsonable(wf),
            "holdout": hm, "holdout_by_regime": by_regime(holdout["trade_ledger"], labels),
            "holdout_rejections": holdout["rejection_reasons"],
            "classification": classify(hm, wf, broker_spec_verified=discovery.verified),
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 2A XAUUSD research via read-only MT5 demo")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("discover", "download"):
        p = sub.add_parser(name)
        p.add_argument("--server-tz", default="ny_close", help="'ny_close' or 'fixed:<hours>' (verify!)")
        p.add_argument("--commission-per-lot", default=None, help="Round-trip commission per lot from broker docs")
        p.add_argument("--output", required=True)
        if name == "download":
            p.add_argument("--interval", default="15m")
            p.add_argument("--start", required=True)
            p.add_argument("--end", required=True)
            p.add_argument("--broker-closures", type=Path, default=None)
    b = sub.add_parser("backtest")
    b.add_argument("--config", required=True)
    b.add_argument("--output", required=True)
    args = parser.parse_args()
    load_demo_environment()  # refuse to run outside a demo-configured environment
    if args.command == "backtest":
        result = cmd_backtest(args)
    else:
        result = {"discover": cmd_discover, "download": cmd_download}[args.command](args)
    if args.command != "download":
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(to_jsonable(result), indent=2), encoding="utf-8")
    print(json.dumps(to_jsonable(result if args.command != "backtest" else
                                 {k: v.get("holdout") for k, v in result["strategies"].items()}), indent=2)[:4000])


if __name__ == "__main__":
    main()
