"""Public, read-only Binance spot OHLCV ingestion. No trading endpoints or credentials."""
from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen
import time

INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
FIELDS = ("timestamps", "open", "high", "low", "close", "volume", "amount")
BASE_URL = "https://data-api.binance.vision"


class DataValidationError(ValueError):
    """A dataset is unsafe to use for backtesting."""


def parse_time(value: str) -> int:
    """Parse UTC ISO-8601 to milliseconds (reject naive datetimes)."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone (use +00:00 or Z)")
    return int(parsed.timestamp() * 1000)


def _decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DataValidationError(f"Invalid {name}: {value!r}") from exc
    if not result.is_finite():
        raise DataValidationError(f"Non-finite {name}")
    return result


def normalize(raw: list, interval_ms: int, now_ms: int) -> dict:
    if len(raw) < 8:
        raise DataValidationError("Kline has too few fields")
    ts = int(raw[0])
    if ts % interval_ms:
        raise DataValidationError(f"Misaligned UTC timestamp: {ts}")
    if ts + interval_ms > now_ms:
        raise DataValidationError(f"Unclosed candle at {ts}")
    opened, high, low, closed, volume, amount = [
        _decimal(value, label) for value, label in zip(raw[1:6] + [raw[7]], FIELDS[1:])
    ]
    if min(opened, high, low, closed) <= 0 or min(volume, amount) < 0:
        raise DataValidationError("Prices must be positive and volumes nonnegative")
    if high < max(opened, closed, low) or low > min(opened, closed, high):
        raise DataValidationError("Inconsistent OHLC prices")
    return dict(zip(FIELDS, [datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat(), *map(str, (opened, high, low, closed, volume, amount))]))


def validate_rows(rows: list[dict], interval_ms: int) -> None:
    if not rows:
        raise DataValidationError("No closed candles returned")
    previous = None
    for row in rows:
        if tuple(row) != FIELDS:
            raise DataValidationError("Unexpected CSV schema")
        ts = parse_time(row["timestamps"])
        if previous is not None and ts - previous != interval_ms:
            raise DataValidationError(f"Duplicate, out-of-order or missing candle between {previous} and {ts}")
        previous = ts


def fetch_page(symbol: str, interval: str, start_ms: int, end_ms: int, *, attempts: int = 3) -> list:
    params = urlencode({"symbol": symbol, "interval": interval, "startTime": start_ms, "endTime": end_ms - 1, "limit": 1000})
    url = f"{BASE_URL}/api/v3/klines?{params}"
    for attempt in range(attempts):
        try:
            with urlopen(url, timeout=20) as response:
                data = json.load(response)
            if not isinstance(data, list):
                raise DataValidationError("Unexpected API response")
            return data
        except (HTTPError, URLError, TimeoutError) as exc:
            if isinstance(exc, HTTPError) and exc.code not in (418, 429, 500, 502, 503, 504):
                raise
            if attempt == attempts - 1:
                raise
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError("Unreachable")


def collect(symbol: str, interval: str, start_ms: int, end_ms: int, *, now_ms: int | None = None, fetcher=fetch_page) -> list[dict]:
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")
    if not symbol.isascii() or not symbol.isalnum() or len(symbol) > 20:
        raise ValueError("Symbol must be a short ASCII alphanumeric pair")
    step = INTERVAL_MS[interval]
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    # Include only complete candles, even if the requested end is in the future.
    stop = min(end_ms, now_ms - (now_ms % step))
    if start_ms >= stop or start_ms % step or stop % step:
        raise ValueError("Start/end must be UTC candle boundaries and contain at least one closed candle")
    cursor = start_ms
    rows = []
    while cursor < stop:
        page = fetcher(symbol, interval, cursor, stop)
        if not page:
            raise DataValidationError(f"Missing candles starting at {cursor}")
        for raw in page:
            ts = int(raw[0])
            if ts < cursor or ts >= stop:
                raise DataValidationError("API returned candles outside requested page")
            rows.append(normalize(raw, step, now_ms))
        next_cursor = int(page[-1][0]) + step
        if next_cursor <= cursor:
            raise DataValidationError("Non-progressing API pagination")
        cursor = next_cursor
    validate_rows(rows, step)
    if parse_time(rows[0]["timestamps"]) != start_ms or parse_time(rows[-1]["timestamps"]) + step != stop:
        raise DataValidationError("Requested coverage incomplete")
    return rows


def save_csv(rows: list[dict], destination: Path) -> None:
    """Atomic replacement; interrupted downloads do not corrupt earlier output."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with staging.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, destination)
    finally:
        staging.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download closed UTC Binance spot candles (research only)")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", choices=INTERVAL_MS, default="1h")
    parser.add_argument("--start", required=True, help="UTC ISO timestamp with timezone (inclusive)")
    parser.add_argument("--end", required=True, help="UTC ISO timestamp with timezone (exclusive)")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = collect(args.symbol.upper(), args.interval, parse_time(args.start), parse_time(args.end))
    save_csv(rows, args.output)
    print(f"Saved {len(rows)} validated closed candles to {args.output}")


if __name__ == "__main__":
    main()
