"""Verified on-disk OHLCV datasets with incremental, gap-safe updates.

Data CSV and manifest are kept together. On every reuse, the SHA-256 and row
coverage are verified, preventing silent reuse of stale or corrupted candles.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path

from research.market_data import FIELDS, INTERVAL_MS, DataValidationError, collect, parse_time, save_csv


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(path: Path, record: dict) -> None:
    staged = path.with_suffix(path.suffix + ".tmp")
    try:
        with staged.open("w", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def load_verified(csv_path: Path, *, symbol: str, interval: str, start_ms: int, source: str = "binance_spot") -> list[dict]:
    manifest_path = csv_path.with_suffix(csv_path.suffix + ".json")
    if not csv_path.exists() and not manifest_path.exists():
        return []
    if not csv_path.exists() or not manifest_path.exists():
        raise DataValidationError("Dataset and manifest must exist together")
    try:
        metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
        with csv_path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != FIELDS:
                raise DataValidationError("Incorrect stored CSV schema")
            rows = list(reader)
    except (OSError, ValueError, csv.Error) as exc:
        raise DataValidationError("Stored dataset cannot be read") from exc
    if (metadata.get("schema_version") != 1
            or metadata.get("source") != source
            or metadata.get("symbol") != symbol
            or metadata.get("interval") != interval
            or metadata.get("start_ms") != start_ms):
        raise DataValidationError("Dataset metadata does not match request")
    if not rows or metadata.get("row_count") != len(rows):
        raise DataValidationError("Stored dataset row count mismatch")
    if metadata.get("sha256") != _checksum(csv_path):
        raise DataValidationError("Stored dataset SHA-256 mismatch")
    from research.market_data import validate_rows
    validate_rows(rows, INTERVAL_MS[interval])
    if parse_time(rows[0]["timestamps"]) != start_ms:
        raise DataValidationError("Stored dataset start mismatch")
    if metadata.get("end_ms") != parse_time(rows[-1]["timestamps"]) + INTERVAL_MS[interval]:
        raise DataValidationError("Stored dataset end mismatch")
    return rows


def update_dataset(
    csv_path: Path, *, symbol: str, interval: str, start_ms: int, end_ms: int,
    now_ms: int | None = None, fetcher=None, source: str = "binance_spot",
) -> dict:
    """Append only missing complete candles; stop rather than fill gaps.

    Existing full coverage is not re-fetched. If a write is interrupted,
    an unmatched CSV/manifest pair fails closed on the next attempt.
    """
    if interval not in INTERVAL_MS:
        raise ValueError("Unsupported interval")
    if source not in ("binance_spot", "coinbase_exchange"):
        raise ValueError("Unsupported data provider")
    if source == "coinbase_exchange" and interval not in ("1m", "5m", "15m", "1h", "1d"):
        raise ValueError("Unsupported Coinbase granularity")
    if source == "coinbase_exchange" and symbol not in ("BTCUSD", "ETHUSD"):
        raise ValueError("Unsupported Coinbase symbol")
    if fetcher is None and source == "coinbase_exchange":
        from research.coinbase_data import fetch_page as coinbase_fetch
        fetcher = coinbase_fetch
    if not symbol.isascii() or not symbol.isalnum() or len(symbol) > 20:
        raise ValueError("Invalid symbol")
    step = INTERVAL_MS[interval]
    if start_ms % step or end_ms % step or start_ms >= end_ms:
        raise ValueError("Requested range must contain whole UTC candles")
    existing = load_verified(csv_path, symbol=symbol, interval=interval, start_ms=start_ms, source=source)
    cursor = parse_time(existing[-1]["timestamps"]) + step if existing else start_ms
    if end_ms < cursor:
        raise DataValidationError("Requested end precedes stored coverage; refusing truncation")
    additions = []
    if end_ms > cursor:
        kwargs = {}
        if now_ms is not None:
            kwargs["now_ms"] = now_ms
        if fetcher is not None:
            kwargs["fetcher"] = fetcher
        additions = collect(symbol, interval, cursor, end_ms, **kwargs)
    combined = existing + additions
    if not combined:
        raise DataValidationError("No completed candles available to store")
    if additions or not csv_path.exists():
        save_csv(combined, csv_path)
        metadata = {
            "schema_version": 1,
            "source": source,
            "symbol": symbol,
            "interval": interval,
            "start_ms": start_ms,
            "end_ms": parse_time(combined[-1]["timestamps"]) + step,
            "row_count": len(combined),
            "sha256": _checksum(csv_path),
        }
        _write_manifest(csv_path.with_suffix(csv_path.suffix + ".json"), metadata)
    else:
        metadata = json.loads(csv_path.with_suffix(csv_path.suffix + ".json").read_text(encoding="utf-8"))
    return {"new_rows": len(additions), **metadata}


def main() -> None:
    parser = argparse.ArgumentParser(description="Incrementally update verified spot OHLCV CSV")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--source", choices=("binance_spot", "coinbase_exchange"), default="binance_spot")
    parser.add_argument("--interval", choices=INTERVAL_MS, default="1h")
    parser.add_argument("--start", required=True, help="UTC ISO 8601 inclusive boundary")
    parser.add_argument("--end", required=True, help="UTC ISO 8601 exclusive boundary")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(update_dataset(
        args.output, symbol=args.symbol, interval=args.interval,
        start_ms=parse_time(args.start), end_ms=parse_time(args.end), source=args.source,
    ), indent=2))


if __name__ == "__main__":
    main()
