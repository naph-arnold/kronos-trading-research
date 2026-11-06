"""Offline deterministic tests for public OHLCV collector."""
import csv
import pytest

from research.market_data import (
    DataValidationError, INTERVAL_MS, collect, normalize, parse_time, save_csv, validate_rows,
)


STEP = INTERVAL_MS["1h"]
START = parse_time("2026-01-01T00:00:00Z")


def candle(timestamp, *, high="110", low="90", close="105"):
    return [timestamp, "100", high, low, close, "20", timestamp + STEP - 1, "2000", 0, "0", "0", "0"]


def test_timezones_required_and_normalized():
    assert parse_time("2026-01-01T03:00:00+03:00") == START
    with pytest.raises(ValueError, match="timezone"):
        parse_time("2026-01-01T00:00:00")


def test_normalization_maps_amount_and_utc():
    row = normalize(candle(START), STEP, START + STEP)
    assert list(row) == ["timestamps", "open", "high", "low", "close", "volume", "amount"]
    assert row["timestamps"] == "2026-01-01T00:00:00+00:00"
    assert row["amount"] == "2000"


@pytest.mark.parametrize("bad", [
    candle(START, high="99"),
    candle(START, low="106"),
    candle(START, close="-1"),
    candle(START, close="NaN"),
])
def test_bad_ohlc_is_rejected(bad):
    with pytest.raises(DataValidationError):
        normalize(bad, STEP, START + STEP)


def test_unclosed_candle_is_rejected():
    with pytest.raises(DataValidationError, match="Unclosed"):
        normalize(candle(START), STEP, START + STEP - 1)


def test_paginated_collection_and_closed_boundary():
    calls = []

    def fetcher(symbol, interval, cursor, stop):
        calls.append((symbol, interval, cursor, stop))
        if cursor == START:
            return [candle(START), candle(START + STEP)]
        return [candle(START + 2 * STEP)]

    rows = collect("BTCUSDT", "1h", START, START + 3 * STEP, now_ms=START + 4 * STEP, fetcher=fetcher)
    assert len(rows) == 3
    assert len(calls) == 2
    assert calls[-1][2] == START + 2 * STEP


def test_incomplete_range_fails_closed():
    with pytest.raises(DataValidationError, match="coverage"):
        collect("BTCUSDT", "1h", START, START + 3 * STEP, now_ms=START + 4 * STEP,
                fetcher=lambda *args: [candle(START + STEP), candle(START + 2 * STEP)])


def test_missing_candles_are_not_filled():
    rows = [normalize(candle(START), STEP, START + 4 * STEP),
            normalize(candle(START + 2 * STEP), STEP, START + 4 * STEP)]
    with pytest.raises(DataValidationError, match="missing"):
        validate_rows(rows, STEP)


def test_duplicate_candles_fail():
    row = normalize(candle(START), STEP, START + STEP)
    with pytest.raises(DataValidationError, match="Duplicate"):
        validate_rows([row, row], STEP)


def test_empty_api_page_fails_closed():
    with pytest.raises(DataValidationError, match="Missing candles"):
        collect("BTCUSDT", "1h", START, START + STEP, now_ms=START + 2 * STEP,
                fetcher=lambda *args: [])


def test_end_at_last_complete_candle():
    rows = collect("BTCUSDT", "1h", START, START + 5 * STEP,
                   now_ms=START + STEP + STEP // 2,
                   fetcher=lambda *args: [candle(START)])
    assert len(rows) == 1


def test_invalid_inputs():
    with pytest.raises(ValueError, match="Symbol"):
        collect("BTC/USDT", "1h", START, START + STEP)
    with pytest.raises(ValueError, match="boundaries"):
        collect("BTCUSDT", "1h", START + 1, START + STEP)


def test_atomic_csv_output(tmp_path):
    rows = [normalize(candle(START), STEP, START + STEP)]
    output = tmp_path / "market" / "BTCUSDT.csv"
    save_csv(rows, output)
    with output.open(newline="", encoding="utf-8") as stream:
        actual = list(csv.DictReader(stream))
    assert actual == rows
    assert not output.with_suffix(".csv.tmp").exists()
