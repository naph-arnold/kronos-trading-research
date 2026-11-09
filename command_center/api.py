"""FastAPI app: GET-only JSON API over `command_center.services` plus the static frontend build.

There is deliberately no POST/PUT/DELETE route: the Command Center cannot change runner state, create STOP/KILL
files, activate agents or place orders. It binds to 127.0.0.1 by default.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from command_center import services

WEB_OUT = Path(__file__).parent / "web" / "out"


def create_app(config_path: Path | None = None) -> FastAPI:
    cfg = services.load_config(config_path)
    app = FastAPI(title="Kronos Trading Command Center", version="1.0.0", docs_url="/api/docs", redoc_url=None,
                  openapi_url="/api/openapi.json")
    app.add_middleware(GZipMiddleware, minimum_size=2048)

    @app.middleware("http")
    async def read_only_guard(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            return JSONResponse({"detail": "Command Center is read-only"}, status_code=405)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/meta")
    def meta():
        return {"read_only": True, "orders_possible": False, "version": app.version,
                "markets": {k: {"label": v["label"], "venue": v["venue"]} for k, v in cfg["markets"].items()},
                "strategies": [{"id": s["id"], "name": s["name"], "market": s["market"]} for s in cfg["strategies"]],
                "timeframes": services.timeframes()}

    @app.get("/api/overview")
    def overview():
        return services.CACHE.get("overview", 5, lambda: services.overview(cfg))

    @app.get("/api/strategies")
    def strategies():
        return services.CACHE.get("strategies", 5, lambda: [services.strategy_panel(cfg, s)
                                                             for s in cfg["strategies"]])

    @app.get("/api/strategies/{strategy_id}/signals")
    def strategy_signals(strategy_id: str, include_warmup: bool = True, limit: int = Query(200, ge=1, le=2000)):
        if strategy_id not in {s["id"] for s in cfg["strategies"]}:
            raise HTTPException(404, "unknown strategy")
        return services.signals(cfg, strategy_id, include_warmup=include_warmup, limit=limit)

    @app.get("/api/chart/{market}")
    def chart(market: str, tf: str = "15m", limit: int = Query(500, ge=10, le=5000), until: str | None = None,
              strategies: str | None = None):
        if market not in cfg["markets"]:
            raise HTTPException(404, "unknown market")
        if tf not in services.timeframes():
            raise HTTPException(400, f"timeframe must be one of {services.timeframes()}")
        keys = [s for s in (strategies or "").split(",") if s] or None
        key = ("chart", market, tf, limit, until, strategies)
        try:
            return services.CACHE.get(key, 10, lambda: services.chart(cfg, market, tf, limit=limit, until=until,
                                                                       strategies=keys))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/availability/{market}")
    def availability(market: str):
        if market not in cfg["markets"]:
            raise HTTPException(404, "unknown market")
        from command_center.bars import availability as avail
        return services.CACHE.get(("avail", market), 300, lambda: avail(cfg["markets"][market]))

    @app.get("/api/grok")
    def grok():
        return services.CACHE.get("grok", 10, lambda: services.grok_room(cfg))

    if WEB_OUT.exists():
        app.mount("/_next", StaticFiles(directory=WEB_OUT / "_next"), name="next-assets")

        @app.get("/{path:path}", include_in_schema=False)
        def frontend(path: str):
            target = (WEB_OUT / path).resolve()
            if WEB_OUT.resolve() not in target.parents and target != WEB_OUT.resolve():
                raise HTTPException(404)
            for candidate in (target, target.with_suffix(".html"), target / "index.html"):
                if candidate.is_file():
                    return FileResponse(candidate)
            return FileResponse(WEB_OUT / "index.html")
    return app
