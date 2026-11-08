"""Phase 2E — BTCUSDT history tools (read-only public market data; research only).

  download  Gap-tolerant download of closed Binance spot klines. Binance had real exchange outages (e.g.
            2019-03, 2019-05, 2020-02, 2021-04, 2023-03), so a long history cannot be gap-free. Every gap is
            recorded in the manifest; nothing is interpolated.
  kronos    Leakage-safe one-step Kronos-mini forecasts (each target predicted only from the previous
            `lookback` closed candles, same settings as KR-05), resumable JSONL output.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from research.market_data import FIELDS, INTERVAL_MS, DataValidationError, fetch_page, normalize, parse_time, save_csv


def download(symbol: str, interval: str, start_ms: int, end_ms: int, output: Path, *, fetcher=fetch_page,
             now_ms: int | None = None) -> dict:
    step = INTERVAL_MS[interval]
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    stop = min(end_ms, now_ms - now_ms % step)
    if start_ms % step or stop % step or start_ms >= stop:
        raise ValueError("Range must be whole candles")
    rows, gaps, cursor = [], [], start_ms
    while cursor < stop:
        page = fetcher(symbol, interval, cursor, stop)
        if not page:
            gaps.append({"from": cursor, "to": stop, "reason": "no data returned"})
            break
        for raw in page:
            ts = int(raw[0])
            if ts < cursor or ts >= stop:
                raise DataValidationError("API returned candles outside the requested page")
            expected = parse_time(rows[-1]["timestamps"]) + step if rows else start_ms
            if ts < expected:
                raise DataValidationError(f"Duplicate or out-of-order candle at {ts}")
            if ts > expected:
                gaps.append({"from": expected, "to": ts, "missing_candles": (ts - expected) // step})
            rows.append(normalize(raw, step, now_ms))
        cursor = int(page[-1][0]) + step
    if not rows:
        raise DataValidationError("No candles returned")
    save_csv(rows, output)
    manifest = {"schema_version": 1, "source": "binance_spot", "endpoint": "data-api.binance.vision/api/v3/klines",
                "symbol": symbol, "interval": interval, "start_ms": start_ms, "end_ms": parse_time(rows[-1]["timestamps"])
                + step, "row_count": len(rows), "gaps": gaps,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "retrieved_at": datetime.now(timezone.utc).isoformat()}
    output.with_suffix(output.suffix + ".json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {k: manifest[k] for k in ("row_count", "sha256", "start_ms", "end_ms")} | {"gaps": len(gaps)}


def load_rows(path: Path) -> tuple[list[dict], dict]:
    """Rows of a gap-tolerant dataset, checksum-verified; strictly increasing, gaps only where recorded."""
    manifest = json.loads(path.with_suffix(path.suffix + ".json").read_text(encoding="utf-8"))
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["sha256"]:
        raise DataValidationError(f"Checksum mismatch for {path}")
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    step = INTERVAL_MS[manifest["interval"]]
    recorded = {(g["from"], g["to"]) for g in manifest.get("gaps", [])}
    for prev, row in zip(rows, rows[1:]):
        a, b = parse_time(prev["timestamps"]), parse_time(row["timestamps"])
        if b <= a or (b - a != step and (a + step, b) not in recorded):
            raise DataValidationError(f"Unrecorded gap or disorder at {row['timestamps']}")
    if rows and tuple(rows[0]) != FIELDS:
        raise DataValidationError("Unexpected schema")
    return rows, manifest


def kronos_forecasts(rows: list[dict], output: Path, *, first_target: str, last_target_exclusive: str,
                     lookback: int = 128, predictor=None, progress_every: int = 500) -> dict:
    """Append one-step forecasts for targets in [first_target, last_target_exclusive) to `output` (JSONL).
    Resumable: targets already present are skipped. Inputs are only rows strictly before each target."""
    import numpy as np
    import pandas as pd
    done = set()
    if output.exists():
        done = {json.loads(line)["timestamp"] for line in output.read_text(encoding="utf-8").splitlines() if line}
    frame = pd.DataFrame(rows)
    frame["timestamps"] = pd.to_datetime(frame["timestamps"], utc=True)
    for field in ("open", "high", "low", "close"):
        frame[field] = pd.to_numeric(frame[field], errors="raise")
    lo, hi = pd.Timestamp(first_target), pd.Timestamp(last_target_exclusive)
    targets = [t for t in range(lookback, len(frame)) if lo <= frame["timestamps"].iloc[t] < hi
               and frame["timestamps"].iloc[t].isoformat() not in done]
    if predictor is None:
        import torch
        from model import Kronos, KronosPredictor, KronosTokenizer
        random.seed(123)
        np.random.seed(123)
        torch.manual_seed(123)
        tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-2k")
        model = Kronos.from_pretrained("NeoQuasar/Kronos-mini")
        model.eval()
        tokenizer.eval()
        predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=512)
    output.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with output.open("a", encoding="utf-8") as stream:
        for t in targets:
            context = frame.iloc[t - lookback:t]
            predicted = predictor.predict(
                df=context[["open", "high", "low", "close"]].reset_index(drop=True),
                x_timestamp=context["timestamps"].reset_index(drop=True),
                y_timestamp=frame["timestamps"].iloc[t:t + 1].reset_index(drop=True),
                pred_len=1, T=1.0, top_k=1, top_p=1.0, sample_count=1, verbose=False)
            value = float(predicted["close"].iloc[0])
            if not np.isfinite(value) or value <= 0:
                raise ValueError("Model returned an invalid forecast")
            stream.write(json.dumps({"timestamp": frame["timestamps"].iloc[t].isoformat(),
                                     "previous_close": float(context["close"].iloc[-1]),
                                     "predicted_close": value}) + "\n")
            written += 1
            if written % progress_every == 0:
                stream.flush()
                print(f"{written}/{len(targets)} forecasts", flush=True)
    return {"written": written, "already_done": len(done), "targets": len(targets)}


def load_forecasts(path: Path) -> dict[datetime, float]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line:
            item = json.loads(line)
            out[datetime.fromisoformat(item["timestamp"])] = item["predicted_close"]
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("download")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("kronos")
    p.add_argument("--dataset", type=Path, nargs="+", required=True, help="one or more gap-tolerant datasets, in order")
    p.add_argument("--first-target", required=True)
    p.add_argument("--last-target-exclusive", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "download":
        print(download("BTCUSDT", "1h", parse_time(args.start), parse_time(args.end), args.output))
    else:
        rows = []
        for path in args.dataset:
            rows += load_rows(path)[0]
        print(kronos_forecasts(rows, args.output, first_target=args.first_target,
                               last_target_exclusive=args.last_target_exclusive))


if __name__ == "__main__":
    main()
