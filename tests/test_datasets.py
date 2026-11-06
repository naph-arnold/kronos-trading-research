"""Offline checks for integrity manifests and gap-safe incremental fetching."""
import json

import pytest

from research.datasets import load_verified, update_dataset
from research.market_data import DataValidationError, INTERVAL_MS, parse_time

START = parse_time("2026-01-01T00:00:00Z")
STEP = INTERVAL_MS["1h"]


def candle(ts):
    return [ts, "100", "110", "90", "105", "20", ts + STEP - 1, "2000", 0, "0", "0", "0"]


def test_create_then_incrementally_append(tmp_path):
    output = tmp_path / "BTC.csv"
    calls = []

    def fetcher(symbol, interval, start, end):
        calls.append(start)
        return [candle(ts) for ts in range(start, end, STEP)]

    first = update_dataset(output, symbol="BTCUSDT", interval="1h",
                           start_ms=START, end_ms=START + 2 * STEP,
                           now_ms=START + 4 * STEP, fetcher=fetcher)
    assert first["new_rows"] == 2
    assert len(first["sha256"]) == 64
    second = update_dataset(output, symbol="BTCUSDT", interval="1h",
                            start_ms=START, end_ms=START + 4 * STEP,
                            now_ms=START + 5 * STEP, fetcher=fetcher)
    assert second["new_rows"] == 2
    assert second["row_count"] == 4
    assert calls == [START, START + 2 * STEP]
    assert len(load_verified(output, symbol="BTCUSDT", interval="1h", start_ms=START)) == 4
    third = update_dataset(output, symbol="BTCUSDT", interval="1h",
                           start_ms=START, end_ms=START + 4 * STEP,
                           now_ms=START + 5 * STEP, fetcher=fetcher)
    assert third["new_rows"] == 0
    assert len(calls) == 2


def test_detect_csv_tamper(tmp_path):
    output = tmp_path / "BTC.csv"
    update_dataset(output, symbol="BTCUSDT", interval="1h",
                   start_ms=START, end_ms=START + STEP,
                   now_ms=START + 2 * STEP, fetcher=lambda *args: [candle(START)])
    output.write_text(output.read_text().replace("105", "106"), encoding="utf-8")
    with pytest.raises(DataValidationError, match="SHA-256"):
        load_verified(output, symbol="BTCUSDT", interval="1h", start_ms=START)


def test_detect_missing_manifest(tmp_path):
    output = tmp_path / "BTC.csv"
    output.write_text("timestamps,open,high,low,close,volume,amount\n", encoding="utf-8")
    with pytest.raises(DataValidationError, match="together"):
        load_verified(output, symbol="BTCUSDT", interval="1h", start_ms=START)


def test_prevent_truncation(tmp_path):
    output = tmp_path / "BTC.csv"
    update_dataset(output, symbol="BTCUSDT", interval="1h",
                   start_ms=START, end_ms=START + 2 * STEP,
                   now_ms=START + 3 * STEP,
                   fetcher=lambda *args: [candle(START), candle(START + STEP)])
    with pytest.raises(DataValidationError, match="truncation"):
        update_dataset(output, symbol="BTCUSDT", interval="1h",
                       start_ms=START, end_ms=START + STEP,
                       now_ms=START + 3 * STEP, fetcher=lambda *args: [])


def test_reject_incorrect_symbol_and_metadata(tmp_path):
    output = tmp_path / "BTC.csv"
    update_dataset(output, symbol="BTCUSDT", interval="1h",
                   start_ms=START, end_ms=START + STEP,
                   now_ms=START + 2 * STEP,
                   fetcher=lambda *args: [candle(START)])
    with pytest.raises(DataValidationError, match="metadata"):
        load_verified(output, symbol="ETHUSDT", interval="1h", start_ms=START)
    manifest = output.with_suffix(".csv.json")
    data = json.loads(manifest.read_text())
    data["row_count"] = 999
    manifest.write_text(json.dumps(data))
    with pytest.raises(DataValidationError, match="row count"):
        load_verified(output, symbol="BTCUSDT", interval="1h", start_ms=START)
