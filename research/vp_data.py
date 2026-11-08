"""Volume-at-price source data for Volume Profile research (read-only; research only; no orders).

Two sources, kept strictly apart by `weight_kind`:

* BTC/USDT `traded_base_qty`: Binance official public aggregate trades (data.binance.vision monthly
  `aggTrades`), each archive verified against Binance's published `.CHECKSUM`. Every aggregate trade carries its
  exact price and base quantity, so volume-at-price is MEASURED, not estimated. Completeness is checked through
  the contiguous aggregate-trade ids and the contiguous underlying trade-id ranges (`first_id` = previous
  `last_id` + 1). A per-minute summary (volume, high, low, first, last) is kept to cross-check against the
  official 1m klines.
* XAUUSD `quote_ticks`: MetaQuotes-Demo has no traded volume for the XAUUSD CFD (real_volume = 0, no trade
  ticks; see docs/volume-profile-feasibility.md). The only defensible activity measure is the broker's own quote
  stream: weight = number of bid/ask quote updates observed at each bid price. This is a PROXY for activity, not
  exchange-traded volume, and every output says so.

Storage: weights summed per (15-minute bucket, base price bin). A profile for any period whose boundaries fall on
15-minute marks is then an exact sum of buckets, and a developing profile can be formed from completed buckets
only. Bin index = floor(price / base_bin); strategy bins are integer multiples of base_bin.

    python -m research.vp_data btc --first 2024-06 --last 2026-05 --base-bin 5 --output data/processed/vp/btc_2024-06_2026-05
    python -m research.vp_data btc-validate --bins data/processed/vp/btc_2024-06_2026-05 --klines data/raw/binance_btcusdt_1m_2023-01_2026-11.csv
    python -m research.vp_data xau --start 2019-01-01 --end 2021-01-01 --base-bin 0.10 --output data/processed/vp/xau_2019_2020 [--bars-output ...]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen

import numpy as np
import pandas as pd

BUCKET_MIN = 15
BUCKET_MS = BUCKET_MIN * 60_000
AGG_BASE = "https://data.binance.vision/data/spot/monthly/aggTrades"
AGG_COLS = ["agg_id", "price", "qty", "first_id", "last_id", "ts", "is_buyer_maker", "best_match"]
WEIGHT_KINDS = {
    "traded_base_qty": "Measured exchange-traded base quantity at each trade price (Binance aggTrades)",
    "quote_ticks": "PROXY - number of broker bid/ask quote updates at each bid price (MT5 CFD; NOT traded volume)",
    "tpo_minutes": "Time-at-price - one unit per 1-minute candle per price bin its high-low range touched (no volume)",
}


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def save_bins(prefix: Path, bucket_ms: np.ndarray, bins: np.ndarray, w: np.ndarray, manifest: dict) -> dict:
    """Write `<prefix>.npz` (sorted by bucket, bin) and `<prefix>.json` (manifest with sha256)."""
    order = np.lexsort((bins, bucket_ms))
    prefix.parent.mkdir(parents=True, exist_ok=True)
    npz = prefix.with_suffix(".npz")
    np.savez_compressed(npz, bucket_ms=bucket_ms[order].astype(np.int64), bin=bins[order].astype(np.int64),
                        w=w[order].astype(np.float64))
    manifest = {**manifest, "bucket_minutes": BUCKET_MIN, "rows": int(len(w)), "npz": npz.name, "sha256": _sha_file(npz)}
    prefix.with_suffix(".json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return manifest


def _group_bins(bucket: np.ndarray, bins: np.ndarray, w: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    df = pd.DataFrame({"b": bucket, "k": bins, "w": w}).groupby(["b", "k"], sort=True)["w"].sum()
    return (df.index.get_level_values(0).to_numpy(np.int64), df.index.get_level_values(1).to_numpy(np.int64),
            df.to_numpy(np.float64))


# ------------------------------------------------------------------------------------------ Binance BTC/USDT

def _get(url: str) -> bytes:
    for attempt in range(4):
        try:
            with urlopen(url, timeout=600) as response:
                return response.read()
        except OSError:
            if attempt == 3:
                raise
            time.sleep(10 * (attempt + 1))
    raise RuntimeError("unreachable")


def process_aggtrades_csv(stream, base_bin: float, state: dict, *, chunksize: int = 4_000_000):
    """Aggregate one month of aggTrades rows. `state` carries continuity across months
    (last agg id / last trade id / last timestamp) and accumulates integrity counters."""
    first = stream.readline()
    header = not first.split(b",")[0].strip().isdigit()
    parts_bins, parts_min = [], []
    reader = pd.read_csv(io.BufferedReader(_Prepend(first if not header else b"", stream)), header=None, names=AGG_COLS,
                         usecols=range(6), chunksize=chunksize,
                         dtype={"agg_id": np.int64, "price": np.float64, "qty": np.float64, "first_id": np.int64,
                                "last_id": np.int64, "ts": np.int64})
    for ch in reader:
        aid, fid, lid = ch.agg_id.to_numpy(), ch.first_id.to_numpy(), ch.last_id.to_numpy()
        ts = ch.ts.to_numpy()
        ts = np.where(ts > 10**14, ts // 1000, ts)  # microseconds (2025+) -> milliseconds
        price, qty = ch.price.to_numpy(), ch.qty.to_numpy()
        prev_aid = np.concatenate([[state["last_agg_id"]], aid[:-1]]) if state["last_agg_id"] is not None else None
        prev_lid = np.concatenate([[state["last_trade_id"]], lid[:-1]]) if state["last_trade_id"] is not None else None
        if prev_aid is not None:
            gap = aid - prev_aid - 1
        else:
            gap = np.concatenate([[0], np.diff(aid) - 1])
        tgap = (fid - prev_lid - 1) if prev_lid is not None else np.concatenate([[0], fid[1:] - lid[:-1] - 1])
        state["agg_id_gaps"] += int((gap != 0).sum())
        state["agg_ids_missing"] += int(gap[gap > 0].sum())
        state["trade_id_gaps"] += int((tgap != 0).sum())
        state["trade_ids_missing"] += int(tgap[tgap > 0].sum())
        prev_ts = np.concatenate([[state["last_ts"]], ts[:-1]]) if state["last_ts"] is not None else np.concatenate([[ts[0]], ts[:-1]])
        state["ts_decreasing"] += int((ts < prev_ts).sum())
        bad = ~((price > 0) & (qty > 0) & (lid >= fid))
        state["invalid_rows"] += int(bad.sum())
        if state["first_ts"] is None:
            state["first_ts"] = int(ts[0])
        state["last_agg_id"], state["last_trade_id"], state["last_ts"] = int(aid[-1]), int(lid[-1]), int(ts[-1])
        state["rows"] += len(ch)
        state["trades"] += int((lid - fid + 1).sum())
        ok = ~bad
        ts, price, qty = ts[ok], price[ok], qty[ok]
        parts_bins.append(_group_bins(ts // BUCKET_MS * BUCKET_MS, np.floor(price / base_bin + 1e-9).astype(np.int64), qty))
        m = pd.DataFrame({"m": ts // 60_000, "p": price, "q": qty}).groupby("m", sort=True)
        parts_min.append(pd.DataFrame({"volume": m.q.sum(), "high": m.p.max(), "low": m.p.min(), "first": m.p.first(),
                                       "last": m.p.last(), "n": m.p.size()}))
    b = np.concatenate([p[0] for p in parts_bins])
    k = np.concatenate([p[1] for p in parts_bins])
    w = np.concatenate([p[2] for p in parts_bins])
    mins = pd.concat(parts_min)
    g = mins.groupby(level=0, sort=True)
    mins = pd.DataFrame({"volume": g.volume.sum(), "high": g.high.max(), "low": g.low.min(), "first": g["first"].first(),
                         "last": g["last"].last(), "n": g.n.sum()})
    return _group_bins(b, k, w), mins


class _Prepend(io.RawIOBase):
    """Raw stream yielding `head` then the rest of `stream` (lets us sniff the first line for a header)."""

    def __init__(self, head: bytes, stream):
        self.head, self.stream = head, stream

    def readable(self):
        return True

    def readinto(self, b):
        if self.head:
            n = min(len(b), len(self.head))
            b[:n], self.head = self.head[:n], self.head[n:]
            return n
        data = self.stream.read(len(b))
        b[: len(data)] = data
        return len(data)


def cmd_btc(a) -> dict:
    months = list(pd.period_range(a.first, a.last, freq="M").strftime("%Y-%m"))
    state = {k: 0 for k in ("agg_id_gaps", "agg_ids_missing", "trade_id_gaps", "trade_ids_missing", "ts_decreasing",
                            "invalid_rows", "rows", "trades")}
    state.update(last_agg_id=None, last_trade_id=None, last_ts=None, first_ts=None)
    files, B, K, W, MIN = [], [], [], [], []
    for month in months:
        name = f"{a.symbol}-aggTrades-{month}.zip"
        url = f"{AGG_BASE}/{a.symbol}/{name}"
        t0 = time.time()
        blob = _get(url)
        published = _get(url + ".CHECKSUM").decode().split()[0]
        actual = hashlib.sha256(blob).hexdigest()
        if actual != published:
            raise ValueError(f"{name}: checksum mismatch")
        before = dict(state)
        with zipfile.ZipFile(io.BytesIO(blob)) as z, z.open(name.replace(".zip", ".csv")) as stream:
            (b, k, w), mins = process_aggtrades_csv(stream, a.base_bin, state)
        del blob
        B.append(b), K.append(k), W.append(w), MIN.append(mins)
        files.append({"file": name, "sha256": actual, "rows": state["rows"] - before["rows"],
                      "agg_id_gaps": state["agg_id_gaps"] - before["agg_id_gaps"],
                      "trade_id_gaps": state["trade_id_gaps"] - before["trade_id_gaps"]})
        print(month, files[-1]["rows"], f"{time.time() - t0:.0f}s", flush=True)
    b, k, w = _group_bins(np.concatenate(B), np.concatenate(K), np.concatenate(W))
    mins = pd.concat(MIN)
    if not mins.index.is_unique:
        raise ValueError("duplicate minutes across months")
    out = Path(a.output)
    mpath = out.with_name(out.name + "_minutes.npz")
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(mpath, minute=mins.index.to_numpy(np.int64), **{c: mins[c].to_numpy() for c in mins.columns})
    integrity = {k2: v for k2, v in state.items() if k2 not in ("last_agg_id", "last_trade_id", "last_ts")}
    manifest = {"schema_version": 1, "market": "BTCUSDT", "source": "data.binance.vision monthly aggTrades (public, no credentials)",
                "weight_kind": "traded_base_qty", "weight_definition": WEIGHT_KINDS["traded_base_qty"],
                "base_bin": a.base_bin, "months": months, "files": files, "integrity": integrity,
                "minutes_file": mpath.name, "minutes_sha256": _sha_file(mpath),
                "span_utc": [datetime.fromtimestamp(state["first_ts"] / 1000, timezone.utc).isoformat(),
                             datetime.fromtimestamp(state["last_ts"] / 1000, timezone.utc).isoformat()],
                "retrieved_at": datetime.now(timezone.utc).isoformat()}
    m = save_bins(out, b, k, w, manifest)
    return {"rows": m["rows"], "trades": integrity["trades"], "agg_rows": integrity["rows"],
            "agg_id_gaps": integrity["agg_id_gaps"], "trade_id_gaps": integrity["trade_id_gaps"], "sha256": m["sha256"]}


def validate_btc_minutes(minutes: dict, klines: pd.DataFrame, start_ms: int, end_ms: int) -> dict:
    """Cross-check per-minute aggregate-trade sums against official 1m klines over [start, end)."""
    mm = pd.DataFrame({k: v for k, v in minutes.items() if k != "minute"}, index=pd.to_datetime(minutes["minute"] * 60_000, unit="ms", utc=True))
    kl = klines[(klines.index >= pd.Timestamp(start_ms, unit="ms", tz="UTC")) & (klines.index < pd.Timestamp(end_ms, unit="ms", tz="UTC"))]
    mm = mm[(mm.index >= kl.index[0]) & (mm.index <= kl.index[-1])]
    both = kl.join(mm, how="outer", lsuffix="_k")
    only_k = both["volume"].isna() & both["volume_k"].notna() & (both["volume_k"] > 0)
    only_t = both["volume_k"].isna() & both["volume"].notna()
    j = both.dropna(subset=["volume", "volume_k"])
    rel = (j["volume"] - j["volume_k"]).abs() / j["volume_k"].where(j["volume_k"] > 0, 1)
    return {"minutes_compared": int(len(j)), "kline_minutes_with_volume_but_no_trades": int(only_k.sum()),
            "trade_minutes_missing_from_klines": int(only_t.sum()),
            "volume_match_1e-6_pct": round(float((rel < 1e-6).mean() * 100), 4),
            "volume_rel_diff_max": float(rel.max()) if len(rel) else None,
            "total_volume_trades": float(j["volume"].sum()), "total_volume_klines": float(j["volume_k"].sum()),
            "high_match_pct": round(float((np.isclose(j["high"], j["high_k"], rtol=0, atol=1e-8)).mean() * 100), 4),
            "low_match_pct": round(float((np.isclose(j["low"], j["low_k"], rtol=0, atol=1e-8)).mean() * 100), 4),
            "open_match_pct": round(float((np.isclose(j["first"], j["open"], rtol=0, atol=1e-8)).mean() * 100), 4),
            "close_match_pct": round(float((np.isclose(j["last"], j["close"], rtol=0, atol=1e-8)).mean() * 100), 4)}


def cmd_btc_validate(a) -> dict:
    prefix = Path(a.bins)
    man = json.loads(prefix.with_suffix(".json").read_text())
    if _sha_file(prefix.with_suffix(".npz")) != man["sha256"]:
        raise ValueError("bins checksum mismatch")
    mpath = prefix.with_name(man["minutes_file"])
    if _sha_file(mpath) != man["minutes_sha256"]:
        raise ValueError("minutes checksum mismatch")
    from research.phase2e_btc_history import load_rows
    load_rows(Path(a.klines))  # verifies kline checksums/gaps
    from trading.mtf.frames import from_csv
    kl = from_csv(a.klines, time_col="timestamps", columns={k: k for k in ("open", "high", "low", "close", "volume")})
    kl = kl.rename(columns={"volume": "volume_k", "high": "high_k", "low": "low_k"})
    mins = dict(np.load(mpath))
    s = pd.Timestamp(man["months"][0] + "-01", tz="UTC").value // 1_000_000
    e = (pd.Timestamp(man["months"][-1] + "-01", tz="UTC") + pd.DateOffset(months=1)).value // 1_000_000
    res = {"bins": str(prefix), "integrity": man["integrity"], "klines_cross_check": validate_btc_minutes(mins, kl, s, e)}
    res["passed"] = bool(man["integrity"]["agg_id_gaps"] == 0 and man["integrity"]["invalid_rows"] == 0
                         and man["integrity"]["ts_decreasing"] == 0
                         and res["klines_cross_check"]["volume_match_1e-6_pct"] >= 99.9)
    prefix.with_name(prefix.name + ".validation.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    return res


# ------------------------------------------------------------------------------------------ MT5 XAUUSD

def utc_ticks(conn, name: str, start: datetime, end: datetime):
    """Read-only quote ticks in [start, end) UTC -> (utc_ms, bid, ask, server_ms), invalid quotes removed.
    Same conversion and filters as research.xau_tick_history._chunk_bars."""
    from trading.connectors.mt5 import _tick_rows
    mt5 = conn._require()
    s_srv = conn.server_tz.to_server(start).replace(tzinfo=timezone.utc)
    e_srv = conn.server_tz.to_server(end).replace(tzinfo=timezone.utc)
    rows = _tick_rows(mt5, name, s_srv, e_srv)
    if rows is None:
        raise RuntimeError(f"copy_ticks_range failed: {mt5.last_error()}")
    empty = (np.zeros(0, np.int64), np.zeros(0), np.zeros(0), np.zeros(0, np.int64))
    if len(rows) == 0:
        return empty
    off_a, off_b = conn.server_tz.offset_at(start), conn.server_tz.offset_at(end)
    msc = rows["time_msc"].astype(np.int64)
    if off_a == off_b:
        utc_ms = msc - int(off_a / timedelta(milliseconds=1))
    else:
        utc_ms = np.array([int(conn.server_tz.server_msc_to_utc(int(m)).timestamp() * 1000) for m in msc], dtype=np.int64)
    bid, ask = rows["bid"].astype(float), rows["ask"].astype(float)
    ok = (bid > 0) & (ask >= bid) & (utc_ms >= int(start.timestamp() * 1000)) & (utc_ms < int(end.timestamp() * 1000))
    return utc_ms[ok], bid[ok], ask[ok], msc[ok]


def tick_bars(utc_ms, bid, ask, srv_ms) -> pd.DataFrame:
    """Bid-OHLC 1m bars, tick count and minimum spread (points of 0.01) - the validated 2019-2020 construction."""
    spread = np.rint((ask - bid) / 0.01).astype(np.int64)
    df = pd.DataFrame({"m": utc_ms // 60_000, "bid": bid, "spread": spread, "srv": srv_ms // 60_000 * 60})
    g = df.groupby("m", sort=True)
    out = pd.DataFrame({"open": g.bid.first(), "high": g.bid.max(), "low": g.bid.min(), "close": g.bid.last(),
                        "tick_volume": g.bid.size(), "spread_points": g.spread.min(), "server_time": g.srv.first()})
    out.index = pd.to_datetime(out.index * 60_000, unit="ms", utc=True)
    return out


def cmd_xau(a) -> dict:
    from research.xau_tick_history import MAX_SPREAD_PTS, _connect, _percentiles, _write
    start = datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc)
    hist = np.zeros((24, MAX_SPREAD_PTS), dtype=np.int64)
    conn = _connect()
    B, K, W, bars, ticks = [], [], [], [], {}
    try:
        name = conn.discover_xauusd()
        cursor = start
        while cursor < end:
            nxt = min(cursor + timedelta(hours=6), end)
            utc_ms, bid, ask, srv = utc_ticks(conn, name, cursor, nxt)
            if len(bid):
                b, k, w = _group_bins(utc_ms // BUCKET_MS * BUCKET_MS, np.floor(bid / a.base_bin + 1e-9).astype(np.int64),
                                      np.ones(len(bid)))
                B.append(b), K.append(k), W.append(w)
                ticks[cursor.strftime("%Y-%m")] = ticks.get(cursor.strftime("%Y-%m"), 0) + len(bid)
                if a.bars_output:
                    bars.append(tick_bars(utc_ms, bid, ask, srv))
                    sp = np.rint((ask - bid) / 0.01).astype(np.int64)
                    hours = pd.DatetimeIndex(pd.to_datetime(utc_ms, unit="ms", utc=True)).tz_convert("America/New_York").hour
                    np.add.at(hist, (np.asarray(hours), np.clip(sp, 0, MAX_SPREAD_PTS - 1)), 1)
            if cursor.day == 1 and cursor.hour == 0:
                print(cursor.date(), sum(ticks.values()), flush=True)
            cursor = nxt
    finally:
        conn.shutdown()
    if not B:
        raise SystemExit("broker returned no ticks for the requested span")
    b, k, w = _group_bins(np.concatenate(B), np.concatenate(K), np.concatenate(W))
    manifest = {"schema_version": 1, "market": "XAUUSD", "broker_symbol": name,
                "source": "mt5_demo ticks (copy_ticks_range, COPY_TICKS_INFO), read-only connector",
                "weight_kind": "quote_ticks", "weight_definition": WEIGHT_KINDS["quote_ticks"], "price": "bid",
                "base_bin": a.base_bin, "requested": [a.start, a.end], "ticks": int(sum(ticks.values())),
                "ticks_by_month": ticks, "retrieved_at": datetime.now(timezone.utc).isoformat()}
    out = {"bins": save_bins(Path(a.output), b, k, w, manifest)["sha256"]}
    if a.bars_output:
        allbars = pd.concat(bars)
        if not allbars.index.is_unique:
            raise ValueError("duplicate minutes across chunks")
        p = Path(a.bars_output)
        digest = _write(allbars, p)
        bm = {"schema_version": 1, "source": "mt5_demo ticks (copy_ticks_range, COPY_TICKS_INFO), read-only",
              "broker_symbol": name, "interval": "1m", "construction": "bid OHLC; tick_volume = ticks; spread_points = min spread",
              "server_tz": "eet_eu_dst_utc+2/+3", "rows": int(len(allbars)), "ticks": int(sum(ticks.values())),
              "ticks_by_month": ticks, "sha256": digest, "start": allbars.index[0].isoformat(),
              "end": (allbars.index[-1] + timedelta(minutes=1)).isoformat(), "requested": [a.start, a.end],
              "retrieved_at": datetime.now(timezone.utc).isoformat(), "spread_by_new_york_hour_p50_p90": _percentiles(hist)}
        p.with_suffix(p.suffix + ".json").write_text(json.dumps(bm, indent=2), encoding="utf-8")
        out["bars"] = digest
    return out | {"ticks": int(sum(ticks.values())), "rows": int(len(w))}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("btc")
    b.add_argument("--symbol", default="BTCUSDT")
    b.add_argument("--first", required=True)
    b.add_argument("--last", required=True)
    b.add_argument("--base-bin", type=float, required=True)
    b.add_argument("--output", required=True)
    v = sub.add_parser("btc-validate")
    v.add_argument("--bins", required=True)
    v.add_argument("--klines", required=True)
    x = sub.add_parser("xau")
    x.add_argument("--start", required=True)
    x.add_argument("--end", required=True)
    x.add_argument("--base-bin", type=float, required=True)
    x.add_argument("--output", required=True)
    x.add_argument("--bars-output")
    a = p.parse_args()
    print(json.dumps({"btc": cmd_btc, "btc-validate": cmd_btc_validate, "xau": cmd_xau}[a.cmd](a), indent=2, default=str))


if __name__ == "__main__":
    main()
