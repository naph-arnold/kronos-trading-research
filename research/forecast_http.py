"""Loopback-only, read-only HTTP interface for Kronos forecasting agents.

This is NOT a public hosted API. Explicit token authentication is required,
even for local requests; never embed the token in the public repository.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from research.forecast_service import ForecastService
from research.market_data import DataValidationError

MAX_BODY_BYTES = 1024


def make_handler(service: ForecastService, token: str):
    if not token or len(token) < 24:
        raise ValueError("KRONOS_API_TOKEN must contain at least 24 characters")

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, value: dict) -> None:
            body = json.dumps(value, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/healthz":
                return self._reply(200, {"status": "ok", "trading_enabled": False})
            return self._reply(404, {"error": "Not found"})

        def do_POST(self):
            if self.path != "/v1/forecast":
                return self._reply(404, {"error": "Not found"})
            if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
                return self._reply(401, {"error": "Unauthorized"})
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                return self._reply(415, {"error": "Content-Type must be application/json"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._reply(400, {"error": "Invalid content length"})
            if length < 2 or length > MAX_BODY_BYTES:
                return self._reply(413, {"error": "Invalid request size"})
            try:
                payload = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, ValueError):
                return self._reply(400, {"error": "Invalid JSON"})
            if not isinstance(payload, dict) or set(payload) != {"horizon"}:
                return self._reply(400, {"error": "Expected only the horizon field"})
            horizon = payload["horizon"]
            if type(horizon) is not int or not 1 <= horizon <= 24:
                return self._reply(400, {"error": "horizon must be 1..24"})
            try:
                return self._reply(200, service.forecast(horizon=horizon))
            except (DataValidationError, OSError, KeyError, ValueError) as exc:
                # Do not leak absolute paths or private system details to callers.
                self.log_error("Forecast unavailable: %s", type(exc).__name__)
                return self._reply(503, {"error": "Verified market data or forecast unavailable"})
            except Exception:
                self.log_exception_safely()
                return self._reply(503, {"error": "Forecast temporarily unavailable"})

        def log_exception_safely(self):
            self.log_error("Forecast failed; internal error withheld")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Start read-only local Kronos forecast service")
    parser.add_argument("--dataset", type=Path, required=True, help="Local verified Binance CSV")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--lookback", type=int, default=128)
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "::1"):
        raise SystemExit("Binding to public interfaces is disabled; use a secure authenticated proxy")
    token = os.environ.get("KRONOS_API_TOKEN", "")
    handler = make_handler(ForecastService(args.dataset, lookback=args.lookback), token)
    with ThreadingHTTPServer((args.host, args.port), handler) as server:
        print("Kronos research-only forecast API listening on loopback", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
