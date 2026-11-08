"""XAUUSD M1 history built from broker ticks (read-only MT5), with validation. Research only; no orders.

Bar history through the MT5 API is capped by the terminal's "Max bars in chart" setting (100,000), and raising
it requires a terminal restart. Tick history (`copy_ticks_range`) is not capped by that setting, so older M1 bars
are rebuilt here from the broker's own ticks with the existing read-only connector, then validated against the
broker's own bars.

    python -m research.xau_tick_history download --start 2019-01-01 --end 2021-01-01 --output data/raw/mt5_xauusd_1m_ticks_2019_2020.csv
    python -m research.xau_tick_history validate --dataset data/raw/mt5_xauusd_1m_ticks_2019_2020.csv
    python -m research.xau_tick_history fomc --html <fomchistorical2019.htm> <fomchistorical2020.htm> --output data/processed/fomc_events_2019_2020.json

Bars: open/high/low/close of the bid (MT5 builds XAUUSD charts from bid), tick_volume = number of ticks,
spread_points = minimum spread in the minute (points of 0.01).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path

import numpy as np
import pandas as pd

from trading.clock import EET_EU, NEW_YORK
from trading.connectors.mt5 import Mt5ReadOnlyConnector, _tick_rows

POINT = 0.01
FIELDS = ("open_time_utc", "server_time", "open", "high", "low", "close", "tick_volume", "spread_points")
MAX_SPREAD_PTS = 5000


def _connect() -> Mt5ReadOnlyConnector:
    conn = Mt5ReadOnlyConnector(server_tz=EET_EU)
    conn.connect()  # verifies a DEMO account; the proxy blocks every order function
    return conn


def _chunk_bars(conn, name: str, start: datetime, end: datetime, spread_hist: np.ndarray | None) -> tuple[pd.DataFrame, int]:
    mt5 = conn._require()
    s_srv = conn.server_tz.to_server(start).replace(tzinfo=timezone.utc)
    e_srv = conn.server_tz.to_server(end).replace(tzinfo=timezone.utc)
    rows = _tick_rows(mt5, name, s_srv, e_srv)
    if rows is None:
        raise RuntimeError(f"copy_ticks_range failed: {mt5.last_error()}")
    if len(rows) == 0:
        return pd.DataFrame(), 0
    off_a, off_b = conn.server_tz.offset_at(start), conn.server_tz.offset_at(end)
    msc = rows["time_msc"].astype(np.int64)
    if off_a == off_b:
        utc_ms = msc - int(off_a / timedelta(milliseconds=1))
    else:  # DST switch inside the chunk: convert tick by tick
        utc_ms = np.array([int(conn.server_tz.server_msc_to_utc(int(m)).timestamp() * 1000) for m in msc], dtype=np.int64)
    bid, ask = rows["bid"].astype(float), rows["ask"].astype(float)
    ok = (bid > 0) & (ask >= bid) & (utc_ms >= int(start.timestamp() * 1000)) & (utc_ms < int(end.timestamp() * 1000))
    utc_ms, bid, ask, srv_ms = utc_ms[ok], bid[ok], ask[ok], msc[ok]
    if not len(bid):
        return pd.DataFrame(), 0
    spread = np.rint((ask - bid) / POINT).astype(np.int64)
    if spread_hist is not None:
        hours = pd.DatetimeIndex(pd.to_datetime(utc_ms, unit="ms", utc=True)).tz_convert("America/New_York").hour
        np.add.at(spread_hist, (np.asarray(hours), np.clip(spread, 0, MAX_SPREAD_PTS - 1)), 1)
    minute = utc_ms // 60_000
    df = pd.DataFrame({"m": minute, "bid": bid, "spread": spread, "srv": srv_ms // 60_000 * 60})
    g = df.groupby("m", sort=True)
    out = pd.DataFrame({"open": g.bid.first(), "high": g.bid.max(), "low": g.bid.min(), "close": g.bid.last(),
                        "tick_volume": g.bid.size(), "spread_points": g.spread.min(), "server_time": g.srv.first()})
    out.index = pd.to_datetime(out.index * 60_000, unit="ms", utc=True)
    return out, int(len(bid))


def build_bars(conn, name: str, start: datetime, end: datetime, *, chunk_hours: int = 6, spread_hist=None,
               progress: bool = False) -> tuple[pd.DataFrame, dict]:
    parts, ticks, cursor = [], {}, start
    while cursor < end:
        nxt = min(cursor + timedelta(hours=chunk_hours), end)
        part, n = _chunk_bars(conn, name, cursor, nxt, spread_hist)
        if n:
            parts.append(part)
            ticks[cursor.strftime("%Y-%m")] = ticks.get(cursor.strftime("%Y-%m"), 0) + n
        if progress and cursor.day == 1 and cursor.hour == 0:
            print(cursor.date(), sum(ticks.values()), flush=True)
        cursor = nxt
    bars = pd.concat(parts) if parts else pd.DataFrame()
    if not bars.empty and not bars.index.is_unique:
        raise ValueError("duplicate minutes across chunks")
    return bars, ticks


def _write(bars: pd.DataFrame, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = bars.copy()
    out.insert(0, "open_time_utc", [t.isoformat() for t in out.index])
    out = out[list(FIELDS)]
    out.to_csv(path, index=False, lineterminator="\n", float_format="%.2f")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _percentiles(hist: np.ndarray) -> dict:
    out = {}
    for h in range(24):
        c = np.cumsum(hist[h])
        if c[-1] == 0:
            continue
        out[str(h)] = [round(float(np.searchsorted(c, q * c[-1]) * POINT), 2) for q in (0.5, 0.9)]
    return out


def cmd_download(a) -> dict:
    start = datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc)
    hist = np.zeros((24, MAX_SPREAD_PTS), dtype=np.int64)
    conn = _connect()
    try:
        name = conn.discover_xauusd()
        bars, ticks = build_bars(conn, name, start, end, spread_hist=hist, progress=True)
    finally:
        conn.shutdown()
    if bars.empty:
        raise SystemExit("broker returned no ticks for the requested span")
    out = Path(a.output)
    digest = _write(bars, out)
    manifest = {"schema_version": 1, "source": "mt5_demo ticks (copy_ticks_range, COPY_TICKS_INFO), read-only",
                "broker_symbol": name, "interval": "1m", "construction": "bid OHLC; tick_volume = ticks; spread_points = min spread",
                "server_tz": EET_EU.name, "rows": int(len(bars)), "ticks": int(sum(ticks.values())), "ticks_by_month": ticks,
                "sha256": digest, "start": bars.index[0].isoformat(), "end": (bars.index[-1] + timedelta(minutes=1)).isoformat(),
                "requested": [a.start, a.end], "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "spread_by_new_york_hour_p50_p90": _percentiles(hist)}
    out.with_suffix(out.suffix + ".json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {k: manifest[k] for k in ("rows", "ticks", "start", "end", "sha256")}


# ------------------------------------------------------------------------------------------- validation

def _load(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.index = pd.to_datetime(df.pop("open_time_utc"), utc=True)
    return df


def _compare(a: pd.DataFrame, b: pd.DataFrame, cols=("open", "high", "low", "close")) -> dict:
    common = a.index.intersection(b.index)
    d = {c: np.abs(a.loc[common, c].to_numpy() - b.loc[common, c].to_numpy()) for c in cols}
    exact = np.all([d[c] < 0.005 for c in cols], axis=0)
    return {"rows_a": int(len(a)), "rows_b": int(len(b)), "common": int(len(common)),
            "only_tick_built": int(len(a.index.difference(b.index))), "only_broker": int(len(b.index.difference(a.index))),
            "ohlc_exact_share": round(float(exact.mean()), 5) if len(common) else None,
            "max_abs_diff": {c: round(float(d[c].max()), 2) for c in cols} if len(common) else None,
            "p99_abs_diff": {c: round(float(np.percentile(d[c], 99)), 2) for c in cols} if len(common) else None}


def cmd_validate(a) -> dict:
    path = Path(a.dataset)
    manifest = json.loads(path.with_suffix(path.suffix + ".json").read_text())
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["sha256"]:
        raise SystemExit("dataset checksum mismatch")
    bars = _load(path)
    report: dict = {"dataset": str(path), "sha256": manifest["sha256"], "rows": len(bars)}
    idx = bars.index
    report["timestamps"] = {"monotonic": bool(idx.is_monotonic_increasing), "unique": bool(idx.is_unique),
                            "minute_aligned": bool(((idx.asi8 // 10**9) % 60 == 0).all()),
                            "first": str(idx[0]), "last": str(idx[-1])}
    o, h, lo, c = (bars[k].to_numpy() for k in ("open", "high", "low", "close"))
    ret = np.abs(np.diff(np.log(c)))
    report["prices"] = {"non_positive": int((np.minimum.reduce([o, h, lo, c]) <= 0).sum()),
                        "ohlc_inconsistent": int(((h < np.maximum(o, c)) | (lo > np.minimum(o, c))).sum()),
                        "min_close": float(c.min()), "max_close": float(c.max()),
                        "minute_moves_over_1pct": int((ret > 0.01).sum()), "max_minute_move_pct": round(float(ret.max() * 100), 3)}
    # session structure: where do weekday gaps fall in New York time? (17:00-18:00 NY daily break expected)
    ny = idx.tz_convert("America/New_York")
    present = pd.Series(1, index=ny)
    by_hour = present.groupby([ny.dayofweek, ny.hour]).size().unstack(fill_value=0)
    report["session_structure"] = {
        "bars_by_ny_hour_weekdays_mon_thu": {str(hh): int(by_hour.loc[0:3, hh].sum()) if hh in by_hour.columns else 0 for hh in range(24)},
        "bars_saturday_ny": int((ny.dayofweek == 5).sum()),
        "bars_friday_after_17_ny": int(((ny.dayofweek == 4) & (ny.hour >= 17)).sum()),
        "bars_sunday_before_18_ny": int(((ny.dayofweek == 6) & (ny.hour < 18)).sum())}
    # missing minutes against the CME Globex gold calendar (NY hours + CME holidays)
    from trading.marketcal.holidays import build_xauusd_calendar
    from trading.sessions import XAUUSD_NY_MARKET_HOURS
    cal, prov = build_xauusd_calendar(XAUUSD_NY_MARKET_HOURS, idx[0].date(), idx[-1].date() + timedelta(days=2))
    grid = pd.date_range(idx[0], idx[-1], freq="1min")
    open_mask = np.fromiter((cal.is_open(t.to_pydatetime()) for t in grid), bool, len(grid))
    have = grid.isin(idx)
    missing = grid[open_mask & ~have]
    closed = idx[~np.fromiter((cal.is_open(t.to_pydatetime()) for t in idx), bool, len(idx))]
    runs = pd.Series(missing).diff().ne(pd.Timedelta(minutes=1)).cumsum() if len(missing) else pd.Series(dtype=int)
    long_gaps = []
    if len(missing):
        for _, grp in pd.Series(missing).groupby(runs.values):
            if len(grp) >= 30:
                long_gaps.append([str(grp.iloc[0]), len(grp)])
    report["calendar"] = {"open_minutes": int(open_mask.sum()), "present_in_open": int((open_mask & have).sum()),
                          "missing_in_open": int(len(missing)), "missing_share": round(len(missing) / max(1, open_mask.sum()), 5),
                          "bars_in_closed_time": int(len(closed)), "closed_examples": [str(t) for t in closed[:10]],
                          "gaps_30min_plus": long_gaps[:60], "gaps_30min_plus_count": len(long_gaps),
                          "missing_by_year": {str(y): int(n) for y, n in pd.Series(missing.year).value_counts().sort_index().items()},
                          "calendar_provenance": {k: prov[k] for k in ("provider", "calendar", "version") if k in prov},
                          "holidays": prov.get("holidays"), "early_closes": prov.get("early_closes")}
    report["spreads"] = {"by_ny_hour_p50_p90_ticks_2019_2020": manifest.get("spread_by_new_york_hour_p50_p90"),
                         "bar_min_spread_points_quantiles": {q: float(np.quantile(bars.spread_points, q)) for q in (0.5, 0.9, 0.99)},
                         "profile_2026_p50_p90": json.loads(Path("data/processed/mt5_xauusd_tick_spreads.json").read_text())["by_new_york_hour_p50_p90"]}
    report["ticks_by_month"] = manifest["ticks_by_month"]
    # broker cross-checks (read-only): H1 bars for the same span, and tick-built M1 vs broker M1 on an overlap week
    conn = _connect()
    try:
        name = conn.discover_xauusd()
        h1 = conn.history(name, "1h", idx[0].to_pydatetime(), (idx[-1] + timedelta(minutes=1)).to_pydatetime())
        broker_h1 = pd.DataFrame({"open": [float(b.bar.open) for b in h1], "high": [float(b.bar.high) for b in h1],
                                  "low": [float(b.bar.low) for b in h1], "close": [float(b.bar.close) for b in h1]},
                                 index=pd.DatetimeIndex([b.bar.open_time for b in h1]))
        agg = bars.resample("1h", label="left", closed="left").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        report["vs_broker_h1"] = _compare(agg, broker_h1)
        ov_s, ov_e = datetime(2026, 8, 3, tzinfo=timezone.utc), datetime(2026, 8, 29, tzinfo=timezone.utc)
        ov, _ = build_bars(conn, name, ov_s, ov_e)
        m1 = _load(Path("data/raw/mt5_xauusd_1m.csv"))
        m1 = m1[(m1.index >= ov_s) & (m1.index < ov_e)]
        cmp = _compare(ov, m1)
        common = ov.index.intersection(m1.index)
        cmp["spread_exact_share"] = round(float((ov.loc[common, "spread_points"] == m1.loc[common, "spread_points"]).mean()), 4)
        cmp["tick_volume_ratio_median"] = round(float((ov.loc[common, "tick_volume"] / m1.loc[common, "tick_volume"]).median()), 3)
        report["vs_broker_m1_overlap_2026_08"] = cmp
    finally:
        conn.shutdown()
    ok = (report["timestamps"]["monotonic"] and report["timestamps"]["unique"] and report["timestamps"]["minute_aligned"]
          and report["prices"]["non_positive"] == 0 and report["prices"]["ohlc_inconsistent"] == 0
          and report["vs_broker_m1_overlap_2026_08"]["ohlc_exact_share"] >= 0.99
          and report["vs_broker_h1"]["ohlc_exact_share"] >= 0.99)
    report["passed"] = bool(ok)
    out = path.with_suffix(path.suffix + ".validation.json")
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


# ------------------------------------------------------------------------------------------- FOMC 2019-2020

_H5 = re.compile(r'<h5 class="panel-heading[^"]*">([^<]*)</h5>')
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def parse_fomc_historical(html: str, *, retrieved_at: datetime, source_url: str) -> list[dict]:
    """Scheduled FOMC meetings from a federalreserve.gov `fomchistoricalYYYY.htm` page.

    Unscheduled meetings and notation votes are excluded (not knowable in advance). A meeting the page marks
    "cancelled" was still on the published schedule beforehand, so it is kept (it only adds a blackout)."""
    events = []
    for heading in _H5.findall(html):
        text = " ".join(unescape(heading).split())
        m = re.match(r"([A-Za-z/]+)\s+([\d\-]+)\s*(\((?:unscheduled|cancelled|notation vote)\))?\s*(?:Meeting)?\s*-\s*(\d{4})", text)
        if not m:
            continue
        months, days, tag, year = m.group(1), m.group(2), (m.group(3) or "").lower(), int(m.group(4))
        if "unscheduled" in tag or "notation" in tag:
            continue
        month = _MONTHS[months.split("/")[-1][:3].lower()]
        day = date(year, month, int(days.split("-")[-1]))
        when = NEW_YORK.to_utc(datetime(day.year, day.month, day.day, 14, 0))
        events.append({"event_id": f"fomc-{day.isoformat()}", "name": "FOMC rate decision and statement",
                       "country": "US", "currency": "USD", "scheduled_at": when.isoformat(), "impact": "high",
                       "provider": "federalreserve.gov/fomchistorical", "retrieved_at": retrieved_at.isoformat(),
                       "updated_at": retrieved_at.isoformat(), "category": "central_bank",
                       "status": "scheduled", "all_day": False, "previous": None, "forecast": None, "actual": None,
                       "source_url": source_url})  # a "cancelled" meeting stays: it was on the published schedule beforehand
    if not events:
        raise ValueError("no FOMC meetings parsed (page layout changed?)")
    return events


def cmd_fomc(a) -> dict:
    now = datetime.now(timezone.utc)
    events, sources = [], []
    for p in a.html:
        html = Path(p).read_text(encoding="utf-8", errors="replace")
        year = re.search(r"fomchistorical(\d{4})", p) or re.search(r"(\d{4})", Path(p).stem)
        url = f"https://www.federalreserve.gov/monetarypolicy/fomchistorical{year.group(1)}.htm"
        events += parse_fomc_historical(html, retrieved_at=now, source_url=url)
        sources.append({"url": url, "sha256": hashlib.sha256(Path(p).read_bytes()).hexdigest()})
    Path(a.output).write_text(json.dumps(events, indent=2), encoding="utf-8")
    Path(a.output).with_suffix(".sources.json").write_text(json.dumps(sources, indent=2), encoding="utf-8")
    return {"events": [e["event_id"] for e in events], "sources": sources}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--start", required=True)
    d.add_argument("--end", required=True)
    d.add_argument("--output", required=True)
    v = sub.add_parser("validate")
    v.add_argument("--dataset", required=True)
    f = sub.add_parser("fomc")
    f.add_argument("--html", nargs="+", required=True)
    f.add_argument("--output", required=True)
    a = p.parse_args()
    res = {"download": cmd_download, "validate": cmd_validate, "fomc": cmd_fomc}[a.cmd](a)
    print(json.dumps(res, indent=2, default=str)[:6000])


if __name__ == "__main__":
    main()
