"""Offline tests for a read-only Grok-compatible forecast JSON interface."""
import http.client
import json
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

import pandas as pd
import pytest

from research.datasets import update_dataset
from research.forecast_http import make_handler
from research.forecast_service import ForecastService
from research.market_data import DataValidationError, INTERVAL_MS


STEP = INTERVAL_MS["1h"]


class StubPredictor:
    def __init__(self):
        self.last_y = None

    def predict(self, **kwargs):
        x = kwargs["x_timestamp"]
        y = kwargs["y_timestamp"]
        assert x.max() < y.min()
        assert len(kwargs["df"]) == 16
        self.last_y = y
        return pd.DataFrame([{x: 101.0 for x in ("open", "high", "low", "close")}
                             for _ in range(kwargs["pred_len"])])


def build_dataset(tmp_path, *, stale=False):
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    boundary = now // STEP * STEP
    end = boundary - 4 * STEP if stale else boundary
    begin = end - 24 * STEP

    def candles(symbol, interval, cursor, stop):
        return [[ts, "100", "102", "99", "101", "5", ts + STEP - 1, "505"]
                for ts in range(cursor, stop, STEP)]

    path = tmp_path / "BTCUSDT_1h.csv"
    update_dataset(path, symbol="BTCUSDT", interval="1h", start_ms=begin, end_ms=end,
                   now_ms=boundary, fetcher=candles)
    return path


def test_forecast_schema_and_no_future_observation(tmp_path):
    predictor = StubPredictor()
    service = ForecastService(build_dataset(tmp_path), lookback=16, predictor=predictor)
    result = service.forecast(horizon=3)
    assert result["trading_enabled"] is False
    assert result["mode"] == "research_only"
    assert result["recommendation"] is None
    assert len(result["dataset_sha256"]) == 64
    assert len(result["forecast"]) == 3
    assert result["horizon"] == 3
    assert predictor.last_y.min().isoformat() == result["forecast"][0]["timestamp"]


def test_refuses_stale_and_invalid_horizon(tmp_path):
    service = ForecastService(build_dataset(tmp_path, stale=True), lookback=16, predictor=StubPredictor())
    with pytest.raises(DataValidationError, match="stale"):
        service.forecast()
    fresh = ForecastService(build_dataset(tmp_path / "fresh"), lookback=16, predictor=StubPredictor())
    for bad in (0, 25, True, 1.5, "1"):
        with pytest.raises(ValueError, match="horizon"):
            fresh.forecast(horizon=bad)


def test_refuses_tampered_csv(tmp_path):
    path = build_dataset(tmp_path)
    path.write_text(path.read_text().replace("101", "103"), encoding="utf-8")
    with pytest.raises(DataValidationError, match="SHA-256"):
        ForecastService(path, lookback=16, predictor=StubPredictor()).forecast()


def test_http_requires_token_and_never_exposes_execution(tmp_path):
    service = ForecastService(build_dataset(tmp_path), lookback=16, predictor=StubPredictor())
    token = "test-only-token-longer-than-24-characters"
    handler = make_handler(service, token)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        def post(payload, token_value):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            conn.request("POST", "/v1/forecast", body=json.dumps(payload),
                         headers={"Content-Type": "application/json", "Authorization": token_value})
            response = conn.getresponse()
            status, data = response.status, json.loads(response.read())
            conn.close()
            return status, data

        assert post({"horizon": 1}, "")[0] == 401
        assert post({"horizon": 1, "execute": True}, "Bearer " + token)[0] == 400
        assert post({"horizon": 1}, "Bearer " + token)[0] == 200
        result = post({"horizon": 1}, "Bearer " + token)[1]
        assert result["trading_enabled"] is False
        assert result["recommendation"] is None
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
