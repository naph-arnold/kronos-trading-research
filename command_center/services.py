"""Read-only views for the Command Center, built on the existing reporting code.

Reused: `research.dual_health_report.market_health` (runner health, alerts, integrity), `trading.shadow.report`
(`book_metrics`, `reason_key`), the strategies' own session definitions (opening ranges), `research.grok_advisory`
(audit verification) and `trading.agents.desk` (role briefs). Every value is labelled by kind:
  observed   - recorded by a runner from the venue (quotes, bars, heartbeats, demo-account checks)
  simulated  - shadow-book results (no order was ever placed)
  derived    - computed here from recorded data (EMA, swing structure, aggregated candles)
  unavailable- not recorded; shown as such, never filled in
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from command_center.bars import TIMEFRAMES, floor_time, load_candles, parse_time, tf_seconds
from command_center.readonly import ReadOnlyShadowStore, connect_ro

UTC = timezone.utc
CONFIG_PATH = Path(__file__).with_name("config.json")
HEARTBEAT_STALE = timedelta(minutes=3)
RUNNER_DOWN = timedelta(minutes=5)


def load_config(path: Path | None = None) -> dict:
    return json.loads(Path(path or os.environ.get("KRONOS_CC_CONFIG") or CONFIG_PATH).read_text(encoding="utf-8"))


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(t: datetime | None) -> str | None:
    return None if t is None else t.astimezone(UTC).isoformat()


class _Cache:
    """Tiny TTL cache so polling clients cannot make the dashboard rescan stores more than every few seconds."""

    def __init__(self, max_items: int = 64):
        self._items: dict = {}
        self.max_items = max_items

    def get(self, key, ttl: float, fn):
        hit = self._items.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        value = fn()
        if len(self._items) >= self.max_items:  # bounded memory: drop the oldest entries
            for old in sorted(self._items, key=lambda k: self._items[k][0])[: self.max_items // 4]:
                self._items.pop(old, None)
        self._items[key] = (time.monotonic(), value)
        return value


# Errors that mean "this runner's data cannot be read right now" (locked, missing, corrupt, half-written).
DATA_ERRORS = (sqlite3.Error, OSError, ValueError, KeyError, TypeError)


def unavailable(kind: str, exc: Exception) -> dict:
    """A runner whose store cannot be read is reported as unavailable, never as healthy or empty."""
    return {"available": False, "runner": "data unavailable", "lifecycle": "unavailable",
            "error": f"{kind}: {type(exc).__name__}", "metrics": None}


CACHE = _Cache()


# --------------------------------------------------------------------------- runner / market status

def _runner_files(state: Path) -> dict:
    breaker = None
    path = state / "breaker.json"
    if path.exists():
        try:
            breaker = json.loads(path.read_text(encoding="utf-8")).get("trips", [])
        except (OSError, ValueError):
            breaker = ["unreadable (fail closed)"]
    return {"kill_file": (state / "KILL").exists(), "stop_file": (state / "STOP").exists(),
            "breaker_trips": breaker or []}


def _last_heartbeat(db) -> tuple[datetime | None, dict]:
    row = db.execute("SELECT at, detail FROM heartbeats ORDER BY at DESC LIMIT 1").fetchone()
    return (parse_time(row[0]), json.loads(row[1])) if row else (None, {})


def _daily_halts(state: Path, now: datetime) -> list[dict]:
    path = state / "daily.sqlite"
    if not path.exists():
        return []
    con = connect_ro(path)
    try:
        rows = con.execute("SELECT account_id, day, start_equity, last_equity, last_at, halted FROM daily "
                           "WHERE day >= ? ORDER BY day DESC", ((now - timedelta(days=2)).date().isoformat(),)).fetchall()
    finally:
        con.close()
    return [{"account": r[0], "day": r[1], "start_equity": float(r[2]), "last_equity": float(r[3]), "at": r[4],
             "halted": bool(r[5])} for r in rows]


def market_status(name: str, market: dict, *, now: datetime | None = None) -> dict:
    """Live observations for one market from its runner's store (read-only)."""
    now = now or _now()
    state = Path(market["state_dir"])
    db_path = state / "shadow.sqlite"
    out = {"market": name, "label": market["label"], "venue": market["venue"], "kind": "observed"}
    if not db_path.exists():
        out.update(runner="not started", available=False)
        return out
    db = connect_ro(db_path)
    try:
        at, hb = _last_heartbeat(db)
        meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
        last_bar = db.execute("SELECT MAX(open_time) FROM bars").fetchone()[0]
        acct = db.execute("SELECT at, detail FROM events WHERE kind IN ('mt5_account_check', 'mt5_account_baseline') "
                          "ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        db.close()
    age = None if at is None else now - at
    out["available"] = True
    out["runner"] = ("running" if age is not None and age <= HEARTBEAT_STALE else
                     "heartbeat delayed" if age is not None and age <= RUNNER_DOWN else "not responding")
    out["last_heartbeat"] = _iso(at)
    out["last_heartbeat_age_s"] = None if age is None else round(age.total_seconds())
    quote = meta.get("last_tick", "")
    if quote.count("|") == 2:
        t, bid, ask = quote.split("|")
        out["last_quote"] = {"time": t, "bid": float(bid), "ask": float(ask), "kind": "observed",
                             "age_s": round((now - parse_time(t)).total_seconds())}
    else:
        out["last_quote"] = None
    out["last_bar_open"] = last_bar
    if "runner_status" in hb or "mt5_connection" in hb:  # XAUUSD (MT5) heartbeat
        out.update(connection=hb.get("mt5_connection"), market_open=hb.get("market_open"),
                   data=hb.get("market_data"), new_entries=hb.get("new_entries"), calendar=hb.get("calendar"),
                   entry_block_reasons=hb.get("entry_block_reasons", []),
                   last_market_data_at=hb.get("last_market_data_at"),
                   last_successful_poll_at=hb.get("last_successful_poll_at"),
                   connection_failures=hb.get("connection_failures"), last_error=hb.get("last_error"))
    else:  # BTC/USDT (Binance) heartbeat
        stream = hb.get("stream") or {}
        out.update(connection="connected" if stream.get("connected") else "disconnected", market_open=True,
                   data=(hb.get("data") or {}).get("klines"), quotes=(hb.get("data") or {}).get("quotes"),
                   new_entries="blocked" if hb.get("status") != "ok" else "allowed", calendar="24/7",
                   entry_block_reasons=[] if hb.get("status") == "ok" else [hb.get("status")],
                   last_market_data_at=None if stream.get("last_message_age_s") is None or at is None else
                   _iso(at - timedelta(seconds=float(stream["last_message_age_s"]))),
                   websocket_reconnects=stream.get("reconnects"), last_error=stream.get("last_error"))
    if acct:
        detail = json.loads(acct[1])
        out["demo_account"] = {"checked_at": acct[0], "balance": detail.get("balance"), "equity": detail.get("equity"),
                               "positions": detail.get("positions_total"), "orders": detail.get("orders_total"),
                               "kind": "observed (read-only demo account; never traded)"}
    out["controls"] = _runner_files(state)
    return out


# --------------------------------------------------------------------------- strategies

def _strategy_runner(cfg: dict, strategy: dict) -> dict:
    """Runner location for a strategy: its own (Strategy A) or its market's runner."""
    if strategy.get("state_dir"):
        return {"state_dir": strategy["state_dir"], "repo": strategy["repo"], "config": strategy["config"],
                "store_interval": strategy.get("store_interval")}
    m = cfg["markets"][strategy["market"]]
    return {"state_dir": m["state_dir"], "repo": m["repo"], "config": m["config"],
            "store_interval": m.get("store_interval")}


def _primary_book(books: dict, key: str) -> str | None:
    """Headline book: largest equity, highest (most conservative) cost scenario. Cost scenarios and equity sizes
    are alternative simulations of the SAME signals, so they are never summed."""
    own = [(b, i) for b, i in books.items() if i["strategy_key"] == key]
    if not own:
        return None
    return max(own, key=lambda bi: (bi[1]["initial_equity"], bi[1]["commission"]))[0]


def _daily_pnl(store: ReadOnlyShadowStore, book_id: str, initial: Decimal, now: datetime) -> dict:
    curve = store.equity_curve(book_id)
    if not curve:
        return {"today": 0.0, "cumulative": 0.0, "equity": float(initial), "marked": False}
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    before = [v for t, v in curve if t < day_start]
    base = before[-1] if before else initial
    last = curve[-1][1]
    return {"today": float(last - base), "cumulative": float(last - initial), "equity": float(last), "marked": True}


def strategy_panel(cfg: dict, strategy: dict, *, now: datetime | None = None) -> dict:
    from trading.shadow.report import book_metrics, reason_key

    now = now or _now()
    runner = _strategy_runner(cfg, strategy)
    state = Path(runner["state_dir"])
    db_path = state / "shadow.sqlite"
    panel = {"id": strategy["id"], "name": strategy["name"], "market": strategy["market"],
             "strategy_key": strategy["strategy_key"], "kind": "simulated"}
    try:
        run_cfg = json.loads((Path(runner["repo"]) / runner["config"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        run_cfg = {}
    panel["risk_limits"] = run_cfg.get("risk")
    if "launch_approved" in run_cfg:
        panel["launch_approved"] = run_cfg["launch_approved"]
    if not db_path.exists():
        panel.update(lifecycle="prepared", lifecycle_detail=(
            "Prepared, not launched: no runner state exists. It is never shown as running until its continuous "
            "runner has been launched and is writing verified heartbeats."), metrics=None)
        return panel

    controls = _runner_files(state)
    with ReadOnlyShadowStore(db_path) as store:
        at, hb = _last_heartbeat(store._db)
        books = store.books()
        key = strategy["strategy_key"]
        sigs = [s for s in store.signals() if s["strategy_key"] == key]
        by_status = Counter(s["status"] for s in sigs)
        forward = [s for s in sigs if s["status"] != "warmup"]
        primary = _primary_book(books, key)
        book_rows = []
        for book_id, info in sorted(books.items()):
            if info["strategy_key"] != key:
                continue
            m = book_metrics(store, book_id, info)
            m["pnl"] = _daily_pnl(store, book_id, info["initial_equity"], now)
            m["primary"] = book_id == primary
            book_rows.append(m)
        rejections = []
        for book_id in [b for b, i in books.items() if i["strategy_key"] == key]:
            for d in store.decisions(book_id):
                if not d["approved"]:
                    rejections.append({"book": book_id, "signal_id": d["signal_id"], "at": d["evaluated_at"],
                                       "reasons": d["reasons"], "categories": sorted({reason_key(r) for r in d["reasons"]})})
        rejections.sort(key=lambda r: r["at"], reverse=True)
        trades = [t for b in books if books[b]["strategy_key"] == key for t in store.trades(b)]
        positions = {b: p for b, p in store.positions().items() if books.get(b, {}).get("strategy_key") == key}
        decisions_at = [d["evaluated_at"] for b in books if books[b]["strategy_key"] == key
                        for d in store.decisions(b)]
    activity = [x for x in ([s["bar_close"] for s in sigs] + decisions_at + [t["closed_at"] for t in trades]) if x]
    age = None if at is None else now - at
    hb_entries_blocked = hb.get("new_entries") == "blocked" or (hb.get("status") not in (None, "ok"))
    if controls["stop_file"]:
        lifecycle, detail = "paused", "STOP file present: the supervisor will not (re)start the runner."
    elif controls["kill_file"] or controls["breaker_trips"]:
        lifecycle, detail = "blocked", "KILL file or circuit-breaker trip: new simulated entries are rejected."
    elif age is None or age > RUNNER_DOWN:
        lifecycle, detail = "stopped", "No recent heartbeat from the runner."
    elif hb_entries_blocked:
        reasons = hb.get("entry_block_reasons") or [hb.get("status")]
        lifecycle, detail = "running", f"Running; new entries currently blocked by guards: {reasons}"
    else:
        lifecycle, detail = "running", "Running; heartbeats fresh; entries allowed by the guards."
    primary_row = next((r for r in book_rows if r["primary"]), None)
    wins = sum(1 for t in trades if Decimal(t["pnl"]) > 0 and t["book_id"] == primary)
    losses = sum(1 for t in trades if Decimal(t["pnl"]) <= 0 and t["book_id"] == primary)
    panel.update(
        lifecycle=lifecycle, lifecycle_detail=detail, last_heartbeat=_iso(at),
        last_activity=max(activity) if activity else None,
        signals={"total": len(sigs), "forward": len(forward), "by_status": dict(by_status)},
        primary_book=primary,
        metrics=None if primary_row is None else {
            "trades": primary_row["trades"], "wins": wins, "losses": losses,
            "win_rate_pct": primary_row["win_rate_pct"], "profit_factor": primary_row["profit_factor"],
            "net_pnl": primary_row["net_pnl"], "net_return_pct": primary_row["net_return_pct"],
            "max_drawdown_pct": primary_row["max_drawdown_pct"], "costs": primary_row["costs"],
            "approved": primary_row["approved"], "rejected": primary_row["rejected"],
            "rejection_reasons": primary_row["rejection_reasons"], "pnl": primary_row["pnl"],
            "mean_r": primary_row["mean_r"]},
        books=book_rows, rejections=rejections[:50], open_positions=positions, controls=controls)
    return panel


# --------------------------------------------------------------------------- overview

def risk_status(cfg: dict, name: str, market: dict, now: datetime) -> dict:
    state = Path(market["state_dir"])
    try:
        limits = json.loads((Path(market["repo"]) / market["config"]).read_text(encoding="utf-8")).get("risk", {})
    except (OSError, ValueError):
        limits = {}
    out = {"limits": {"risk_per_trade_pct": float(limits.get("risk_fraction", 0)) * 100 or None,
                      "daily_loss_halt_pct": float(limits.get("daily_loss_halt_fraction", 0)) * 100 or None,
                      "aggregate_open_risk_pct": float(limits.get("aggregate_risk_fraction", 0)) * 100 or None},
           "controls": _runner_files(state), "daily": _daily_halts(state, now)}
    db_path = state / "shadow.sqlite"
    worst = 0.0
    if db_path.exists():
        with ReadOnlyShadowStore(db_path) as store:
            books = store.books()
            for book_id, pos in store.positions().items():
                eq = books.get(book_id, {}).get("initial_equity")
                if eq:
                    worst = max(worst, float(Decimal(str(pos.get("planned_risk", 0))) / eq) * 100)
    out["max_open_risk_pct_of_equity"] = round(worst, 4)
    out["halted_accounts_today"] = [d["account"] for d in out["daily"]
                                    if d["halted"] and d["day"] == now.date().isoformat()]
    tripped = out["controls"]["kill_file"] or out["controls"]["breaker_trips"] or out["halted_accounts_today"]
    out["state"] = "halted" if tripped else "within limits"
    return out


def overview(cfg: dict) -> dict:
    from research.dual_health_report import market_health

    now = _now()
    hours = float(cfg.get("health_window_hours", 24))
    markets, health, risk = {}, {}, {}
    for name, m in cfg["markets"].items():
        try:
            markets[name] = market_status(name, m, now=now)
        except DATA_ERRORS as exc:
            markets[name] = {"market": name, "label": m["label"], "venue": m["venue"], **unavailable("store", exc)}
        try:
            health[name] = market_health(name, Path(m["state_dir"]), Path(m["repo"]), m["config"], now=now,
                                         hours=hours)
        except DATA_ERRORS as exc:
            health[name] = {"alerts": [f"health data unavailable ({type(exc).__name__})"]}
        try:
            risk[name] = risk_status(cfg, name, m, now)
        except DATA_ERRORS as exc:
            risk[name] = {"state": "unknown", "error": type(exc).__name__}
    strategies = []
    for s in cfg["strategies"]:
        try:
            strategies.append(strategy_panel(cfg, s, now=now))
        except DATA_ERRORS as exc:
            strategies.append({"id": s["id"], "name": s["name"], "market": s["market"],
                               "strategy_key": s["strategy_key"], **unavailable("store", exc),
                               "lifecycle_detail": "The runner's database could not be read; nothing is inferred."})
    accounts = []
    for name, h in health.items():
        for book_id, b in sorted((h.get("books") or {}).items()):
            accounts.append({"market": name, "book": book_id, **b, "kind": "simulated"})
    alerts = [{"market": n, "text": a} for n, h in health.items() for a in h.get("alerts", [])]
    from command_center import decisions as dec

    try:
        decision = dec.decision_status(cfg, markets, strategies, now, CACHE)
    except Exception as exc:  # noqa: BLE001 - never hide the rest of the overview
        decision = [{"id": "all", "state": "unavailable", "headline": f"decision status unavailable ({type(exc).__name__})"}]
    try:
        groups = dec.alert_groups(cfg, markets, health, risk, now)
    except Exception as exc:  # noqa: BLE001
        groups = {"active": [{"market": "-", "severity": "medium", "text": f"alert classification unavailable ({type(exc).__name__})"}],
                  "connection": [], "resolved": [], "stop_kill": []}
    try:
        activity = dec.recent_activity(cfg, now)
    except Exception:  # noqa: BLE001
        activity = []
    return {"generated_at": _iso(now), "read_only": True, "orders_possible": False, "markets": markets,
            "decisions": decision, "alert_groups": groups, "activity": activity,
            "warmup_note": "Warm-up signals were generated on history before each forward-test boundary to prime the "
                           "strategies. They are not evidence, never reach the risk engine and are never traded.",
            "health": {n: {k: h.get(k) for k in ("uptime", "disconnections", "data_gaps", "signals", "decisions",
                                                    "trades", "integrity")} for n, h in health.items()},
            "risk": risk, "alerts": alerts,
            "strategies": [{k: s.get(k) for k in ("id", "name", "market", "lifecycle", "lifecycle_detail",
                                                    "last_activity", "signals", "metrics", "primary_book",
                                                    "launch_approved")} for s in strategies],
            "accounts": accounts}


# --------------------------------------------------------------------------- charts

def _ema(values: list[float], n: int) -> list[float | None]:
    out, k, ema = [], 2 / (n + 1), None
    for i, v in enumerate(values):
        ema = v if ema is None else v * k + ema * (1 - k)
        out.append(ema if i >= n - 1 else None)
    return out


def _structure(candles: list[dict], width: int = 2) -> list[dict]:
    """Swing points (fractal of `width` bars each side) labelled HH/LH/HL/LL relative to the previous swing."""
    points, last_high, last_low = [], None, None
    for i in range(width, len(candles) - width):
        c = candles[i]
        window = candles[i - width:i + width + 1]
        if c["high"] == max(x["high"] for x in window):
            label = "SH" if last_high is None else ("HH" if c["high"] > last_high else "LH")
            points.append({"time": c["time"], "kind": "high", "price": c["high"], "label": label})
            last_high = c["high"]
        if c["low"] == min(x["low"] for x in window):
            label = "SL" if last_low is None else ("HL" if c["low"] > last_low else "LL")
            points.append({"time": c["time"], "kind": "low", "price": c["low"], "label": label})
            last_low = c["low"]
    return points[-60:]


def _opening_range(signal: dict, seconds: int) -> dict | None:
    """ORB opening range from the strategy's own session definition and the signal's recorded range."""
    try:
        from trading.strategies.xauusd import SESSIONS
        body = signal["body"]
        prov = body.get("provenance") or {}
        ctx, params = prov.get("context") or {}, prov.get("params") or {}
        lo, hi = ctx["range"]
        tz, open_minute = SESSIONS[ctx.get("session", "newyork")]
        local = tz.to_server(parse_time(prov["source_bar_open"]))  # naive session-local wall clock
        start = tz.to_utc(datetime(local.year, local.month, local.day) + timedelta(minutes=open_minute))
        start = start if start.tzinfo else start.replace(tzinfo=UTC)
        end = start + timedelta(minutes=int(params.get("range_minutes", 30)))
        return {"signal_id": signal["signal_id"], "from": int(floor_time(start, seconds).timestamp()),
                "to": int(end.timestamp()), "low": float(lo), "high": float(hi),
                "session": ctx.get("session"), "kind": "derived from the signal's recorded range"}
    except (KeyError, ValueError, TypeError, ImportError):
        return None


def _outlier_wicks(candles: list[dict]) -> list[dict]:
    """Derived data-quality note: candles whose wick is > 10x the median range of the displayed candles while the
    body is ordinary (an isolated off-market print). They are flagged, never removed or edited."""
    ranges = sorted(c["high"] - c["low"] for c in candles)
    if len(ranges) < 20:
        return []
    median = ranges[len(ranges) // 2] or 1e-9
    out = []
    for c in candles:
        body_lo, body_hi = min(c["open"], c["close"]), max(c["open"], c["close"])
        wick = max(body_lo - c["low"], c["high"] - body_hi)
        if wick > 10 * median and (body_hi - body_lo) < 3 * median:
            out.append({"time": c["time"], "low": c["low"], "high": c["high"], "wick": round(wick, 2),
                        "median_range": round(median, 2)})
    return out


def chart(cfg: dict, market_name: str, tf: str, *, limit: int = 500, until: str | None = None,
          strategies: list[str] | None = None, include_controls: bool = True, include_warmup: bool = True) -> dict:
    market = cfg["markets"][market_name]
    seconds = tf_seconds(tf)
    data = load_candles(market, tf, limit=limit, until=None if until is None else parse_time(until))
    candles = data["candles"]
    closes = [c["close"] for c in candles]
    data["indicators"] = {"kind": "derived (computed from the displayed candles)",
                          "ema20": [{"time": c["time"], "value": v} for c, v in zip(candles, _ema(closes, 20)) if v],
                          "ema50": [{"time": c["time"], "value": v} for c, v in zip(candles, _ema(closes, 50)) if v]}
    e20, e50 = data["indicators"]["ema20"], data["indicators"]["ema50"]
    if e20 and e50:
        trend = "up" if e20[-1]["value"] > e50[-1]["value"] else "down"
        data["indicators"]["trend"] = trend
    data["structure"] = _structure(candles)
    data["outliers"] = _outlier_wicks(candles)
    if data["outliers"]:
        data["notes"].append(f"{len(data['outliers'])} candle(s) with an isolated wick over 10x the median range "
                             "(off-market print in the broker's recorded data). Shown exactly as recorded.")
    data["signals"], data["trades"], data["positions"], data["opening_ranges"] = [], [], [], []
    if not candles:
        return data
    t0, t1 = candles[0]["time"], candles[-1]["time"] + seconds
    candidates = {s["strategy_key"] for s in cfg["strategies"] if s["market"] == market_name}
    stores = {Path(market["state_dir"]) / "shadow.sqlite"}
    stores |= {Path(s["state_dir"]) / "shadow.sqlite" for s in cfg["strategies"]
               if s["market"] == market_name and s.get("state_dir")}
    for db_path in stores:
        if not db_path.exists():
            continue
        with ReadOnlyShadowStore(db_path) as store:
            books = store.books()
            decisions = {}
            for b in books:
                for d in store.decisions(b):
                    decisions.setdefault(d["signal_id"], []).append(d)
            for s in store.signals():
                if strategies and s["strategy_key"] not in strategies:
                    continue
                is_control = s["strategy_key"] not in candidates  # baselines and random controls
                if (is_control and not include_controls) or (s["status"] == "warmup" and not include_warmup):
                    continue
                close = parse_time(s["bar_close"])
                marker_t = int(floor_time(close - timedelta(seconds=1), seconds).timestamp())
                if not t0 <= marker_t < t1:
                    continue
                body = s["body"]
                ds = decisions.get(s["signal_id"], [])
                data["signals"].append({
                    "time": marker_t, "bar_close": s["bar_close"], "signal_id": s["signal_id"],
                    "strategy": s["strategy_key"], "control": is_control,
                    "status": s["status"], "side": body.get("side"), "entry": _f(body.get("entry_price")),
                    "stop": _f(body.get("stop_price")), "take_profit": _f(body.get("take_profit")),
                    "decisions": {"approved": sum(1 for d in ds if d["approved"]),
                                  "rejected": sum(1 for d in ds if not d["approved"])}})
                if s["strategy_key"].startswith("orb_"):
                    rng = _opening_range(s, seconds)
                    if rng and t0 <= rng["from"] < t1:
                        data["opening_ranges"].append(rng)
            for b, info in books.items():
                if strategies and info["strategy_key"] not in strategies:
                    continue
                for t in store.trades(b):
                    opened, closed = parse_time(t["opened_at"]), parse_time(t["closed_at"])
                    if int(closed.timestamp()) < t0 or int(opened.timestamp()) >= t1:
                        continue
                    data["trades"].append({
                        "book": b, "strategy": info["strategy_key"], "signal_id": t["signal_id"], "side": t["side"],
                        "entry_time": int(floor_time(opened, seconds).timestamp()), "entry": _f(t["entry"]),
                        "exit_time": int(floor_time(closed, seconds).timestamp()), "exit": _f(t["exit"]),
                        "stop": _f(t["stop"]), "take_profit": _f(t.get("take_profit")), "pnl": _f(t["pnl"]),
                        "r": _f(t.get("r_multiple")), "exit_reason": t.get("exit_reason"), "kind": "simulated"})
                for book_id, p in store.positions().items():
                    if book_id != b:
                        continue
                    data["positions"].append({
                        "book": b, "strategy": info["strategy_key"], "side": p["side"], "entry": _f(p["entry"]),
                        "entry_time": int(floor_time(parse_time(p["opened_at"]), seconds).timestamp()),
                        "stop": _f(p["stop"]), "take_profit": _f(p.get("take_profit")), "kind": "simulated"})
    if not data["signals"]:
        data["notes"].append("No strategy signals fall inside the displayed range.")
    if not data["trades"] and not data["positions"]:
        data["notes"].append("No simulated trades in the displayed range.")
    return data


def _f(v):
    return None if v is None else float(v)


# --------------------------------------------------------------------------- signal inspection

def signals(cfg: dict, strategy_id: str, *, include_warmup: bool = True, limit: int = 200) -> dict:
    strategy = next(s for s in cfg["strategies"] if s["id"] == strategy_id)
    db_path = Path(_strategy_runner(cfg, strategy)["state_dir"]) / "shadow.sqlite"
    if not db_path.exists():
        return {"strategy": strategy_id, "available": False, "signals": [],
                "note": "Runner not started: no signals recorded."}
    keys = {strategy["strategy_key"], *strategy.get("controls", [])}
    with ReadOnlyShadowStore(db_path) as store:
        books = store.books()
        decisions = {}
        for b in books:
            for d in store.decisions(b):
                decisions.setdefault(d["signal_id"], []).append(d)
        trades = {}
        for b in books:
            for t in store.trades(b):
                trades.setdefault(t["signal_id"], []).append(t)
        rows = []
        for s in sorted(store.signals(), key=lambda x: x["bar_close"], reverse=True):
            if s["strategy_key"] not in keys or (not include_warmup and s["status"] == "warmup"):
                continue
            body = s["body"]
            prov = body.get("provenance") or {}
            rows.append({
                "signal_id": s["signal_id"], "strategy": s["strategy_key"],
                "control": s["strategy_key"] != strategy["strategy_key"], "status": s["status"],
                "bar_close": s["bar_close"], "side": body.get("side"), "entry": _f(body.get("entry_price")),
                "stop": _f(body.get("stop_price")), "take_profit": _f(body.get("take_profit")),
                "context": prov.get("context"), "params_hash": prov.get("params_hash"),
                "decisions": [{"book": d["book_id"], "approved": d["approved"], "reasons": d["reasons"],
                               "at": d["evaluated_at"], "size": d["size"], "planned_risk": d["planned_risk"]}
                              for d in decisions.get(s["signal_id"], [])],
                "trades": [{"book": t["book_id"], "pnl": _f(t["pnl"]), "r": _f(t.get("r_multiple")),
                            "exit_reason": t.get("exit_reason"), "closed_at": t["closed_at"]}
                           for t in trades.get(s["signal_id"], [])]})
            if len(rows) >= limit:
                break
    return {"strategy": strategy_id, "available": True, "signals": rows,
            "note": "Status 'warmup' = generated on history before the forward-test boundary: not evidence."}


# --------------------------------------------------------------------------- Grok decision room

def grok_room(cfg: dict) -> dict:
    from trading.agents.desk import ROLE_BRIEFS, ROLES

    g = cfg.get("grok", {})
    try:
        gcfg = json.loads(Path(g["config"]).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        gcfg = {}
    room = {
        "activated": gcfg.get("activated") is True,
        "env_activation_flag": os.environ.get("GROK_AGENTS_ACTIVATED") == "1",
        "xai_key_present": bool(os.environ.get("XAI_API_KEY")),  # presence only; the key is never read here
        "model": gcfg.get("model"), "provider": "xAI Grok API (official, https://api.x.ai)",
        "daily_usd_budget": gcfg.get("daily_usd_budget"), "daily_call_budget": gcfg.get("daily_call_budget"),
        "pricing": gcfg.get("pricing_usd_per_million"), "trigger": gcfg.get("review_trigger"),
        "roles": [{"role": r, "brief": ROLE_BRIEFS[r]} for r in ROLES],
        "advisory_only": True, "stores": []}
    for d in g.get("advisory_dirs", []):
        db = Path(d) / "advisory.sqlite"
        if not db.exists():
            continue
        con = connect_ro(db)
        try:
            day = _now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            cols = {r[1] for r in con.execute("PRAGMA table_info(calls)")}
            cost = con.execute("SELECT COALESCE(SUM(est_cost_usd), 0) FROM calls WHERE at >= ? AND transport='xai'",
                               (day,)).fetchone()[0] if "est_cost_usd" in cols else None
            summaries = [{"subject": r[0], "at": r[1], "runner": r[2], "recommendation": r[3],
                          "body": json.loads(r[4])} for r in con.execute(
                "SELECT subject, at, runner, recommendation, body FROM summaries ORDER BY at DESC LIMIT 25")]
            outputs = [{"subject": r[0], "role": r[1], "at": r[2], "valid": bool(r[3]), "error": r[4]}
                       for r in con.execute("SELECT subject, role, at, valid, error FROM outputs ORDER BY at DESC LIMIT 60")]
            calls = dict(con.execute("SELECT transport, COUNT(*) FROM calls GROUP BY transport").fetchall())
        finally:
            con.close()
        audit = None
        try:
            from research.grok_advisory import AuditLog
            audit = AuditLog.verify(Path(d) / "audit.jsonl")
        except Exception as exc:  # noqa: BLE001 - verification problems are shown, not raised
            audit = {"ok": False, "problems": [f"{type(exc).__name__}: {exc}"]}
        room["stores"].append({"dir": d, "calls_by_transport": calls, "usd_today": cost, "summaries": summaries,
                               "outputs": outputs, "audit": audit})
    for i, store in enumerate(room["stores"]):  # never expose filesystem paths
        store["dir"] = f"advisory store {i + 1}"
    room["status"] = ("active" if room["activated"] and room["env_activation_flag"] and room["xai_key_present"]
                      else "not activated")
    return room


def timeframes() -> list[str]:
    return list(TIMEFRAMES)


# --------------------------------------------------------------------------- history and analytics (section 7)

INTERRUPTION_EVENTS = {"data_interruption", "mt5_error", "start_failed", "reconnect", "quote_source", "clock_offset",
                       "kline_gap", "bar_without_ticks", "kronos_unavailable", "warmup_partial"}
ALERT_EVENTS = {"calendar_refresh_failed", "kill_file", "stop_file", "housekeeping_error"}


def runners(cfg: dict) -> dict:
    """Every recorded store the dashboard can read, keyed by a public id (no paths leave the server)."""
    out = {}
    for name, m in cfg["markets"].items():
        out[name] = {"label": m["label"], "market": name, "db": Path(m["state_dir"]) / "shadow.sqlite",
                     "interval": m.get("store_interval")}
    for s in cfg["strategies"]:
        if s.get("state_dir"):
            out[s["id"]] = {"label": s["name"], "market": s["market"], "db": Path(s["state_dir"]) / "shadow.sqlite",
                            "interval": s.get("store_interval")}
    return out


def _match(q: str | None, *fields) -> bool:
    if not q:
        return True
    q = q.lower()
    return any(q in str(f).lower() for f in fields if f is not None)


def _in_range(at: str | None, start: str | None, end: str | None) -> bool:
    if at is None:
        return start is None and end is None
    return (start is None or at >= start) and (end is None or at <= end)


def _event_class(kind: str, detail: dict) -> str:
    if kind in INTERRUPTION_EVENTS:
        return "interruption"
    if kind in ALERT_EVENTS or (kind == "reconciliation" and not detail.get("ok", True)):
        return "alert"
    return "routine"


def history(cfg: dict, runner_id: str, kind: str, *, q: str | None = None, strategy: str | None = None,
            status: str | None = None, start: str | None = None, end: str | None = None, limit: int = 200,
            offset: int = 0) -> dict:
    """Searchable, paginated history of one runner's recorded signals, risk decisions, simulated trades or events.
    Newest first. Read-only."""
    from trading.shadow.report import reason_key

    r = runners(cfg).get(runner_id)
    if r is None:
        raise KeyError(runner_id)
    base = {"runner": runner_id, "label": r["label"], "kind": kind, "rows": [], "total": 0}
    if not r["db"].exists():
        return {**base, "available": False, "note": "Runner not started: nothing recorded."}
    rows: list[dict] = []
    with ReadOnlyShadowStore(r["db"]) as store:
        books = store.books()
        key_of = {b: i["strategy_key"] for b, i in books.items()}
        if kind == "signals":
            decisions: dict[str, list] = {}
            for d in store.decisions():
                decisions.setdefault(d["signal_id"], []).append(d)
            traded = {t["signal_id"] for t in store.trades()}
            for s in store.signals():
                body = s["body"]
                ds = decisions.get(s["signal_id"], [])
                row = {"at": s["bar_close"], "signal_id": s["signal_id"], "strategy": s["strategy_key"],
                       "status": s["status"], "side": body.get("side"), "entry": _f(body.get("entry_price")),
                       "stop": _f(body.get("stop_price")), "take_profit": _f(body.get("take_profit")),
                       "approved": sum(1 for d in ds if d["approved"]), "rejected": sum(1 for d in ds if not d["approved"]),
                       "traded": s["signal_id"] in traded,
                       "phase": "warmup" if s["status"] == "warmup" else "forward"}
                if strategy and row["strategy"] != strategy:
                    continue
                if status == "forward" and row["phase"] != "forward" or status not in (None, "", "forward") and row["status"] != status:
                    continue
                if _in_range(row["at"], start, end) and _match(q, row["signal_id"], row["strategy"], row["side"]):
                    rows.append(row)
        elif kind == "decisions":
            for d in store.decisions():
                row = {"at": d["evaluated_at"], "book": d["book_id"], "strategy": key_of.get(d["book_id"]),
                       "signal_id": d["signal_id"], "approved": d["approved"], "size": _f(d["size"]),
                       "planned_risk": _f(d["planned_risk"]), "reasons": d["reasons"],
                       "categories": sorted({reason_key(x) for x in d["reasons"]})}
                if strategy and row["strategy"] != strategy:
                    continue
                if status and status != ("approved" if d["approved"] else "rejected"):
                    continue
                if _in_range(row["at"], start, end) and _match(q, row["signal_id"], row["book"], *row["reasons"]):
                    rows.append(row)
        elif kind == "trades":
            for t in store.trades():
                costs = {k: _f(t.get(k)) for k in ("spread_slippage_cost", "commission", "swap")}
                row = {"at": t["closed_at"], "opened_at": t.get("opened_at"), "book": t["book_id"],
                       "strategy": key_of.get(t["book_id"]), "signal_id": t["signal_id"], "side": t.get("side"),
                       "size": _f(t.get("size")), "entry": _f(t.get("entry")), "exit": _f(t.get("exit")),
                       "stop": _f(t.get("stop")), "take_profit": _f(t.get("take_profit")),
                       "gross_pnl": _f(t.get("gross_pnl")), "costs": costs, "pnl": _f(t.get("pnl")),
                       "r": _f(t.get("r_multiple")), "mfe_r": _f(t.get("mfe_r")), "mae_r": _f(t.get("mae_r")),
                       "exit_reason": t.get("exit_reason"), "planned_risk": _f(t.get("planned_risk")),
                       "loss_beyond_planned": _f(t.get("loss_beyond_planned"))}
                if strategy and row["strategy"] != strategy or status and row["exit_reason"] != status:
                    continue
                if _in_range(row["at"], start, end) and _match(q, row["signal_id"], row["book"], row["exit_reason"]):
                    rows.append(row)
        elif kind == "events":
            for e in store.events():
                detail = {k: v for k, v in e.items() if k not in ("at", "kind")}
                row = {"at": e["at"], "event": e["kind"], "class": _event_class(e["kind"], detail),
                       "detail": json.dumps(detail, default=str)[:400]}
                if status and row["class"] != status:
                    continue
                if _in_range(row["at"], start, end) and _match(q, row["event"], row["detail"]):
                    rows.append(row)
        else:
            raise ValueError(f"unknown history kind {kind!r}")
    rows.sort(key=lambda x: x["at"] or "", reverse=True)
    return {**base, "available": True, "total": len(rows), "offset": offset, "rows": rows[offset:offset + limit]}


def _downsample(points: list, max_points: int = 600) -> list:
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    picked = [points[int(i * step)] for i in range(max_points)]
    return picked[:-1] + [points[-1]]


def equity(cfg: dict, runner_id: str, *, strategy: str | None = None) -> dict:
    """Simulated equity and drawdown (derived) per book of a runner, downsampled for display."""
    r = runners(cfg).get(runner_id)
    if r is None:
        raise KeyError(runner_id)
    if not r["db"].exists():
        return {"runner": runner_id, "available": False, "books": []}
    out = []
    with ReadOnlyShadowStore(r["db"]) as store:
        trades_by_book: dict[str, list] = {}
        for t in store.trades():
            trades_by_book.setdefault(t["book_id"], []).append(t)
        for book_id, info in sorted(store.books().items()):
            if strategy and info["strategy_key"] != strategy:
                continue
            initial = float(info["initial_equity"])
            # Realized curve: initial equity + cumulative net P&L of this book's recorded closed trades.
            realized, cum = [], 0.0
            for t in sorted(trades_by_book.get(book_id, []), key=lambda x: x["closed_at"]):
                cum += float(t["pnl"])
                realized.append({"time": int(parse_time(t["closed_at"]).timestamp()), "value": initial + cum})
            # Marks: equity recorded at bar closes while a position was open (includes unrealized P&L).
            marks = [{"time": int(parse_time(at).timestamp()), "value": float(v)} for at, v in store.equity_curve(book_id)]
            merged = {p["time"]: p["value"] for p in marks}
            merged.update({p["time"]: p["value"] for p in realized})  # closed-trade values win at equal times
            points = [{"time": t, "value": v} for t, v in sorted(merged.items())]
            peak, dd = initial, []
            for p in points:
                peak = max(peak, p["value"])
                dd.append({"time": p["time"], "value": round(-100 * (peak - p["value"]) / peak, 4) if peak else 0.0})
            out.append({"book": book_id, "strategy": info["strategy_key"], "initial_equity": initial,
                        "commission": float(info["commission"]), "points": len(points), "trades": len(realized),
                        "marks": len(marks), "equity": _downsample(points), "realized": _downsample(realized),
                        "drawdown_pct": _downsample(dd), "last": points[-1]["value"] if points else initial,
                        "realized_pnl": cum, "max_drawdown_pct": min((p["value"] for p in dd), default=0.0)})
    return {"runner": runner_id, "available": True, "books": out, "kind": "simulated",
            "note": ("Each book is one alternative simulation (one equity size x one cost scenario) of the same "
                     "signals. Books are shown one at a time and never summed. Curve = initial equity + recorded "
                     "closed-trade P&L, plus equity marks recorded at bar closes while a position was open.")}


def signal_detail(cfg: dict, runner_id: str, signal_id: str) -> dict:
    """Everything recorded about one signal: market context, provenance, every book's risk decision, simulated
    executions with costs, the chart around it, and any Grok advisory review of it."""
    from trading.shadow.report import reason_key

    r = runners(cfg).get(runner_id)
    if r is None or not r["db"].exists():
        raise KeyError(runner_id)
    with ReadOnlyShadowStore(r["db"]) as store:
        sig = next((s for s in store.signals() if s["signal_id"] == signal_id), None)
        if sig is None:
            raise KeyError(signal_id)
        books = store.books()
        decisions = [d for d in store.decisions() if d["signal_id"] == signal_id]
        trades = [t for t in store.trades() if t["signal_id"] == signal_id]
        positions = {b: p for b, p in store.positions().items() if p.get("signal_id") == signal_id}
    body = sig["body"]
    prov = body.get("provenance") or {}
    interval = prov.get("interval") or r["interval"] or "15m"
    if interval not in TIMEFRAMES:
        interval = r["interval"] or "15m"
    seconds = tf_seconds(interval)
    close = parse_time(sig["bar_close"])
    until = (close + timedelta(seconds=seconds * 60)).isoformat()
    market = cfg["markets"][r["market"]]
    data = chart(cfg, r["market"], interval, limit=180, until=until, strategies=[sig["strategy_key"]])
    if r["db"] != Path(market["state_dir"]) / "shadow.sqlite":  # Strategy A: overlays live in its own store
        data["notes"].append("Overlays come from the strategy's own runner store.")
    reviews = []
    subject_suffix = f":{signal_id}"
    for room_store in grok_room(cfg)["stores"]:
        reviews += [s for s in room_store["summaries"] if s["subject"].endswith(subject_suffix)]
    return {
        "runner": runner_id, "label": r["label"], "signal_id": signal_id, "strategy": sig["strategy_key"],
        "status": sig["status"], "bar_close": sig["bar_close"], "side": body.get("side"),
        "entry": _f(body.get("entry_price")), "stop": _f(body.get("stop_price")),
        "take_profit": _f(body.get("take_profit")), "generated_at": body.get("generated_at"),
        "strategy_version": body.get("strategy_version"), "interval": interval,
        "context": prov.get("context"), "params_hash": prov.get("params_hash"), "dataset_id": prov.get("dataset_id"),
        "decisions": [{"book": d["book_id"], "approved": d["approved"], "at": d["evaluated_at"], "size": _f(d["size"]),
                       "planned_risk": _f(d["planned_risk"]), "reasons": d["reasons"],
                       "categories": sorted({reason_key(x) for x in d["reasons"]}),
                       "equity_at_decision": _f((d.get("context") or {}).get("equity")),
                       "initial_equity": float(books[d["book_id"]]["initial_equity"]) if d["book_id"] in books else None}
                      for d in decisions],
        "trades": [{"book": t["book_id"], "side": t.get("side"), "size": _f(t.get("size")), "opened_at": t.get("opened_at"),
                    "closed_at": t.get("closed_at"), "entry": _f(t.get("entry")), "exit": _f(t.get("exit")),
                    "gross_pnl": _f(t.get("gross_pnl")), "spread_slippage_cost": _f(t.get("spread_slippage_cost")),
                    "commission": _f(t.get("commission")), "swap": _f(t.get("swap")), "pnl": _f(t.get("pnl")),
                    "r": _f(t.get("r_multiple")), "mfe_r": _f(t.get("mfe_r")), "mae_r": _f(t.get("mae_r")),
                    "exit_reason": t.get("exit_reason")} for t in trades],
        "open_positions": [{"book": b, "entry": _f(p.get("entry")), "stop": _f(p.get("stop")),
                            "take_profit": _f(p.get("take_profit")), "opened_at": p.get("opened_at")}
                           for b, p in positions.items()],
        "chart": data, "grok_reviews": reviews,
        "evidence_note": ("Warm-up signal: generated on history before the forward-test boundary; it is not evidence "
                          "and no book acted on it.") if sig["status"] == "warmup" else None}


# --------------------------------------------------------------------------- Monte Carlo research reports

_MC_ID = __import__("re").compile(r"^[a-z0-9_]+__[a-z0-9_]+__(historical_holdout|development|forward)$")


def _mc_dir(cfg: dict) -> Path | None:
    d = (cfg.get("montecarlo") or {}).get("reports_dir")
    return Path(d) if d else None


def montecarlo_index(cfg: dict) -> dict:
    """List the precomputed Monte Carlo reports (read-only; the dashboard never runs a simulation)."""
    d = _mc_dir(cfg)
    if d is None or not (d.parent / "index.json").exists():
        return {"available": False, "reports": [],
                "note": "No Monte Carlo reports have been generated (python -m research.montecarlo run)."}
    idx = json.loads((d.parent / "index.json").read_text(encoding="utf-8"))
    return {"available": True, "algorithm": idx.get("algorithm"), "updated_at": idx.get("updated_at"),
            "reports": [r for r in idx["reports"] if _MC_ID.match(r["id"])]}


def montecarlo_report(cfg: dict, report_id: str) -> dict:
    d = _mc_dir(cfg)
    if d is None or not _MC_ID.match(report_id):
        raise KeyError(report_id)
    path = d / f"{report_id}.json"
    if not path.is_file():
        raise KeyError(report_id)
    rep = json.loads(path.read_text(encoding="utf-8"))
    rep.get("dataset", {}).pop("trades", None)  # reports never carry trade lists; defensive
    return rep


# --------------------------------------------------------------------------- response hygiene

def _sensitive_roots(cfg: dict) -> list[tuple[str, str]]:
    roots = []
    for name, m in cfg["markets"].items():
        roots += [(m["state_dir"], f"<{name} state>"), (m["repo"], f"<{name} checkout>")]
        roots += [(p, f"<{name} history>") for p in m.get("history", {}).values()]
    for s in cfg["strategies"]:
        for key in ("state_dir", "repo"):
            if s.get(key):
                roots.append((s[key], f"<{s['id']} {key}>"))
    g = cfg.get("grok", {})
    roots += [(d, "<advisory store>") for d in g.get("advisory_dirs", [])]
    if (cfg.get("montecarlo") or {}).get("reports_dir"):
        roots.append((cfg["montecarlo"]["reports_dir"], "<montecarlo reports>"))
    if g.get("config"):
        roots.append((g["config"], "<grok config>"))
    roots.append((str(Path.home()), "<home>"))
    out = []
    for path, label in roots:
        for variant in {path, path.replace("/", "\\"), path.replace("\\", "/"), path.replace("\\", "\\\\")}:
            out.append((variant, label))
    return sorted(out, key=lambda x: -len(x[0]))  # longest first


def scrub(value, cfg: dict):
    """Remove local filesystem paths (and the user's home directory) from any response value."""
    text = json.dumps(value, default=str)
    if not any(v in text for v, _ in _sensitive_roots(cfg)):
        return value
    for variant, label in _sensitive_roots(cfg):
        text = text.replace(json.dumps(variant)[1:-1], label)
    return json.loads(text)
