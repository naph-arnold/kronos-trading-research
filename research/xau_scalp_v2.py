"""XAUUSD scalp v2 DEVELOPMENT evaluation, exactly as pre-registered (research/forward_test/
xau_scalp_v2_preregistration.json, commit c766013). Research only; no orders.

Development data is examined data: results here only (1) select one stop multiple per hypothesis by the pre-declared
rule and (2) decide whether a hypothesis may enter independent validation. They are not evidence of an edge.

    python -m research.xau_scalp_v2
"""
from __future__ import annotations

import gc
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from research import mtf_research as base
from trading.common import Market
from trading.mtf.features import atr
from trading.mtf.frames import Frame, align, from_csv, take, validate_base
from trading.mtf.scalp_v2 import HYPOTHESES
from trading.mtf.simulator import AccountRules, MarketSpec, simulate, summarize
from trading.mtf.strategies import random_candidates
from trading.risk import RiskLimits

ROOT = Path("C:/Users/Admin/Projects/kronos-mtf-research")
PREREG = Path("research/forward_test/xau_scalp_v2_preregistration.json")
OUT = Path("research/output/xau_scalp_v2")
DEV = {
    "dev_2025_2026": {"csv": ROOT / "data/raw/mt5_xauusd_1m_ticks_2025_2026.csv", "start": "2025-07-01", "end": "2026-11-07",
                      "events": [ROOT / "data/processed/fomc_events.json", ROOT / "data/processed/fomc_events_2025.json"],
                      "spread": "2026"},
    "dev_2019_2020": {"csv": ROOT / "data/raw/mt5_xauusd_1m_ticks_2019_2020.csv", "start": "2019-02-01", "end": "2021-01-01",
                      "events": [ROOT / "data/processed/fomc_events_2019_2020.json"], "spread": "hist"},
}
COSTS = {"base": ("0", "0.10", "p50"), "stress": ("7", "0.30", "p90")}
_S: dict = {}


def load(name: str) -> None:
    d = DEV[name]
    manifest = json.loads(d["csv"].with_suffix(".csv.json").read_text())
    if hashlib.sha256(d["csv"].read_bytes()).hexdigest() != manifest["sha256"]:
        raise ValueError(f"{name}: checksum mismatch")
    cols = {"open": "open", "high": "high", "low": "low", "close": "close", "volume": "tick_volume"}
    f1 = Frame("1m", from_csv(d["csv"], time_col="open_time_utc", columns=cols, spread_col="spread_points", spread_scale=0.01))
    validate_base(f1.df, "1m")
    from trading.marketcal.holidays import build_xauusd_calendar
    from trading.sessions import XAUUSD_NY_MARKET_HOURS
    cal, _ = build_xauusd_calendar(XAUUSD_NY_MARKET_HOURS, f1.df.index[0].date(), f1.df.index[-1].date() + pd.Timedelta(days=2))
    frames = base.frames_for(f1)
    a5 = atr(*(frames["5m"].df[k].to_numpy() for k in ("high", "low", "close")), 14)
    events = []
    for p in d["events"]:
        if p.exists():
            events += json.loads(p.read_text())
    hist = manifest.get("spread_by_new_york_hour_p50_p90")
    _S.clear()
    _S.update(name=name, base1=f1, frames=frames, calendar=cal, events=events, hist_spread=hist,
              atr5_on_1m=take(a5, align(f1, frames["5m"])), dataset_sha256=manifest["sha256"],
              start=pd.Timestamp(d["start"], tz="UTC").value, end=pd.Timestamp(d["end"], tz="UTC").value,
              spread_kind=d["spread"])


def spec(commission: str, slippage: str, q: str) -> MarketSpec:
    from trading.clock import EET_EU
    from trading.connectors.mt5 import SpecDiscovery
    from trading.marketcal.blackout import EventBlackout
    from trading.marketcal.events import EconomicEvent, EventStore
    from trading.shadow.costs import SwapSpec
    rec = json.loads((ROOT / "data/processed/mt5_xauusd_discovery.json").read_text())["specification"]
    disc = SpecDiscovery(rec["symbol"], rec["retrieved_at"], rec["fields"], rec["missing"], rec["problems"])
    inst = disc.to_instrument_spec(commission_assumption=Decimal(commission))
    limits = RiskLimits(market=Market.XAUUSD_MT5, max_spread=Decimal("1.00"), slippage_allowance=Decimal(slippage))
    if _S["spread_kind"] == "2026":
        prof = json.loads((ROOT / "data/processed/mt5_xauusd_tick_spreads.json").read_text())["by_new_york_hour_p50_p90"]
        default = [0.17, 0.45]
    else:
        prof, default = _S["hist_spread"], [0.27, 0.45]
    qi = 0 if q == "p50" else 1
    table = np.array([prof.get(str(h), default)[qi] for h in range(24)], dtype=float)

    def spread_fn(t):
        hours = pd.DatetimeIndex(pd.to_datetime(t, utc=True)).tz_convert("America/New_York").hour
        return table[np.asarray(hours)]
    store = EventStore()
    store.upsert([EconomicEvent.from_json(e) for e in _S["events"]])
    swap = SwapSpec(Decimal("-12.6"), Decimal("-4.6"), Decimal("1"), "Wednesday", EET_EU,
                    "mt5.symbol_info 2026-11-07 (verified)", True)

    def day_key(ns):
        return (pd.Timestamp(ns, tz="UTC").tz_convert("America/New_York") + pd.Timedelta(hours=7)).date()
    return MarketSpec(Market.XAUUSD_MT5, inst, limits, spread_fn, "bid", day_key, base._session, calendar=_S["calendar"],
                      event_guard=EventBlackout(store=store, market=Market.XAUUSD_MT5, max_calendar_age=None), swap=swap)


def run_one(cands: pd.DataFrame, cost: str, equity: float = 10_000.0) -> dict:
    res = simulate(cands, _S["base1"], spec(*COSTS[cost]), AccountRules(initial_equity=equity),
                   start_ns=_S["start"], end_ns=_S["end"])
    s = summarize(res)
    trades = res["trades"]
    r = [t["r"] for t in trades if t.get("r") is not None]
    years: dict = {}
    for t in trades:
        y = pd.Timestamp(t["closed_ns"], tz="UTC").year
        years[y] = years.get(y, 0.0) + t["pnl"]
    return {"cost": cost, "equity": equity, "candidates": res["candidates"], "trades": len(trades),
            "mean_r": float(np.mean(r)) if r else None, "summary": s, "net_by_year": years,
            "rejections_top": dict(sorted(res["rejections"].items(), key=lambda kv: -kv[1])[:5])}


def main() -> int:
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)
    prereg = json.loads(PREREG.read_text())
    ms = prereg["common"]["stop_atr_multiples_research_variable"]
    report = {"prereg_sha256": hashlib.sha256(PREREG.read_bytes()).hexdigest(), "status": "development only", "hypotheses": {}}
    load("dev_2025_2026")
    report["dev_2025_2026_sha256"] = _S["dataset_sha256"]
    cands_cache = {}
    for name, fn in HYPOTHESES.items():
        h = report["hypotheses"][name] = {"variants": {}}
        for m in ms:
            c = fn(_S["frames"], m)
            cands_cache[(name, m)] = c
            h["variants"][str(m)] = {cost: run_one(c, cost) for cost in COSTS}
            v = h["variants"][str(m)]
            print(f"{name} m={m}: base trades {v['base']['trades']} meanR {v['base']['mean_r']}  "
                  f"stress meanR {v['stress']['mean_r']}", flush=True)
        eligible = [(m, h["variants"][str(m)]["base"]) for m in ms if h["variants"][str(m)]["base"]["trades"] >= 100]
        if not eligible:
            h.update(selected=None, gate="rejected: no variant with >= 100 development trades")
            continue
        m_sel, sel = max(eligible, key=lambda x: x[1]["mean_r"] if x[1]["mean_r"] is not None else -9)
        h["selected"] = m_sel
        stress = h["variants"][str(m_sel)]["stress"]["mean_r"]
        h["gate_2025_2026"] = {"base_mean_r": sel["mean_r"], "stress_mean_r": stress,
                               "pass": bool(sel["mean_r"] and sel["mean_r"] > 0 and stress is not None and stress >= 0)}
        ref = cands_cache[(name, m_sel)]
        rand = []
        for seed in range(1, 6):
            rc = random_candidates(ref[(ref.time_ns >= _S["start"]) & (ref.time_ns < _S["end"])], _S["frames"]["1m"],
                                   _S["atr5_on_1m"], seed=seed, start_ns=_S["start"], end_ns=_S["end"], allow_short=True)
            rand.append(run_one(rc, "base")["mean_r"])
        h["random_controls_base_mean_r"] = rand
        h["feasibility_3000"] = run_one(ref, "base", 3000.0)
        print(f"  selected m={m_sel}; gate 2025-26 {h['gate_2025_2026']}; random {rand}", flush=True)
    del cands_cache
    _S.clear()
    gc.collect()
    load("dev_2019_2020")
    report["dev_2019_2020_sha256"] = _S["dataset_sha256"]
    for name, fn in HYPOTHESES.items():
        h = report["hypotheses"][name]
        if h.get("selected") is None:
            continue
        r19 = run_one(fn(_S["frames"], h["selected"]), "base")
        h["dev_2019_2020_base"] = r19
        g = h["gate_2025_2026"]["pass"] and r19["mean_r"] is not None and r19["mean_r"] > 0
        h["gate"] = "advance to independent validation" if g else "rejected at the development gate"
        print(f"{name}: 2019-20 meanR {r19['mean_r']} trades {r19['trades']} -> {h['gate']}", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "development_report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
