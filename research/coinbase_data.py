"""Public Coinbase Exchange OHLCV adapter; strictly research-only.

Exchange candles are [epoch_seconds, low, high, open, close, base_volume],
usually reverse-chronological. Coinbase does not supply quote turnover; the
Kronos 'amount' column is recorded as 0 (unknown), never manufactured.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from research.market_data import DataValidationError, INTERVAL_MS

BASE_URL = "https://api.exchange.coinbase.com"
PAIRS = {"BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD"}
GRANULARITY = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


def convert_candles(data: list, start_ms: int, end_ms: int, step: int) -> list:
    """Map Coinbase candles to normalized Binance-shaped records."""
    if not isinstance(data, list):
        raise DataValidationError("Unexpected Coinbase response")
    records = []
    for item in data:
        if not isinstance(item, list) or len(item) < 6:
            raise DataValidationError("Invalid Coinbase candle")
        ts = int(item[0]) * 1000
        if start_ms <= ts < end_ms:
            low, high, opened, closed, volume = item[1:6]
            records.append([ts, opened, high, low, closed, volume, ts + step - 1, "0"])
    records.sort(key=lambda row: row[0])
    if len(set(row[0] for row in records)) != len(records):
        raise DataValidationError("Coinbase returned duplicate candles")
    return records


def fetch_page(symbol: str, interval: str, start_ms: int, end_ms: int) -> list:
    if symbol not in PAIRS or interval not in GRANULARITY:
        raise ValueError("Unsupported Coinbase pair or granularity")
    step = INTERVAL_MS[interval]
    # Coinbase Exchange public candles are limited to 300 buckets per query.
    page_end = min(end_ms, start_ms + 299 * step)
    params = urlencode({
        "start": datetime.fromtimestamp(start_ms / 1000, timezone.utc).isoformat(),
        "end": datetime.fromtimestamp(page_end / 1000, timezone.utc).isoformat(),
        "granularity": GRANULARITY[interval],
    })
    url = f"{BASE_URL}/products/{PAIRS[symbol]}/candles?{params}"
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"User-Agent": "kronos-trading-research/1.0"}), timeout=20) as response:
                data = json.load(response)
            return convert_candles(data, start_ms, page_end, step)
        except (HTTPError, URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")
