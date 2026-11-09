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

    def __init__(self):
        self._items: dict = {}

    def get(self, key, ttl: float, fn):
        hit = self._items.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        value = fn()
        self._items[key] = (time.monotonic(), value)
        return value


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
        markets[name] = market_status(name, m, now=now)
        health[name] = market_health(name, Path(m["state_dir"]), Path(m["repo"]), m["config"], now=now, hours=hours)
        risk[name] = risk_status(cfg, name, m, now)
    strategies = [strategy_panel(cfg, s, now=now) for s in cfg["strategies"]]
    accounts = []
    for name, h in health.items():
        for book_id, b in sorted((h.get("books") or {}).items()):
            accounts.append({"market": name, "book": book_id, **b, "kind": "simulated"})
    alerts = [{"market": n, "text": a} for n, h in health.items() for a in h.get("alerts", [])]
    return {"generated_at": _iso(now), "read_only": True, "orders_possible": False, "markets": markets,
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


def chart(cfg: dict, market_name: str, tf: str, *, limit: int = 500, until: str | None = None,
          strategies: list[str] | None = None) -> dict:
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
    data["signals"], data["trades"], data["positions"], data["opening_ranges"] = [], [], [], []
    if not candles:
        return data
    t0, t1 = candles[0]["time"], candles[-1]["time"] + seconds
    keys = {s["strategy_key"]: s for s in cfg["strategies"] if s["market"] == market_name}
    for s in cfg["strategies"]:
        if s["market"] == market_name:
            keys.update({c: {**s, "control": True} for c in s.get("controls", [])})
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
                close = parse_time(s["bar_close"])
                marker_t = int(floor_time(close - timedelta(seconds=1), seconds).timestamp())
                if not t0 <= marker_t < t1:
                    continue
                body = s["body"]
                ds = decisions.get(s["signal_id"], [])
                data["signals"].append({
                    "time": marker_t, "bar_close": s["bar_close"], "signal_id": s["signal_id"],
                    "strategy": s["strategy_key"], "control": bool(keys.get(s["strategy_key"], {}).get("control")),
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
    room["status"] = ("active" if room["activated"] and room["env_activation_flag"] and room["xai_key_present"]
                      else "not activated")
    return room


def timeframes() -> list[str]:
    return list(TIMEFRAMES)
