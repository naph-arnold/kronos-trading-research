"""FastAPI app: GET-only JSON API over `command_center.services` plus the static frontend build.

Security model (V1, local):
  * No POST/PUT/PATCH/DELETE route: the Command Center cannot change runner state, create STOP/KILL files,
    activate agents or place orders. Non-read methods get 405 before any handler runs.
  * Bound to 127.0.0.1 by default; `__main__` refuses a non-loopback bind unless a token is configured.
  * Host-header allow-list (DNS-rebinding protection), strict security headers, no stack traces in responses,
    no filesystem paths, credentials or keys in any payload.
  * Optional bearer token (env KRONOS_CC_TOKEN) for a future authenticated HTTPS deployment behind a reverse proxy;
    unset for local use.
"""
from __future__ import annotations

import hmac
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from command_center import services

WEB_OUT = Path(__file__).parent / "web" / "out"
STARTED_AT = datetime.now(timezone.utc).isoformat()
LOG = logging.getLogger("command_center")

SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; object-src 'none'; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def create_app(config_path: Path | None = None) -> FastAPI:
    cfg = services.load_config(config_path)
    security = cfg.get("security", {})
    token = os.environ.get(security.get("token_env", "KRONOS_CC_TOKEN"), "")
    app = FastAPI(title="Kronos Trading Command Center", version="1.1.0", docs_url=None, redoc_url=None,
                  openapi_url="/api/openapi.json" if not token else None)
    app.add_middleware(GZipMiddleware, minimum_size=2048)
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=security.get("allowed_hosts", ["127.0.0.1", "localhost", "testserver"]))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            return JSONResponse({"detail": "Command Center is read-only"}, status_code=405,
                                headers=SECURITY_HEADERS)
        if token and request.url.path != "/api/health":
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                return JSONResponse({"detail": "authentication required"}, status_code=401, headers=SECURITY_HEADERS)
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):  # never leak tracebacks or paths
        LOG.warning("request failed: %s %s: %s", request.method, request.url.path, type(exc).__name__)
        return JSONResponse({"detail": "data temporarily unavailable", "error": type(exc).__name__},
                            status_code=503, headers=SECURITY_HEADERS)

    def cached(key, ttl, fn):
        try:
            return services.CACHE.get(key, ttl, lambda: services.scrub(fn(), cfg))
        except KeyError:
            raise HTTPException(404, "not found") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/health")
    def health():
        """Identity and liveness of THIS dashboard process (used by the launcher). No runner data."""
        return {"service": "kronos-command-center", "status": "ok", "pid": os.getpid(), "started_at": STARTED_AT,
                "version": app.version, "read_only": True, "orders_possible": False, "auth": bool(token),
                "uptime_s": round(time.monotonic() - _T0)}

    @app.get("/api/meta")
    def meta():
        return {"read_only": True, "orders_possible": False, "version": app.version,
                "markets": {k: {"label": v["label"], "venue": v["venue"]} for k, v in cfg["markets"].items()},
                "strategies": [{"id": s["id"], "name": s["name"], "market": s["market"],
                                "strategy_key": s["strategy_key"]} for s in cfg["strategies"]],
                "runners": {k: {"label": v["label"], "market": v["market"]} for k, v in services.runners(cfg).items()},
                "timeframes": services.timeframes()}

    @app.get("/api/overview")
    def overview():
        return cached("overview", 5, lambda: services.overview(cfg))

    @app.get("/api/strategies")
    def strategies():
        return cached("strategies", 5, lambda: [services.strategy_panel(cfg, s) for s in cfg["strategies"]])

    @app.get("/api/strategies/{strategy_id}/signals")
    def strategy_signals(strategy_id: str, include_warmup: bool = True, limit: int = Query(200, ge=1, le=2000)):
        if strategy_id not in {s["id"] for s in cfg["strategies"]}:
            raise HTTPException(404, "unknown strategy")
        return cached(("sigs", strategy_id, include_warmup, limit), 10,
                      lambda: services.signals(cfg, strategy_id, include_warmup=include_warmup, limit=limit))

    @app.get("/api/chart/{market}")
    def chart(market: str, tf: str = "15m", limit: int = Query(500, ge=10, le=5000), until: str | None = None,
              strategies: str | None = None, controls: bool = True, warmup: bool = True):
        if market not in cfg["markets"]:
            raise HTTPException(404, "unknown market")
        if tf not in services.timeframes():
            raise HTTPException(400, f"timeframe must be one of {services.timeframes()}")
        keys = [s for s in (strategies or "").split(",") if s] or None
        return cached(("chart", market, tf, limit, until, strategies, controls, warmup), 10,
                      lambda: services.chart(cfg, market, tf, limit=limit, until=until, strategies=keys,
                                             include_controls=controls, include_warmup=warmup))

    @app.get("/api/availability/{market}")
    def availability(market: str):
        if market not in cfg["markets"]:
            raise HTTPException(404, "unknown market")
        from command_center.bars import availability as avail
        return cached(("avail", market), 300, lambda: avail(cfg["markets"][market]))

    @app.get("/api/history/{runner}/{kind}")
    def history(runner: str, kind: str, q: str | None = Query(None, max_length=100), strategy: str | None = None,
                status: str | None = None, start: str | None = None, end: str | None = None,
                limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        if kind not in ("signals", "decisions", "trades", "events"):
            raise HTTPException(404, "unknown history kind")
        return cached(("hist", runner, kind, q, strategy, status, start, end, limit, offset), 10,
                      lambda: services.history(cfg, runner, kind, q=q, strategy=strategy, status=status, start=start,
                                               end=end, limit=limit, offset=offset))

    @app.get("/api/equity/{runner}")
    def equity(runner: str, strategy: str | None = None):
        return cached(("equity", runner, strategy), 15, lambda: services.equity(cfg, runner, strategy=strategy))

    @app.get("/api/signal/{runner}/{signal_id}")
    def signal(runner: str, signal_id: str):
        return cached(("signal", runner, signal_id), 15, lambda: services.signal_detail(cfg, runner, signal_id))

    @app.get("/api/grok")
    def grok():
        return cached("grok", 10, lambda: services.grok_room(cfg))

    if WEB_OUT.exists():
        app.mount("/_next", StaticFiles(directory=WEB_OUT / "_next"), name="next-assets")

        @app.get("/{path:path}", include_in_schema=False)
        def frontend(path: str):
            root = WEB_OUT.resolve()
            target = (WEB_OUT / path).resolve()
            if root not in target.parents and target != root:
                raise HTTPException(404)
            for candidate in (target, target.with_suffix(".html"), target / "index.html"):
                if candidate.is_file():
                    return FileResponse(candidate)
            return FileResponse(WEB_OUT / "404.html", status_code=404)
    return app


_T0 = time.monotonic()
