"""Coinbase Exchange candle adapter tests (fully offline)."""
import pytest

from research.coinbase_data import convert_candles
from research.market_data import DataValidationError, INTERVAL_MS, parse_time, collect

STEP = INTERVAL_MS["1h"]
START = parse_time("2026-01-01T00:00:00Z")


def exchange_candle(timestamp):
    return [timestamp // 1000, "90", "110", "100", "105", "20"]


def test_reverse_order_coinbase_candles_normalize():
    raw = [exchange_candle(START + STEP), exchange_candle(START)]
    normalized = convert_candles(raw, START, START + 2 * STEP, STEP)
    assert [item[0] for item in normalized] == [START, START + STEP]
    rows = collect("BTCUSD", "1h", START, START + 2 * STEP,
                   now_ms=START + 3 * STEP,
                   fetcher=lambda *args: normalized)
    assert len(rows) == 2
    assert rows[0]["amount"] == "0"
    assert rows[0]["close"] == "105"


def test_duplicate_from_exchange_fails():
    with pytest.raises(DataValidationError, match="duplicate"):
        convert_candles([exchange_candle(START)] * 2, START, START + STEP, STEP)


def test_outside_requested_window_is_excluded():
    rows = convert_candles([exchange_candle(START - STEP), exchange_candle(START),
                            exchange_candle(START + STEP)],
                           START, START + STEP, STEP)
    assert len(rows) == 1


def test_unexpected_coinbase_payload_fails():
    with pytest.raises(DataValidationError):
        convert_candles({"error": "blocked"}, START, START + STEP, STEP)
