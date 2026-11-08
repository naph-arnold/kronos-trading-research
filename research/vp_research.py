"""Volume Profile value-area research (vp-v1): rejection (A) and breakout-retest (B), with and without 4H
confirmation, on BTC/USDT (measured traded volume) and XAUUSD (quote-tick PROXY). Research only; no orders.

Follows research/configs/vp_v1_protocol.json (written before any strategy result):

    python -m research.vp_research dev --dataset btc_dev --workers 10      # walk-forward development
    python -m research.vp_research dev --dataset xau_dev --workers 10
    python -m research.vp_research frozen --dataset xau_val --from xau_dev  # single run of frozen selections
    python -m research.vp_research frozen --dataset btc_final --prereg research/forward_test/vp_btc_2017_2018_preregistration.json

Every entry goes through trading.mtf.simulator.simulate -> trading.risk.evaluate (unchanged): 0.5% intended
risk on current equity, 1.5% daily halt, 1% aggregate open risk, minimum size, spread cap, session calendar and
event blackouts (XAUUSD), long-only spot (BTC/USDT).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import statistics
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from research import mtf_research as base
from research.mtf_scalp4h_oos import drawdown_analysis
from research.mtf_scalp4h_research import _bootstrap_diff
from trading.common import Market
from trading.evaluation import param_grid
from trading.mtf.features import atr
from trading.mtf.frames import Frame, from_csv, resample, validate_base
from trading.mtf.simulator import AccountRules, MarketSpec, simulate, summarize
from trading.mtf.strategies import random_candidates
from trading.risk import RiskLimits
from trading.vp.profile import BinStore, levels_from_table, periods, tpo_store, value_area
from trading.vp.strategies import FEATURES, VPParams, direction_4h, value_area_candidates

PROTOCOL = Path("research/configs/vp_v1_protocol.json")
OUT = Path("research/output/vp/v1")
NS_DAY = 86_400_000_000_000
VA_GRID = (0.6, 0.7, 0.8)
BIN_GRID = (1.0, 2.0, 5.0)
_C: dict = {}


def _sha(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def protocol() -> dict:
    return json.loads(PROTOCOL.read_text())


# ------------------------------------------------------------------------------------------------ data

def load_bars(ds: dict) -> Frame:
    path = Path(ds["bars"])
    man = json.loads(path.with_suffix(path.suffix + ".json").read_text())
    if _sha(path) != man["sha256"]:
        raise ValueError(f"{path}: checksum mismatch")
    if ds["market"] == "btcusdt":
        df = from_csv(path, time_col="timestamps", columns={k: k for k in ("open", "high", "low", "close", "volume")})
    else:
        val = path.with_suffix(path.suffix + ".validation.json")
        if not json.loads(val.read_text())["passed"]:
            raise ValueError(f"{path}: dataset did not pass validation")
        df = from_csv(path, time_col="open_time_utc", columns={"open": "open", "high": "high", "low": "low",
                                                               "close": "close", "volume": "tick_volume"},
                      spread_col="spread_points", spread_scale=0.01)
    df = df[(df.index >= pd.Timestamp(ds["warm"])) & (df.index < pd.Timestamp(ds["end"]))]
    validate_base(df, "1m")
    return Frame("1m", df)


def level_tables(ds_name: str) -> dict:
    """Completed-period level tables for every (source, period, bin_bps, va_pct), plus profile diagnostics.
    Cached under research/output/vp/v1/levels_<dataset>.pkl (keyed by the input checksums)."""
    proto = protocol()
    ds = proto["datasets"][ds_name]
    mk = ds["market"]
    store = BinStore.load(ds["bins"])
    key = {"bins_sha": store.manifest["sha256"], "bars": ds["bars"], "protocol_profile": proto["profile"]}
    cache = OUT / f"levels_{ds_name}.pkl"
    if cache.exists():
        obj = pickle.loads(cache.read_bytes())
        if obj["key"] == key:
            return obj
    bars = load_bars(ds)
    start, end = pd.Timestamp(ds["warm"]), pd.Timestamp(ds["end"])
    stores = {"volume": store, "tpo": tpo_store(bars.df, store.base_bin)}
    cov = proto["profile"]["min_coverage"][mk]
    tables, diag = {}, {}
    for pname, kind in proto["profile"]["period_kinds"][mk].items():
        pers = periods(kind, start, end)
        bt = bars.open_time
        rng = {"end_ns": [], "vah": [], "val": [], "poc": [], "low": [], "high": [], "total": []}
        for s, e, _ in pers:
            a, b = np.searchsorted(bt, s), np.searchsorted(bt, e)
            nb = np.unique(bt[a:b] // (15 * 60 * 10**9)).size if b > a else 0
            if b <= a or nb < np.ceil(cov * (e - s) / (15 * 60 * 10**9)):
                continue
            hi, lo = float(bars.df["high"].to_numpy()[a:b].max()), float(bars.df["low"].to_numpy()[a:b].min())
            for k, v in (("end_ns", e), ("vah", hi), ("val", lo), ("poc", (hi + lo) / 2), ("low", lo), ("high", hi),
                         ("total", float(b - a))):
                rng[k].append(v)
        rt = {k: np.array(v, dtype=np.int64 if k == "end_ns" else float) for k, v in rng.items()}
        for va in VA_GRID:
            for bb in BIN_GRID:
                tables[("range", pname, bb, va)] = rt
        for src, st in stores.items():
            for bb in BIN_GRID:
                rows = {va: {k: [] for k in rt} for va in VA_GRID}
                for s, e, _ in pers:
                    need = max(1, int(np.ceil(cov * (e - s) / st.bucket_ns_len)))
                    p = st.profile(s, e, bin_bps=bb, va_pct=VA_GRID[0], min_buckets=need)
                    if p is None:
                        continue
                    for va in VA_GRID:
                        poc, lo_i, hi_i = value_area(p.weights, va)
                        vals = {"end_ns": e, "vah": p.lo_price + (hi_i + 1) * p.bin_size, "val": p.lo_price + lo_i * p.bin_size,
                                "poc": p.lo_price + (poc + 0.5) * p.bin_size, "low": p.low, "high": p.high, "total": p.total}
                        for k, v in vals.items():
                            rows[va][k].append(v)
                for va in VA_GRID:
                    tables[(src, pname, bb, va)] = {k: np.array(v, dtype=np.int64 if k == "end_ns" else float)
                                                    for k, v in rows[va].items()}
        diag[pname] = _profile_diagnostics(tables, pname, len(pers))
    obj = {"key": key, "tables": tables, "diagnostics": diag, "weight_kind": store.weight_kind,
           "base_bin": store.base_bin, "bins_manifest": {k: store.manifest.get(k) for k in ("sha256", "rows", "weight_kind", "weight_definition", "span_utc", "requested", "ticks", "integrity")}}
    OUT.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(pickle.dumps(obj))
    return obj


def _profile_diagnostics(tables: dict, pname: str, n_periods: int) -> dict:
    """Engine-level checks (no trading): coverage, VA width, how far volume levels sit from time-at-price and
    range levels, and how much levels move with bin size."""
    v, t = tables[("volume", pname, 2.0, 0.7)], tables[("tpo", pname, 2.0, 0.7)]
    out = {"periods": n_periods, "volume_profiles": int(len(v["end_ns"])), "tpo_profiles": int(len(t["end_ns"]))}
    common, iv, it = np.intersect1d(v["end_ns"], t["end_ns"], return_indices=True)
    mid = (v["high"][iv] + v["low"][iv]) / 2
    bps = lambda x: x / mid * 1e4  # noqa: E731
    out["median_va_width_bps"] = round(float(np.median(bps(v["vah"][iv] - v["val"][iv]))), 2)
    out["median_range_bps"] = round(float(np.median(bps(v["high"][iv] - v["low"][iv]))), 2)
    out["median_abs_poc_volume_vs_tpo_bps"] = round(float(np.median(np.abs(bps(v["poc"][iv] - t["poc"][it])))), 2)
    out["median_abs_vah_volume_vs_tpo_bps"] = round(float(np.median(np.abs(bps(v["vah"][iv] - t["vah"][it])))), 2)
    out["median_abs_val_volume_vs_tpo_bps"] = round(float(np.median(np.abs(bps(v["val"][iv] - t["val"][it])))), 2)
    out["share_poc_identical_volume_vs_tpo"] = round(float(np.mean(np.isclose(v["poc"][iv], t["poc"][it]))), 3)
    for bb in (1.0, 5.0):
        o = tables[("volume", pname, bb, 0.7)]
        c2, i2, j2 = np.intersect1d(v["end_ns"], o["end_ns"], return_indices=True)
        m2 = (v["high"][i2] + v["low"][i2]) / 2
        out[f"median_abs_vah_shift_bin2_vs_bin{int(bb)}_bps"] = round(float(np.median(np.abs((v["vah"][i2] - o["vah"][j2]) / m2 * 1e4))), 2)
        out[f"median_abs_poc_shift_bin2_vs_bin{int(bb)}_bps"] = round(float(np.median(np.abs((v["poc"][i2] - o["poc"][j2]) / m2 * 1e4))), 2)
    for va in VA_GRID:
        w = tables[("volume", pname, 2.0, va)]
        out[f"median_va_width_bps_va{int(va * 100)}"] = round(float(np.median((w["vah"] - w["val"]) / ((w["high"] + w["low"]) / 2) * 1e4)), 2)
    return out


# ------------------------------------------------------------------------------------------------ worker state

def _init(ds_name: str) -> None:
    proto = protocol()
    ds = proto["datasets"][ds_name]
    b1 = load_bars(ds)
    frames = {"1m": b1, **{iv: resample(b1, iv) for iv in ("5m", "15m", "1h", "4h")}}
    lt = level_tables(ds_name)
    _C.clear()
    _C.update(ds_name=ds_name, ds=ds, market=ds["market"], proto=proto, base1=b1, frames=frames, tables=lt["tables"],
              cands={}, allow_short=ds["market"] == "xauusd", start=pd.Timestamp(ds["start"]).value,
              end=pd.Timestamp(ds["end"]).value, max_age=int(proto["profile"]["max_level_age_days"] * NS_DAY))
    if ds["market"] == "btcusdt":
        base._S["bps_ref"] = (frames["5m"].open_time, frames["5m"].df["close"].to_numpy())
    else:
        from trading.marketcal.holidays import build_xauusd_calendar
        from trading.sessions import XAUUSD_NY_MARKET_HOURS
        idx = b1.df.index
        cal, _ = build_xauusd_calendar(XAUUSD_NY_MARKET_HOURS, idx[0].date(), idx[-1].date() + pd.Timedelta(days=2))
        _C["calendar"] = cal
        man = json.loads(Path(ds["bars"] + ".json").read_text())
        _C["hist_spread"] = man["spread_by_new_york_hour_p50_p90"]
    _C["specs"] = {}


def xau_spec(commission: str, slippage: str, quantile: int) -> MarketSpec:
    """XAUUSD MarketSpec as in research.mtf_research.market_spec('xauusd'), with the dataset's own historical
    spread profile (p50 or p90 by New York hour) and the official FOMC dates covering every dataset span."""
    from trading.clock import EET_EU
    from trading.connectors.mt5 import SpecDiscovery
    from trading.marketcal.blackout import EventBlackout
    from trading.marketcal.events import EconomicEvent, EventStore
    from trading.shadow.costs import SwapSpec
    rec = json.loads(Path("data/processed/mt5_xauusd_discovery.json").read_text())["specification"]
    disc = SpecDiscovery(rec["symbol"], rec["retrieved_at"], rec["fields"], rec["missing"], rec["problems"])
    inst = disc.to_instrument_spec(commission_assumption=Decimal(commission))
    limits = RiskLimits(market=Market.XAUUSD_MT5, max_spread=Decimal("1.00"), slippage_allowance=Decimal(slippage))
    prof = _C["hist_spread"]
    table = np.array([prof.get(str(h), [0.27, 0.45])[quantile] for h in range(24)], dtype=float)

    def spread_fn(t):
        hours = pd.DatetimeIndex(pd.to_datetime(t, utc=True)).tz_convert("America/New_York").hour
        return table[np.asarray(hours)]
    store = EventStore()
    for f in ("data/processed/fomc_events_2019_2020.json", "data/processed/fomc_events_2025.json", "data/processed/fomc_events.json"):
        store.upsert([EconomicEvent.from_json(e) for e in json.loads(Path(f).read_text())])
    swap = SwapSpec(Decimal("-12.6"), Decimal("-4.6"), Decimal("1"), "Wednesday", EET_EU,
                    "mt5.symbol_info 2026-11-07 (verified)", True)

    def day_key(ns):
        return (pd.Timestamp(ns, tz="UTC").tz_convert("America/New_York") + pd.Timedelta(hours=7)).date()
    return MarketSpec(Market.XAUUSD_MT5, inst, limits, spread_fn, "bid", day_key, base._session, calendar=_C["calendar"],
                      event_guard=EventBlackout(store=store, market=Market.XAUUSD_MT5, max_calendar_age=None), swap=swap)


def spec(scenario: str) -> MarketSpec:
    if scenario not in _C["specs"]:
        if _C["market"] == "btcusdt":
            _C["specs"][scenario] = base.market_spec("btcusdt", scenario)
        else:
            _C["specs"][scenario] = {"base": lambda: xau_spec("0", "0.10", 0), "stress": lambda: xau_spec("7", "0.30", 0),
                                     "stress_p90": lambda: xau_spec("7", "0.30", 1)}[scenario]()
    return _C["specs"][scenario]


def stress_scenarios(market: str) -> tuple:
    return ("stress", "low_fee") if market == "btcusdt" else ("stress", "stress_p90")


def cands(p: VPParams) -> pd.DataFrame:
    key = json.dumps(asdict(p), sort_keys=True)
    if key not in _C["cands"]:
        sig = _C["frames"][p.signal_tf]
        table = _C["tables"][(p.level_source, p.profile_period, p.bin_bps, p.va_pct)]
        lv = levels_from_table(table, sig.open_time, _C["max_age"])
        d4 = direction_4h(sig, _C["frames"]["4h"], p) if p.htf_filter else None
        _C["cands"][key] = value_area_candidates(sig, lv, p, allow_short=_C["allow_short"], d4=d4)
    return _C["cands"][key]


def random_matched(ref: pd.DataFrame, p: VPParams, seed: int, s: int, e: int) -> pd.DataFrame:
    sig = _C["frames"][p.signal_tf]
    a = atr(*(sig.df[k].to_numpy() for k in ("high", "low", "close")), p.atr_n)
    r = ref[(ref.time_ns >= s) & (ref.time_ns < e)]
    out = random_candidates(r, sig, a, seed=seed, start_ns=s, end_ns=e, allow_short=_C["allow_short"])
    if len(out):
        out["rr"] = np.random.default_rng(seed + 1000).choice(r.rr.to_numpy(), size=len(out), replace=True)
    return out


def _task(t: dict) -> dict:
    p = t["params"]
    if t.get("select"):
        rows = []
        for k, g in enumerate(t["grid"]):
            res = simulate(cands(g), _C["base1"], spec("base"), AccountRules(), start_ns=t["span"][0], end_ns=t["span"][1],
                           record_rejections=False)
            s = summarize(res, resamples=10)
            rows.append((k, s["net_return_pct"], s["trades"]))
        ok = [r for r in rows if r[2] >= t["min_trades"]]
        best = max(ok, key=lambda r: (r[1], -r[0]))[0] if ok else None
        return {"id": t["id"], "best": best, "rows": rows}
    c = cands(p)
    if t.get("seed") is not None:
        c = pd.concat([random_matched(c, p, t["seed"], s, e) for s, e in t["spans"]], ignore_index=True)
    res = simulate(c, _C["base1"], spec(t.get("scenario", "base")), t.get("rules", AccountRules()),
                   start_ns=t["span"][0], end_ns=t["span"][1])
    feats = c.set_index("time_ns")[list(FEATURES)] if t.get("seed") is None and len(c) else None
    for tr in res["trades"]:
        if feats is not None and tr["signal_ns"] in feats.index:
            row = feats.loc[tr["signal_ns"]]
            row = row.iloc[0] if isinstance(row, pd.DataFrame) else row
            tr.update({k: float(row[k]) for k in FEATURES})
    return {"id": t["id"], **{k: res[k] for k in ("trades", "rejections", "final_equity", "initial_equity", "candidates")}}


# ------------------------------------------------------------------------------------------------ statistics

def deflated_sharpe(r: list[float], trial_srs: list[float]) -> dict | None:
    """Deflated Sharpe ratio (Bailey & Lopez de Prado 2014) of a per-trade R series, given the per-trade
    Sharpe ratios of all trials. Probability that the true Sharpe exceeds the expected maximum of N null trials."""
    r = np.asarray([x for x in r if x is not None], dtype=float)
    srs = np.asarray([x for x in trial_srs if x is not None and np.isfinite(x)], dtype=float)
    if r.size < 10 or srs.size < 2 or r.std() == 0:
        return None
    nd, g = NormalDist(), 0.5772156649
    n = srs.size
    sr = r.mean() / r.std(ddof=1)
    sr0 = np.sqrt(srs.var(ddof=1)) * ((1 - g) * nd.inv_cdf(1 - 1 / n) + g * nd.inv_cdf(1 - 1 / (n * np.e)))
    z = (r - r.mean()) / r.std()
    skew, kurt = float((z ** 3).mean()), float((z ** 4).mean())
    den = np.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr ** 2))
    return {"sr_per_trade": round(float(sr), 4), "sr0_expected_max_of_trials": round(float(sr0), 4), "trials": int(n),
            "trades": int(r.size), "dsr": round(nd.cdf((sr - sr0) * np.sqrt(r.size - 1) / den), 4)}


def _sr(trades) -> float | None:
    r = [t["r"] for t in trades if t["r"] is not None]
    return float(np.mean(r) / np.std(r, ddof=1)) if len(r) > 2 and np.std(r) > 0 else None


def _terciles(trades, key) -> dict:
    vals = [t.get(key) for t in trades if t.get(key) is not None and np.isfinite(t.get(key))]
    if len(vals) < 9:
        return {}
    q1, q2 = np.quantile(vals, [1 / 3, 2 / 3])
    groups = defaultdict(list)
    for t in trades:
        v = t.get(key)
        if v is None or not np.isfinite(v):
            continue
        groups["low" if v <= q1 else "mid" if v <= q2 else "high"].append(t)
    return {k: {"trades": len(v), "mean_r": round(statistics.fmean(x["r"] for x in v), 4),
                "net_pnl": round(sum(x["pnl"] for x in v), 2)} for k, v in groups.items()} | \
        {"cutpoints": [round(float(q1), 4), round(float(q2), 4)]}


def _short(s: dict) -> dict:
    return {k: s[k] for k in ("trades", "net_return_pct", "mean_r", "mean_r_ci95", "mean_r_gross", "cost_r", "win_rate_pct",
                              "profit_factor", "max_drawdown_pct", "sharpe_daily_annualized")}


# ------------------------------------------------------------------------------------------------ stages

def variant_grid(proto: dict, name: str) -> list[VPParams]:
    strat, htf = proto["variants"][name]
    fixed = {k: v for k, v in proto["fixed_params"].items() if k != "execution_tf"}
    proto_p = VPParams(strategy=strat, htf_filter=htf, bin_bps=proto["profile"]["primary_bin_bps"], **fixed)
    return param_grid(proto_p, proto["grid"][strat])


def run_dev(ds_name: str, workers: int) -> dict:
    proto = protocol()
    lt = level_tables(ds_name)
    _init(ds_name)
    ds, mk = _C["ds"], _C["market"]
    wf = proto["walk_forward"]
    wins = base.windows(pd.Timestamp(ds["start"]), pd.Timestamp(ds["end"]), wf["train_months"], wf["test_months"])
    report = {"dataset": ds_name, "role": ds["role"], "market": mk, "weight_kind": lt["weight_kind"],
              "protocol_sha256": _sha(PROTOCOL), "bars_sha256": json.loads(Path(ds["bars"] + ".json").read_text())["sha256"],
              "bins": lt["bins_manifest"], "profile_diagnostics": lt["diagnostics"],
              "windows": [w["label"] for w in wins], "variants": {}}
    grids = {v: variant_grid(proto, v) for v in proto["variants"]}
    with ProcessPoolExecutor(workers, initializer=_init, initargs=(ds_name,)) as pool:
        sel_tasks = [{"id": f"{v}|{k}", "select": True, "params": None, "grid": grids[v], "span": w["train"],
                      "min_trades": wf["min_train_trades"]} for v in grids for k, w in enumerate(wins)]
        sel = {r["id"]: r for r in pool.map(_task, sel_tasks)}
        chosen = {v: {k: (grids[v][sel[f"{v}|{k}"]["best"]] if sel[f"{v}|{k}"]["best"] is not None else None)
                      for k in range(len(wins))} for v in grids}
        tasks = []
        for v in grids:
            for k, w in enumerate(wins):
                p = chosen[v][k]
                if p is None:
                    continue
                tasks.append({"id": f"{v}|oos|{k}", "params": p, "span": w["test"]})
                for src in ("tpo", "range"):
                    tasks.append({"id": f"{v}|ctl-{src}|{k}", "params": replace(p, level_source=src), "span": w["test"]})
                for sc in stress_scenarios(mk):
                    tasks.append({"id": f"{v}|{sc}|{k}", "params": p, "span": w["test"], "scenario": sc})
                tasks.append({"id": f"{v}|3k|{k}", "params": p, "span": w["test"], "rules": AccountRules(initial_equity=3000.0)})
            tested = [k for k in range(len(wins)) if chosen[v][k] is not None]
            for seed in range(1, 21):
                for k in tested:  # each window's random entries match that window's selected candidate set
                    tasks.append({"id": f"{v}|rand{seed}|{k}", "params": chosen[v][k], "span": wins[k]["test"],
                                  "spans": [wins[k]["test"]], "seed": seed})
            for g_i, g in enumerate(grids[v]):
                tasks.append({"id": f"{v}|grid|{g_i}", "params": g, "span": (_C["start"], _C["end"])})
            counts = Counter(json.dumps(asdict(chosen[v][k]), sort_keys=True) for k in tested)
            if counts:
                most = VPParams(**json.loads(counts.most_common(1)[0][0]))
                for bb in proto["profile"]["bin_sensitivity_bps"]:
                    tasks.append({"id": f"{v}|bin{bb}", "params": replace(most, bin_bps=bb), "span": (_C["start"], _C["end"])})
                tasks.append({"id": f"{v}|most", "params": most, "span": (_C["start"], _C["end"])})
        res = {r["id"]: r for r in pool.map(_task, tasks, chunksize=2)}
    regime = base._regime_fn(_C["frames"])
    all_srs = [_sr(res[f"{v}|grid|{i}"]["trades"]) for v in grids for i in range(len(grids[v]))]
    for v in grids:
        tested = [k for k in range(len(wins)) if chosen[v][k] is not None]
        oos = base._chain([res[f"{v}|oos|{k}"] for k in tested], 10_000.0)
        s = summarize(oos)
        trades = oos["trades"]
        rand_means = []
        for seed in range(1, 21):
            ch = base._chain([res[f"{v}|rand{seed}|{k}"] for k in tested], 10_000.0)
            m = summarize(ch, resamples=10)["mean_r"]
            if m is not None:
                rand_means.append(m)
        ctl = {src: base._chain([res[f"{v}|ctl-{src}|{k}"] for k in tested], 10_000.0) for src in ("tpo", "range")}
        scen = {sc: summarize(base._chain([res[f"{v}|{sc}|{k}"] for k in tested], 10_000.0)) for sc in stress_scenarios(mk)}
        k3 = base._chain([res[f"{v}|3k|{k}"] for k in tested], 3_000.0)
        win_net = {wins[k]["label"]: round((res[f"{v}|oos|{k}"]["final_equity"] / 10_000.0 - 1) * 100, 3) for k in tested}
        grid_rows = []
        for i, g in enumerate(grids[v]):
            gs = summarize(res[f"{v}|grid|{i}"], resamples=200)
            grid_rows.append({"params": {k: asdict(g)[k] for k in proto["grid"][grids[v][0].strategy]}, **_short(gs),
                              "sr_per_trade": _sr(res[f"{v}|grid|{i}"]["trades"])})
        best_i = max(range(len(grids[v])), key=lambda i: (grid_rows[i]["net_return_pct"], -i))
        r_all = [t["r"] for t in trades]
        entry = {
            "strategy": grids[v][0].strategy, "htf_4h": grids[v][0].htf_filter,
            "chosen_per_window": {wins[k]["label"]: ({kk: asdict(chosen[v][k])[kk] for kk in proto["grid"][grids[v][0].strategy]}
                                                     if chosen[v][k] else None) for k in range(len(wins))},
            "windows_tested": len(tested), "windows_positive": sum(1 for x in win_net.values() if x > 0), "window_net_pct": win_net,
            "oos": s, "drawdown": drawdown_analysis(oos),
            "random_mean_r": {"median": statistics.median(rand_means) if rand_means else None,
                              "min": min(rand_means, default=None), "max": max(rand_means, default=None)},
            "beats_random": f"{sum(1 for m in rand_means if s['mean_r'] is not None and s['mean_r'] > m)}/{len(rand_means)}",
            "controls": {src: _short(summarize(c)) for src, c in ctl.items()},
            "volume_minus_control_mean_r": {src: _bootstrap_diff(r_all, [t["r"] for t in c["trades"]]) for src, c in ctl.items()},
            "costs": {sc: _short(x) for sc, x in scen.items()},
            "eligibility_3000": {"trades": len(k3["trades"]), "net_return_pct": summarize(k3)["net_return_pct"],
                                 "min_size_rejections": sum(n for kk, n in k3["rejections"].items() if kk.startswith(("Minimum size", "Notional")))},
            "breakdowns": {**base._breakdowns(trades, regime),
                           "va_width_atr_tercile": _terciles(trades, "va_width_atr"),
                           "dist_poc_atr_tercile": _terciles(trades, "dist_poc_atr"),
                           "profile_weight_tercile": _terciles(trades, "profile_total"),
                           "cost_r_by_session": {ss: round(statistics.fmean(t["r_gross"] - t["r"] for t in trades if t["session"] == ss), 4)
                                                 for ss in sorted({t["session"] for t in trades})}},
            "rejections": dict(sorted(oos["rejections"].items(), key=lambda x: -x[1])[:10]),
            "full_grid_diagnostic": grid_rows,
            "full_grid_profitable": f"{sum(1 for g in grid_rows if g['net_return_pct'] > 0)}/{len(grid_rows)}",
            "va_pct_sensitivity_mean_r": {str(va): round(statistics.fmean(g["mean_r"] for g in grid_rows if g["params"]["va_pct"] == va and g["mean_r"] is not None), 4)
                                          for va in VA_GRID},
            "best_full_grid": {"params": grid_rows[best_i]["params"], "net_return_pct": grid_rows[best_i]["net_return_pct"],
                               "deflated_sharpe": deflated_sharpe([t["r"] for t in res[f"{v}|grid|{best_i}"]["trades"]], all_srs)},
        }
        if f"{v}|most" in res:
            entry["most_selected"] = {"params": {kk: asdict(VPParams(**json.loads(Counter(json.dumps(asdict(chosen[v][k]), sort_keys=True) for k in tested).most_common(1)[0][0])))[kk]
                                                 for kk in proto["grid"][grids[v][0].strategy]},
                                      "full_span": _short(summarize(res[f"{v}|most"])),
                                      "bin_sensitivity": {str(bb): _short(summarize(res[f"{v}|bin{bb}"])) for bb in proto["profile"]["bin_sensitivity_bps"]}}
        stress_key = stress_scenarios(mk)[0]
        dsr = entry["best_full_grid"]["deflated_sharpe"]
        entry["decision"] = {
            "mean_r_ci_lower_gt_0": bool(s["mean_r"] is not None and s["mean_r"] > 0 and s["mean_r_ci95"] and s["mean_r_ci95"][0] > 0),
            "net_positive_under_stress": bool(scen[stress_key]["net_return_pct"] > 0),
            "beats_random_18_of_20": sum(1 for m in rand_means if s["mean_r"] is not None and s["mean_r"] > m) >= 18,
            "windows_positive_60pct": bool(tested and entry["windows_positive"] / len(tested) >= 0.6),
            "deflated_sharpe_ge_0.95": bool(dsr and dsr["dsr"] >= 0.95)}
        entry["decision"]["dev_candidate"] = all(entry["decision"].values())
        entry["decision"]["volume_adds_information"] = all(
            (d["ci95"] is not None and d["ci95"][0] > 0) for d in entry["volume_minus_control_mean_r"].values())
        report["variants"][v] = entry
    return report


def run_frozen(ds_name: str, frozen: dict, workers: int, source: str) -> dict:
    """Single evaluation of frozen parameters per variant (no selection)."""
    lt = level_tables(ds_name)
    _init(ds_name)
    ds, mk = _C["ds"], _C["market"]
    span = (_C["start"], _C["end"])
    tasks = []
    for v, p in frozen.items():
        tasks.append({"id": f"{v}|base", "params": p, "span": span})
        for src in ("tpo", "range"):
            tasks.append({"id": f"{v}|ctl-{src}", "params": replace(p, level_source=src), "span": span})
        for sc in stress_scenarios(mk):
            tasks.append({"id": f"{v}|{sc}", "params": p, "span": span, "scenario": sc})
        tasks.append({"id": f"{v}|3k", "params": p, "span": span, "rules": AccountRules(initial_equity=3000.0)})
        tasks += [{"id": f"{v}|rand{s}", "params": p, "span": span, "spans": [span], "seed": s} for s in range(1, 21)]
    with ProcessPoolExecutor(workers, initializer=_init, initargs=(ds_name,)) as pool:
        res = {r["id"]: r for r in pool.map(_task, tasks, chunksize=2)}
    regime = base._regime_fn(_C["frames"])
    report = {"dataset": ds_name, "role": ds["role"], "market": mk, "weight_kind": lt["weight_kind"], "frozen_from": source,
              "protocol_sha256": _sha(PROTOCOL), "bars_sha256": json.loads(Path(ds["bars"] + ".json").read_text())["sha256"],
              "bins": lt["bins_manifest"], "profile_diagnostics": lt["diagnostics"], "span": [ds["start"], ds["end"]], "variants": {}}
    for v, p in frozen.items():
        b = res[f"{v}|base"]
        s = summarize(b)
        rm = sorted(m for m in (summarize(res[f"{v}|rand{k}"], resamples=10)["mean_r"] for k in range(1, 21)) if m is not None)
        r_all = [t["r"] for t in b["trades"]]
        years = defaultdict(list)
        for t in b["trades"]:
            years[str(pd.Timestamp(t["opened_ns"]).year)].append(t["r"])
        stress_key = stress_scenarios(mk)[0]
        stress = summarize(res[f"{v}|{stress_key}"])
        entry = {"params": asdict(p), "base": s, "drawdown": drawdown_analysis(b),
                 "random_mean_r": {"median": statistics.median(rm) if rm else None, "min": rm[0] if rm else None, "max": rm[-1] if rm else None},
                 "beats_random": f"{sum(1 for m in rm if s['mean_r'] is not None and s['mean_r'] > m)}/{len(rm)}",
                 "controls": {src: _short(summarize(res[f"{v}|ctl-{src}"])) for src in ("tpo", "range")},
                 "volume_minus_control_mean_r": {src: _bootstrap_diff(r_all, [t["r"] for t in res[f"{v}|ctl-{src}"]["trades"]]) for src in ("tpo", "range")},
                 "costs": {sc: _short(summarize(res[f"{v}|{sc}"])) for sc in stress_scenarios(mk)},
                 "eligibility_3000": {"trades": len(res[f"{v}|3k"]["trades"]), "net_return_pct": summarize(res[f"{v}|3k"])["net_return_pct"],
                                      "min_size_rejections": sum(n for kk, n in res[f"{v}|3k"]["rejections"].items() if kk.startswith(("Minimum size", "Notional")))},
                 "breakdowns": {**base._breakdowns(b["trades"], regime),
                                "va_width_atr_tercile": _terciles(b["trades"], "va_width_atr"),
                                "dist_poc_atr_tercile": _terciles(b["trades"], "dist_poc_atr"),
                                "profile_weight_tercile": _terciles(b["trades"], "profile_total")},
                 "mean_r_by_year": {y: round(statistics.fmean(x), 4) for y, x in sorted(years.items())},
                 "rejections": dict(sorted(b["rejections"].items(), key=lambda x: -x[1])[:10])}
        entry["checks"] = {"mean_r_ci_lower_gt_0": bool(s["mean_r"] is not None and s["mean_r"] > 0 and s["mean_r_ci95"] and s["mean_r_ci95"][0] > 0),
                           "net_positive_under_stress": bool(stress["net_return_pct"] > 0),
                           "beats_random_18_of_20": sum(1 for m in rm if s["mean_r"] is not None and s["mean_r"] > m) >= 18}
        entry["passed"] = all(entry["checks"].values())
        report["variants"][v] = entry
    return report


def frozen_from_dev(dev_report: dict) -> dict:
    proto = protocol()
    out = {}
    for v, e in dev_report["variants"].items():
        if "most_selected" not in e:
            continue
        g = variant_grid(proto, v)[0]
        out[v] = replace(g, **e["most_selected"]["params"])
    return out


def to_markdown(r: dict) -> str:
    lines = [f"# Volume Profile vp-v1 - {r['dataset']} ({r['market']}, weights: {r['weight_kind']})", "", r["role"], "",
             f"Protocol sha256 `{r['protocol_sha256']}`; bars sha256 `{r['bars_sha256']}`.", "",
             "| Variant | Trades | Net % | Mean R (95% CI) | Gross R | Cost R | PF | Max DD % | Beats random | TPO-level mean R | Range-level mean R | Stress net % |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for v, e in r["variants"].items():
        s = e.get("oos") or e.get("base")
        st = e["costs"][next(iter(e["costs"]))]
        lines.append(f"| {v} | {s['trades']} | {s['net_return_pct']} | {s['mean_r']} {s['mean_r_ci95']} | {s['mean_r_gross']} | {s['cost_r']} | "
                     f"{s['profit_factor']} | {s['max_drawdown_pct']} | {e['beats_random']} | {e['controls']['tpo']['mean_r']} | "
                     f"{e['controls']['range']['mean_r']} | {st['net_return_pct']} |")
    lines += ["", "Decisions:"]
    for v, e in r["variants"].items():
        lines.append(f"- {v}: {json.dumps(e.get('decision') or e.get('checks'), default=str)}")
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dev")
    d.add_argument("--dataset", required=True)
    d.add_argument("--workers", type=int, default=10)
    f = sub.add_parser("frozen")
    f.add_argument("--dataset", required=True)
    f.add_argument("--from", dest="src")
    f.add_argument("--prereg")
    f.add_argument("--workers", type=int, default=10)
    lv = sub.add_parser("levels")
    lv.add_argument("--dataset", required=True)
    a = p.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if a.cmd == "levels":
        print(json.dumps(level_tables(a.dataset)["diagnostics"], indent=2))
        return
    if a.cmd == "dev":
        rep = run_dev(a.dataset, a.workers)
    else:
        if a.prereg:
            pre = json.loads(Path(a.prereg).read_text())
            frozen = {v: VPParams(**x) for v, x in pre["frozen_variants"].items()}
            src = f"{a.prereg} sha256 {_sha(a.prereg)}"
        else:
            frozen = frozen_from_dev(json.loads((OUT / f"{a.src}.json").read_text()))
            src = f"{OUT / (a.src + '.json')} sha256 {_sha(OUT / (a.src + '.json'))}"
        rep = run_frozen(a.dataset, frozen, a.workers, src)
    (OUT / f"{a.dataset}.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    (OUT / f"{a.dataset}.md").write_text(to_markdown(rep), encoding="utf-8")
    print(to_markdown(rep))


if __name__ == "__main__":
    main()
