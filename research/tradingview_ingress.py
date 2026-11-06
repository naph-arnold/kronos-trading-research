"""Fail-closed TradingView alert validation and durable research-only inbox.

TradingView cannot attach custom Authorization headers. In production, a trusted
HTTPS ingress MUST authenticate TradingView at the edge and inject a private
header. This listener binds to loopback and rejects requests without the header.
It never initiates Grok calls, simulation orders, or exchange orders.
"""
from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from research.market_data import INTERVAL_MS, parse_time

ALLOWED_EVENTS = ("ema_bull_cross", "ema_bear_cross")
KEYS = {"schema_version", "source", "exchange", "symbol", "interval",
        "event", "bar_time", "bar_close"}
MAX_BODY = 2048
MAX_DELAY_MS = 2 * INTERVAL_MS["1h"]


class AlertRejected(ValueError):
    """Malformed, unauthorized-by-contract, or stale market alert."""


def validate_alert(payload: object, *, now_ms: int | None = None) -> dict:
    if not isinstance(payload, dict) or set(payload) != KEYS:
        raise AlertRejected("Incorrect alert schema")
    if (type(payload["schema_version"]) is not int or payload["schema_version"] != 1
            or payload["source"] != "tradingview"
            or payload["exchange"] != "BINANCE"
            or payload["symbol"] != "BTCUSDT"
            or payload["interval"] != "1h"
            or payload["event"] not in ALLOWED_EVENTS):
        raise AlertRejected("Unsupported source, market, timeframe, or event")
    if not isinstance(payload["bar_time"], str) or len(payload["bar_time"]) > 40:
        raise AlertRejected("Invalid UTC bar timestamp")
    try:
        bar_ms = parse_time(payload["bar_time"])
    except (ValueError, TypeError, OverflowError) as exc:
        raise AlertRejected("Invalid UTC bar timestamp") from exc
    if bar_ms % INTERVAL_MS["1h"]:
        raise AlertRejected("Unaligned hourly candle")
    try:
        close = float(payload["bar_close"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise AlertRejected("Invalid closing price") from exc
    if isinstance(payload["bar_close"], bool) or not math.isfinite(close) or close <= 0:
        raise AlertRejected("Invalid closing price")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000) if now_ms is None else now_ms
    # TradingView's {{time}} is the bar-opening timestamp. Only accept signals
    # from a finished bar, and reject excessively delayed or replayed alerts.
    if bar_ms + INTERVAL_MS["1h"] > now_ms or now_ms - (bar_ms + INTERVAL_MS["1h"]) > MAX_DELAY_MS:
        raise AlertRejected("Unclosed, future, or stale bar")
    # Deterministic idempotency key; not controlled by the sender.
    alert_id = f"tv:BINANCE:BTCUSDT:1h:{payload['event']}:{bar_ms}"
    return {
        "alert_id": alert_id,
        "schema_version": 1,
        "source": "tradingview",
        "exchange": "BINANCE",
        "symbol": "BTCUSDT",
        "interval": "1h",
        "event": payload["event"],
        "bar_time": datetime.fromtimestamp(bar_ms / 1000, timezone.utc).isoformat(),
        "bar_close": close,
        "received_at": datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(),
        "status": "research_pending",
        "execution_enabled": False,
        "warning": "Untrusted chart alert; requires independent Binance verification and six-role review.",
    }


class AlertInbox:
    """SQLite unique-key inbox: persistent deduplication across process restarts."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS tradingview_alerts (
                alert_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                received_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status = 'research_pending')
            )""")

    def store(self, alert: dict) -> bool:
        with sqlite3.connect(self.path, timeout=5) as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO tradingview_alerts VALUES (?, ?, ?, ?)",
                (alert["alert_id"], json.dumps(alert, sort_keys=True),
                 alert["received_at"], "research_pending"),
            )
            return cursor.rowcount == 1

    def list_pending(self, *, limit: int = 100) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError("Invalid limit")
        with sqlite3.connect(self.path, timeout=5) as db:
            rows = db.execute(
                "SELECT payload_json FROM tradingview_alerts ORDER BY received_at, alert_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]


def make_handler(inbox: AlertInbox, trusted_proxy_token: str):
    if len(trusted_proxy_token) < 32:
        raise ValueError("TRUSTED_INGRESS_TOKEN must contain at least 32 characters")

    class Handler(BaseHTTPRequestHandler):
        def reply(self, code: int, body: dict):
            data = json.dumps(body, allow_nan=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/healthz":
                return self.reply(200, {"status": "ok", "execution_enabled": False})
            return self.reply(404, {"error": "Not found"})

        def do_POST(self):
            if self.path != "/v1/tradingview/alerts":
                return self.reply(404, {"error": "Not found"})
            # This header is set by a verified TLS reverse proxy, NEVER TradingView.
            if not hmac.compare_digest(self.headers.get("X-Trusted-Ingress", ""), trusted_proxy_token):
                return self.reply(401, {"error": "Unauthorized ingress"})
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                return self.reply(415, {"error": "JSON required"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.reply(400, {"error": "Invalid request length"})
            if size < 2 or size > MAX_BODY:
                return self.reply(413, {"error": "Invalid request length"})
            try:
                body = json.loads(self.rfile.read(size))
                alert = validate_alert(body)
            except (ValueError, TypeError):
                return self.reply(400, {"error": "Invalid or stale alert"})
            try:
                created = inbox.store(alert)
            except sqlite3.Error:
                self.log_error("SQLite inbox write failed")
                return self.reply(503, {"error": "Storage unavailable"})
            return self.reply(202 if created else 200, {
                "alert_id": alert["alert_id"],
                "status": "queued_for_research" if created else "duplicate",
                "execution_enabled": False,
            })

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Loopback-only TradingView alert inbox")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if args.host != "127.0.0.1":
        raise SystemExit("Public bind forbidden: use a separately authenticated TLS proxy")
    token = os.environ.get("TRUSTED_INGRESS_TOKEN", "")
    handler = make_handler(AlertInbox(args.db), token)
    with ThreadingHTTPServer((args.host, args.port), handler) as server:
        print("TradingView research-only ingress ready (loopback)", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
