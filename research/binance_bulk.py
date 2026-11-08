"""Official Binance public bulk data (data.binance.vision) -> verified kline CSV (research only, read-only).

Each monthly archive is verified against Binance's published `.CHECKSUM` (SHA-256) before use. Timestamps are
normalized to UTC (the archives switched from milliseconds to microseconds in 2025). Output matches the format of
`research.phase2e_btc_history` (gaps recorded in the manifest, never filled), so `load_rows` verifies it.

    python -m research.binance_bulk --interval 1m --first 2023-01 --last 2026-05 --output data/raw/x.csv
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

import pandas as pd

from research.market_data import FIELDS, INTERVAL_MS, save_csv

BASE = "https://data.binance.vision/data/spot/monthly/klines"


def _get(url: str) -> bytes:
    with urlopen(url, timeout=120) as response:
        return response.read()


def month_rows(symbol: str, interval: str, month: str, fetch=_get, *, drop_misaligned: bool = False) -> tuple[list[dict], dict]:
    name = f"{symbol}-{interval}-{month}.zip"
    url = f"{BASE}/{symbol}/{interval}/{name}"
    blob = fetch(url)
    published = fetch(url + ".CHECKSUM").decode().split()[0]
    actual = hashlib.sha256(blob).hexdigest()
    if actual != published:
        raise ValueError(f"{name}: checksum mismatch")
    rows, dropped = [], []
    for line in zipfile.ZipFile(io.BytesIO(blob)).read(name.replace(".zip", ".csv")).decode().splitlines():
        parts = line.split(",")
        if not parts[0].isdigit():
            continue  # header (some archives carry one)
        ts = int(parts[0])
        ts_ms = ts // 1000 if ts > 10**14 else ts  # microseconds (2025+) -> milliseconds
        if ts_ms % INTERVAL_MS[interval]:
            # Binance's 2017-12-04..18 klines are stamped hh:mm:20.799 after an exchange outage. Optionally
            # excluded (count and volume recorded) for cross-check use; otherwise fail closed.
            if drop_misaligned:
                dropped.append((ts_ms, float(parts[5])))
                continue
            raise ValueError(f"{name}: misaligned timestamp {ts}")
        rows.append({"timestamps": datetime.fromtimestamp(ts_ms / 1000, timezone.utc).isoformat(), "open": parts[1],
                     "high": parts[2], "low": parts[3], "close": parts[4], "volume": parts[5], "amount": parts[7]})
    meta = {"file": name, "sha256": actual}
    if dropped:
        meta["dropped_misaligned"] = {"count": len(dropped), "volume": round(sum(v for _, v in dropped), 8),
                                      "first_ms": dropped[0][0], "last_ms": dropped[-1][0]}
    return rows, meta


def download(symbol: str, interval: str, first: str, last: str, output: Path, fetch=_get, *,
             drop_misaligned: bool = False) -> dict:
    step = INTERVAL_MS[interval]
    rows, files, gaps = [], [], []
    for month in pd.period_range(first, last, freq="M").strftime("%Y-%m"):
        part, meta = month_rows(symbol, interval, month, fetch, drop_misaligned=drop_misaligned)
        rows += part
        files.append(meta)
    rows.sort(key=lambda r: r["timestamps"])
    times = [int(datetime.fromisoformat(r["timestamps"]).timestamp() * 1000) for r in rows]
    for a, b in zip(times, times[1:]):
        if b <= a:
            raise ValueError("duplicate or out-of-order candle")
        if b - a != step:
            gaps.append({"from": a + step, "to": b, "missing_candles": (b - a) // step - 1})
    for r in rows:
        if tuple(r) != FIELDS:
            raise ValueError("unexpected field order")
    save_csv(rows, output)
    manifest = {"schema_version": 1, "source": "binance_spot", "endpoint": "data.binance.vision monthly klines",
                "symbol": symbol, "interval": interval, "start_ms": times[0], "end_ms": times[-1] + step,
                "row_count": len(rows), "gaps": gaps, "files": files,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "retrieved_at": datetime.now(timezone.utc).isoformat()}
    output.with_suffix(output.suffix + ".json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {k: manifest[k] for k in ("row_count", "start_ms", "end_ms", "sha256")} | {"gaps": len(gaps), "files": len(files)}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--interval", required=True)
    p.add_argument("--first", required=True)
    p.add_argument("--last", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--drop-misaligned", action="store_true",
                   help="exclude candles at non-aligned times (recorded in the manifest); for cross-checks only")
    a = p.parse_args()
    print(download(a.symbol, a.interval, a.first, a.last, a.output, drop_misaligned=a.drop_misaligned))


if __name__ == "__main__":
    main()
