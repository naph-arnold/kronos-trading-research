"""TradingView webhook parsing, durable replay protection and HTTP safety tests."""
import http.client
import json
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

import pytest

from research.market_data import INTERVAL_MS
from research.tradingview_ingress import (
    AlertInbox, AlertRejected, make_handler, validate_alert,
)

STEP = INTERVAL_MS["1h"]
NOW = 1_800_000_000_000 // STEP * STEP


def message(*, now_ms=NOW, **overrides):
    start = now_ms - 2 * STEP
    result = {
        "schema_version": 1, "source": "tradingview",
        "exchange": "BINANCE", "symbol": "BTCUSDT", "interval": "1h",
        "event": "ema_bull_cross",
        "bar_time": datetime.fromtimestamp(start / 1000, timezone.utc).isoformat(),
        "bar_close": "71500.50",
    }
    return {**result, **overrides}


def test_hourly_closed_signal_and_canonical_id():
    alert = validate_alert(message(), now_ms=NOW)
    assert alert["alert_id"].startswith("tv:BINANCE:BTCUSDT:1h:ema_bull_cross:")
    assert alert["bar_close"] == 71500.5
    assert alert["status"] == "research_pending"
    assert alert["execution_enabled"] is False


@pytest.mark.parametrize("override", [
    {"source": "fake"}, {"exchange": "BYBIT"}, {"symbol": "ETHUSDT"},
    {"interval": "15m"}, {"event": "buy_now"}, {"schema_version": True},
    {"bar_close": "-2"}, {"bar_close": "NaN"}, {"bar_close": "inf"},
    {"bar_close": True}, {"order": "buy"},
])
def test_rejects_unsupported_or_injected_fields(override):
    with pytest.raises(AlertRejected):
        validate_alert(message(**override), now_ms=NOW)


def test_rejects_future_incomplete_and_old_bars():
    for start in (NOW, NOW - 4 * STEP, NOW - STEP // 2):
        stamp = datetime.fromtimestamp(start / 1000, timezone.utc).isoformat()
        with pytest.raises(AlertRejected):
            validate_alert(message(bar_time=stamp), now_ms=NOW)


def test_durable_deduplication_and_new_events(tmp_path):
    path = tmp_path / "alerts.sqlite"
    alert = validate_alert(message(), now_ms=NOW)
    inbox = AlertInbox(path)
    assert inbox.store(alert) is True
    assert inbox.store(alert) is False
    assert AlertInbox(path).store(alert) is False  # survives restart
    second = validate_alert(message(event="ema_bear_cross"), now_ms=NOW)
    assert inbox.store(second) is True
    assert len(inbox.list_pending()) == 2
    assert all(not x["execution_enabled"] for x in inbox.list_pending())


def test_http_unauthorized_and_duplicate_behavior(tmp_path, monkeypatch):
    from research import tradingview_ingress
    monkeypatch.setattr(tradingview_ingress, "validate_alert",
                        lambda payload: validate_alert(payload, now_ms=NOW))
    token = "example-verified-ingress-token-at-least-32-chars"
    handler = make_handler(AlertInbox(tmp_path / "alerts.sqlite"), token)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        def send(body, *, credentials=True, path="/v1/tradingview/alerts"):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            headers = {"Content-Type": "application/json"}
            if credentials:
                headers["X-Trusted-Ingress"] = token
            conn.request("POST", path, json.dumps(body), headers=headers)
            response = conn.getresponse()
            result = response.status, json.loads(response.read())
            conn.close()
            return result

        assert send(message(), credentials=False)[0] == 401
        assert send(message())[0] == 202
        duplicate = send(message())
        assert duplicate[0] == 200 and duplicate[1]["status"] == "duplicate"
        assert send(message(order="buy"))[0] == 400
        assert send(message(), path="/v1/order")[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_fails_closed_when_proxy_secret_is_weak(tmp_path):
    with pytest.raises(ValueError, match="32"):
        make_handler(AlertInbox(tmp_path / "alerts.sqlite"), "short")
