"""Live decision status, alert classification and recent activity (read-only).

Answers "why is Kronos monitoring but not trading?" from recorded data and the strategies' own frozen rules only:

  observed - the runner's latest heartbeat (connection, data freshness, market open, guard results), STOP/KILL/breaker
             files, recorded signals, decisions, trades and events;
  derived  - (a) session windows computed from the strategy's frozen parameters and the same calendar rules the
             runner uses (New York gold market hours + CME gold holidays/early closes via pandas_market_calendars);
             (b) the strategy's current setup, obtained by replaying its *frozen code* (the same class, the same
             pre-registered parameters) over the bars the runner itself recorded. The replay never writes anything
             and never produces a signal of record: only the runner's store does that.

Nothing here predicts when a trade will happen. A state is only reported when the inputs needed for it exist;
otherwise it is "unknown"/"unavailable".
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from command_center.bars import parse_time
from command_center.readonly import connect_ro

UTC = timezone.utc
TICK = Decimal("0.01")  # replay only; the strategies' on_bar logic does not use the tick size
CALENDAR_DAYS = 21


def _iso(t: datetime | None) -> str | None:
    return None if t is None else t.astimezone(UTC).isoformat()


# --------------------------------------------------------------------------- calendars (derived, cached)

def xau_calendar(today: date, cache) -> dict:
    """Rule-based XAUUSD calendar: NY gold market hours + CME gold holidays/early closes. The runner builds the same
    calendar and additionally intersects it with the broker's observed sessions ("broker_verified" in heartbeats)."""
    def build():
        try:
            from trading.marketcal.holidays import build_xauusd_calendar
            from trading.sessions import XAUUSD_NY_MARKET_HOURS
            cal, prov = build_xauusd_calendar(XAUUSD_NY_MARKET_HOURS, today - timedelta(days=3),
                                              today + timedelta(days=CALENDAR_DAYS + 3))
            return {"calendar": cal, "source": "NY gold market hours + CME gold holiday rules "
                    f"({prov['provider']} {prov['version']}, {prov['calendar']})",
                    "holidays": prov["holidays"], "early_closes": prov["early_closes"], "error": None}
        except Exception as exc:  # noqa: BLE001 - shown as unavailable, never guessed
            return {"calendar": None, "source": None, "holidays": [], "early_closes": [],
                    "error": f"holiday calendar unavailable ({type(exc).__name__})"}
    return cache.get(("xau_calendar", today.isoformat()), 6 * 3600, build)


def _runner_config(runner: dict) -> dict:
    try:
        return json.loads((Path(runner["repo"]) / runner["config"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def orb_windows(params: dict, now: datetime, cal) -> list[dict]:
    """Opening-range and entry windows of the NY ORB for the coming trading days, from the frozen parameters.
    A day is listed only if the calendar has the market open for the whole opening range."""
    from trading.strategies.xauusd import SESSIONS

    tz, open_minute = SESSIONS[params.get("session", "newyork")]
    rng, entry = int(params.get("range_minutes", 30)), int(params.get("entry_window_minutes", 180))
    local_today = tz.to_server(now).date()
    out = []
    for i in range(-1, CALENDAR_DAYS):
        d = local_today + timedelta(days=i)
        start = tz.to_utc(datetime(d.year, d.month, d.day) + timedelta(minutes=open_minute))
        start = start if start.tzinfo else start.replace(tzinfo=UTC)
        range_end = start + timedelta(minutes=rng)
        entry_end = range_end + timedelta(minutes=entry)
        if entry_end <= now:
            continue
        if cal is not None:
            if not (cal.is_open(start) and cal.is_open(range_end - timedelta(minutes=1))):
                continue  # weekend, holiday or closure: no opening range can form
            shortened = not cal.is_open(entry_end - timedelta(minutes=1))
        else:
            if d.weekday() >= 5:
                continue
            shortened = None
        out.append({"date": d.isoformat(), "range_from": _iso(start), "range_to": _iso(range_end),
                    "entry_from": _iso(range_end), "entry_to": _iso(entry_end), "shortened_by_closure": shortened,
                    "tz": "America/New_York", "local_open": f"{open_minute // 60:02d}:{open_minute % 60:02d}"})
        if len(out) >= 3:
            break
    return out


def market_session(cal, now: datetime) -> dict | None:
    """Whether the gold market is open now and the next open/close boundary (calendar, derived)."""
    if cal is None:
        return None
    is_open = cal.is_open(now)
    t = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = now + timedelta(days=10)
    while t < limit and cal.is_open(t) == is_open:
        t += timedelta(minutes=15 if (t.minute % 15 == 0) else 1)
    return {"open": is_open, "next_change": _iso(t) if t < limit else None,
            "next_change_kind": "close" if is_open else "open"}


# --------------------------------------------------------------------------- recorded bars and frozen-code replay

def _recent_bars(db_path: Path, symbol: str, limit: int):
    from trading.marketdata import Bar

    con = connect_ro(db_path)
    try:
        rows = con.execute("SELECT open_time, open, high, low, close, volume, interval FROM bars "
                           "ORDER BY open_time DESC LIMIT ?", (limit,)).fetchall()
    finally:
        con.close()
    return [Bar(symbol=symbol, interval=r[6], open_time=datetime.fromisoformat(r[0]), open=Decimal(r[1]),
                high=Decimal(r[2]), low=Decimal(r[3]), close=Decimal(r[4]), volume=Decimal(r[5]))
            for r in reversed(rows)]


def replay_orb(bars, params: dict, now: datetime) -> dict:
    """Run the frozen OpeningRangeBreakout over the recorded bars and report its state after the last bar."""
    from trading.strategies.xauusd import OpeningRangeBreakout, OpeningRangeParams

    strat = OpeningRangeBreakout(OpeningRangeParams(**params), tick_size=TICK, dataset_id="dashboard-replay")
    drafts = []
    for bar in bars:
        d = strat.on_bar(bar)
        if d is not None:
            drafts.append((bar, d))
    if not bars:
        return {"available": False, "note": "No recorded bars."}
    last = bars[-1]
    atr = strat.atr.value
    p = strat.params
    out = {"available": True, "kind": "derived (frozen strategy code replayed on the runner's recorded bars)",
           "last_bar_open": _iso(last.open_time), "last_bar_close": _iso(last.close_time),
           "bars_replayed": len(bars), "session_day": strat.day.isoformat() if strat.day else None,
           "atr": atr, "range_low": strat.range_low, "range_high": strat.range_high,
           "signal_today": strat.traded}
    if strat.range_high is not None and atr:
        width = strat.range_high - strat.range_low
        ratio = width / atr
        out.update(range_width=width, width_atr=ratio, min_range_atr=p.min_range_atr, max_range_atr=p.max_range_atr,
                   range_filter_ok=p.min_range_atr <= ratio <= p.max_range_atr,
                   long_trigger=strat.range_high + p.breakout_buffer_atr * atr,
                   short_trigger=strat.range_low - p.breakout_buffer_atr * atr,
                   buffer_atr=p.breakout_buffer_atr)
    if drafts:
        bar, d = drafts[-1]
        out["last_replayed_setup"] = {"bar_close": _iso(bar.close_time), "side": d.side.value}
    stale = now - last.close_time
    out["bars_age_s"] = round(stale.total_seconds())
    return out


def replay_donchian(bars, params: dict) -> dict:
    """Run the frozen DonchianBreakout over the recorded bars; the channel high it holds after the last bar is the
    level the next hourly close must exceed (long-only)."""
    from trading.strategies.crypto import DonchianBreakout, DonchianParams

    strat = DonchianBreakout(DonchianParams(**params), tick_size=TICK, dataset_id="dashboard-replay")
    last_draft = None
    for bar in bars:
        d = strat.on_bar(bar)
        if d is not None:
            last_draft = (bar, d)
    if not bars or not strat.highs.full:
        return {"available": False, "note": "Not enough recorded bars for the channel."}
    last = bars[-1]
    level = max(strat.highs.values)
    out = {"available": True, "kind": "derived (frozen strategy code replayed on the runner's recorded bars)",
           "lookback": strat.params.entry_lookback, "channel_high": level, "last_close": float(last.close),
           "last_bar_close": _iso(last.close_time), "atr": strat.atr.value, "bars_replayed": len(bars),
           "distance": level - float(last.close),
           "distance_pct": 100 * (level - float(last.close)) / float(last.close)}
    if last_draft:
        out["last_replayed_setup"] = {"bar_close": _iso(last_draft[0].close_time), "side": last_draft[1].side.value}
    return out


# --------------------------------------------------------------------------- economic-event blackouts (observed schedule)

def upcoming_blackouts(state_dir: Path, market_key: str, now: datetime, days: int = 7) -> dict:
    """Event blackout windows from the runner's own calendar store (official providers) and the risk engine's
    fixed blackout rules. Read-only."""
    from trading.common import Market
    from trading.marketcal.blackout import DEFAULT_RULES
    from trading.marketcal.events import EconomicEvent

    path = state_dir / "calendar.sqlite"
    if not path.exists():
        return {"available": False, "windows": []}
    market = Market.XAUUSD_MT5 if market_key == "xauusd" else Market.BTCUSDT_BINANCE_SPOT
    rules = DEFAULT_RULES[market]
    con = connect_ro(path)
    try:
        rows = con.execute("SELECT body FROM events WHERE scheduled_at >= ? AND scheduled_at <= ? ORDER BY scheduled_at",
                           ((now - timedelta(days=1)).isoformat(timespec="seconds"),
                            (now + timedelta(days=days)).isoformat(timespec="seconds"))).fetchall()
        refreshed = con.execute("SELECT MAX(refreshed_at) FROM refreshes").fetchone()[0]
    finally:
        con.close()
    windows = []
    for (body,) in rows:
        try:
            ev = EconomicEvent.from_json(json.loads(body))
        except (ValueError, TypeError, KeyError):
            continue
        matching = [r for r in rules if r.matches(ev)]
        if not matching:
            continue
        if ev.all_day:
            start = ev.scheduled_at.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
        else:
            start = ev.scheduled_at - max(r.before for r in matching)
            end = ev.scheduled_at + max(r.after for r in matching)
        if end < now:
            continue
        windows.append({"event": ev.name, "at": _iso(ev.scheduled_at), "from": _iso(start), "to": _iso(end),
                        "provider": ev.provider, "category": ev.category, "all_day": ev.all_day,
                        "active": start <= now <= end})
    return {"available": True, "windows": windows[:12], "calendar_refreshed_at": refreshed,
            "kind": "observed schedule (official providers) + fixed blackout rules"}


# --------------------------------------------------------------------------- decision status

STATES = {
    "not_launched": "Not launched",
    "unavailable": "Data unavailable",
    "runner_down": "Runner not responding",
    "blocked": "Blocked by a deterministic guard",
    "in_position": "Simulated position open",
    "waiting_for_session": "Waiting for its session",
    "monitoring": "Monitoring (building the opening range)",
    "waiting_for_signal": "Waiting for a valid signal",
}


def _check(name: str, status: str, detail: str, kind: str) -> dict:
    return {"check": name, "status": status, "detail": detail, "kind": kind}


def _last_signals(db_path: Path, key: str) -> dict:
    con = connect_ro(db_path)
    try:
        fwd = con.execute("SELECT signal_id, bar_close, status FROM signals WHERE strategy_key=? AND status!='warmup' "
                          "ORDER BY bar_close DESC LIMIT 1", (key,)).fetchone()
        warm = con.execute("SELECT signal_id, bar_close, COUNT(*) OVER () FROM signals WHERE strategy_key=? AND "
                           "status='warmup' ORDER BY bar_close DESC LIMIT 1", (key,)).fetchone()
        n_fwd = con.execute("SELECT COUNT(*) FROM signals WHERE strategy_key=? AND status!='warmup'", (key,)).fetchone()[0]
    finally:
        con.close()
    return {"forward": None if not fwd else {"signal_id": fwd[0], "bar_close": fwd[1], "status": fwd[2]},
            "forward_count": n_fwd,
            "warmup": None if not warm else {"signal_id": warm[0], "bar_close": warm[1]},
            "warmup_count": warm[2] if warm else 0}


def strategy_decision(cfg: dict, strategy: dict, market: dict, panel: dict, now: datetime, cache) -> dict:
    from command_center.services import _strategy_runner

    runner = _strategy_runner(cfg, strategy)
    state_dir = Path(runner["state_dir"])
    db_path = state_dir / "shadow.sqlite"
    run_cfg = _runner_config(runner)
    key = strategy["strategy_key"]
    scfg = (run_cfg.get("strategies") or run_cfg.get("candidates") or {}).get(key, {})
    params = scfg.get("params", {})
    out = {"id": strategy["id"], "name": strategy["name"], "market": strategy["market"], "strategy_key": key,
           "generated_at": _iso(now), "checks": [], "session": None, "setup": None, "blackouts": None,
           "signals": None}

    # -- session / evaluation schedule (derived from frozen params + calendar) -------------------------------------
    if strategy["market"] == "xauusd":
        calinfo = xau_calendar(now.date(), cache)
        cal = calinfo["calendar"]
        msess = market_session(cal, now)
        if key.startswith("orb"):
            windows = orb_windows(params, now, cal)
            current = windows[0] if windows else None
            phase = None
            if current:
                rf, rt, et = (parse_time(current[k]) for k in ("range_from", "range_to", "entry_to"))
                phase = ("before_range" if now < rf else "building_range" if now < rt else
                         "entry_window" if now < et else "after_window")
            out["session"] = {"kind": "derived", "type": "orb", "phase": phase, "windows": windows,
                              "market": msess, "calendar_source": calinfo["source"], "error": calinfo["error"],
                              "rule": (f"Opening range: M15 bars opening {params.get('range_minutes', 30)} min from "
                                       f"08:20 New York time; entries only on an M15 close within the next "
                                       f"{params.get('entry_window_minutes', 180)} min; one signal per session-day."),
                              "holidays": calinfo["holidays"], "early_closes": calinfo["early_closes"]}
        else:
            out["session"] = {"kind": "derived", "type": "market_hours", "market": msess,
                              "calendar_source": calinfo["source"], "error": calinfo["error"],
                              "rule": "Evaluated on every closed bar while the gold market is open (no extra "
                                      "session filter in the frozen rules).",
                              "holidays": calinfo["holidays"], "early_closes": calinfo["early_closes"]}
    else:
        interval = timedelta(hours=1)
        nxt = now.replace(minute=0, second=0, microsecond=0) + interval
        out["session"] = {"kind": "derived", "type": "continuous", "market": {"open": True, "next_change": None},
                          "next_evaluation": _iso(nxt),
                          "rule": "BTC/USDT trades 24/7: the strategy evaluates every closed 1-hour bar (long-only)."}

    if panel.get("lifecycle") == "prepared" or not db_path.exists():
        out.update(state="not_launched", headline="Prepared, not launched: not monitoring",
                   explanation=("No continuous runner has been launched for this strategy, so it is not evaluating "
                                "any market data. Launch requires separate approval."))
        out["checks"].append(_check("Runner", "info", "not launched (no runner state exists)", "observed"))
        return out
    if panel.get("lifecycle") == "unavailable" or not market.get("available", False):
        out.update(state="unavailable", headline="The runner's records could not be read",
                   explanation="Nothing is inferred while the database is unreadable; the dashboard retries.")
        return out

    # -- observed runner status -------------------------------------------------------------------------------------
    hb_age = market.get("last_heartbeat_age_s")
    runner_ok = market.get("runner") == "running"
    out["checks"].append(_check("Runner heartbeat", "ok" if runner_ok else "blocked",
                                f"{market.get('runner')} · last {hb_age if hb_age is not None else '?'} s ago",
                                "observed"))
    data = market.get("data")
    data_ok = data in ("fresh", "ok")
    out["checks"].append(_check("Market data", "ok" if data_ok else ("info" if data == "market closed" else "blocked"),
                                f"{data} · {market.get('connection')}", "observed"))
    sess = out["session"] or {}
    msess = sess.get("market") or {}
    if strategy["market"] == "xauusd":
        hb_open = market.get("market_open")
        detail = ("open" if hb_open else "closed") + " (runner, broker-verified)" if hb_open is not None else "unknown"
        if msess.get("next_change"):
            detail += f" · next {msess['next_change_kind']} {msess['next_change']}"
        out["checks"].append(_check("Market session", "ok" if hb_open else "info", detail, "observed"))
    reasons = list(market.get("entry_block_reasons") or [])
    controls = panel.get("controls") or {}
    if controls.get("stop_file"):
        reasons.insert(0, "STOP file present")
    if controls.get("kill_file"):
        reasons.insert(0, "KILL file present")
    reasons += [f"breaker: {t}" for t in controls.get("breaker_trips") or []]
    out["checks"].append(_check("Deterministic guards", "blocked" if reasons else "ok",
                                "; ".join(reasons) if reasons else "all pass (no new-entry block reported)",
                                "observed"))
    out["blocking_reasons"] = reasons
    out["blackouts"] = upcoming_blackouts(state_dir, strategy["market"], now)

    # -- strategy setup (frozen-code replay) ------------------------------------------------------------------------
    setup = None
    try:
        if key.startswith("orb"):
            setup = cache.get(("orb_replay", str(db_path), market.get("last_bar_open")), 60,
                              lambda: replay_orb(_recent_bars(db_path, "XAUUSD", 600), params, now))
        elif key == "donchian":
            setup = cache.get(("don_replay", str(db_path), market.get("last_bar_open")), 60,
                              lambda: replay_donchian(_recent_bars(db_path, "BTCUSDT", 400), params))
    except Exception as exc:  # noqa: BLE001 - shown as unavailable
        setup = {"available": False, "note": f"replay unavailable ({type(exc).__name__})"}
    out["setup"] = setup
    sigs = _last_signals(db_path, key)
    out["signals"] = sigs

    # -- state -------------------------------------------------------------------------------------------------------
    lifecycle = panel.get("lifecycle")
    open_pos = panel.get("open_positions") or {}
    if lifecycle == "stopped" or not runner_ok and market.get("runner") == "not responding":
        state, headline = "runner_down", "No recent heartbeat: the runner is not evaluating"
        expl = "Heartbeats stopped; nothing is evaluated until the runner (supervised by its scheduled task) resumes."
    elif controls.get("kill_file") or controls.get("stop_file") or controls.get("breaker_trips"):
        state, headline = "blocked", "Blocked: " + reasons[0]
        expl = "A safety control is active; new simulated entries are rejected."
    elif open_pos:
        state, headline = "in_position", f"{len(open_pos)} simulated position(s) open"
        expl = "Signals are being managed in the shadow books; no order exists anywhere."
    elif key.startswith("orb"):
        phase = sess.get("phase")
        w = (sess.get("windows") or [None])[0]
        if market.get("market_open") is False:
            state, headline = "waiting_for_session", "Market closed: waiting for the next session"
            expl = "The gold market is closed (runner heartbeat); the opening range forms on the next trading day."
        elif phase == "before_range":
            state, headline = "waiting_for_session", "Waiting for the New York opening range"
            expl = (f"The opening range forms {w['range_from']} → {w['range_to']} (08:20 New York time). No entry is "
                    "possible before the range has formed.")
        elif phase == "building_range":
            state, headline = "monitoring", "Building today's opening range"
            expl = "Recording the high and low of the opening-range bars; entries start when the range is complete."
        elif phase == "entry_window":
            if reasons:
                state, headline = "blocked", "Entry window open, but a guard blocks new entries"
                expl = "; ".join(reasons)
            elif setup and setup.get("signal_today"):
                state, headline = "waiting_for_session", "Today's signal already generated (one per session-day)"
                expl = "The frozen rules allow one signal per session-day; the next opportunity is the next session."
            elif setup and setup.get("range_filter_ok") is False:
                state, headline = "waiting_for_session", "No setup today: opening range outside the volatility filter"
                expl = (f"Range width {setup['width_atr']:.2f}× ATR is outside the required "
                        f"{setup['min_range_atr']}–{setup['max_range_atr']}× ATR, so the frozen rules take no trade "
                        "today.")
            else:
                state, headline = "waiting_for_signal", "Entry window open: waiting for a breakout close"
                expl = ("A signal requires an M15 close above the long trigger or below the short trigger "
                        "(range ± 0.1 × ATR)." if setup and setup.get("long_trigger") else
                        "Waiting for an M15 close beyond the opening range plus buffer.")
        else:
            state, headline = "waiting_for_session", "Today's entry window has ended"
            expl = "The next opening range forms in the next session window shown below."
    else:  # continuous (Donchian)
        if reasons:
            state, headline = "blocked", "A guard blocks new entries"
            expl = "; ".join(reasons)
        else:
            state, headline = "waiting_for_signal", "Monitoring every hourly close for a channel breakout"
            expl = ("A signal requires a 1-hour close above the highest high of the previous "
                    f"{params.get('entry_lookback', '?')} bars (long-only)." )
    if state in ("waiting_for_signal", "monitoring") and reasons:
        state = "blocked"
    out.update(state=state, state_label=STATES[state], headline=headline, explanation=expl)
    # Session / setup checks for the checklist
    if key.startswith("orb"):
        w = (sess.get("windows") or [None])[0]
        out["checks"].append(_check("Strategy session window",
                                    "ok" if sess.get("phase") in ("building_range", "entry_window") else "info",
                                    f"{sess.get('phase') or 'unknown'}" + (f" · next range {w['range_from']}" if w else ""),
                                    "derived"))
    if setup and setup.get("available"):
        if key.startswith("orb") and setup.get("width_atr") is not None:
            out["checks"].append(_check("Setup condition",
                                        "ok" if setup.get("range_filter_ok") else "info",
                                        f"range {setup['range_low']:.2f}–{setup['range_high']:.2f} "
                                        f"({setup['width_atr']:.2f}× ATR; filter {setup['min_range_atr']}–"
                                        f"{setup['max_range_atr']}×) for session-day {setup['session_day']}",
                                        "derived"))
        elif key == "donchian":
            out["checks"].append(_check("Setup condition", "info",
                                        f"next close must exceed {setup['channel_high']:.2f} "
                                        f"(last close {setup['last_close']:.2f}, {setup['distance_pct']:.2f}% below)",
                                        "derived"))
    return out


def decision_status(cfg: dict, markets: dict, panels: list[dict], now: datetime, cache) -> list[dict]:
    out = []
    by_id = {p["id"]: p for p in panels}
    for s in cfg["strategies"]:
        try:
            out.append(strategy_decision(cfg, s, markets.get(s["market"], {}), by_id.get(s["id"], {}), now, cache))
        except Exception as exc:  # noqa: BLE001 - one strategy's problem never hides the others
            out.append({"id": s["id"], "name": s["name"], "market": s["market"], "strategy_key": s["strategy_key"],
                        "state": "unavailable", "state_label": STATES["unavailable"],
                        "headline": "Decision status unavailable", "explanation": type(exc).__name__,
                        "checks": []})
    return out


# --------------------------------------------------------------------------- alerts: active vs historical

def _events_since(db_path: Path, since: datetime) -> list[tuple[str, str, dict]]:
    con = connect_ro(db_path)
    try:
        rows = con.execute("SELECT at, kind, detail FROM events WHERE at >= ? ORDER BY id",
                           (since.isoformat(),)).fetchall()
    finally:
        con.close()
    out = []
    for at, kind, detail in rows:
        try:
            out.append((at, kind, json.loads(detail)))
        except ValueError:
            out.append((at, kind, {}))
    return out


def gap_evidence(db_path: Path, gap: dict, events: list) -> str | None:
    """What the runner itself recorded at the end of a heartbeat gap (never a guess): a restart, or a first heartbeat
    reporting stale market data (= its polling loop was blocked, not the machine)."""
    end = parse_time(gap["to"])
    for at, kind, _ in events:
        if kind == "start" and abs((parse_time(at) - end).total_seconds()) <= 120:
            return f"runner (re)started at {at} (start event recorded)"
    try:
        con = connect_ro(db_path)
        try:
            row = con.execute("SELECT detail FROM heartbeats WHERE at = ?", (gap["to"],)).fetchone()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    text = row[0]
    if "stale" in text:
        detail = json.loads(text)
        reason = detail.get("guard") or "; ".join(detail.get("entry_block_reasons") or []) or "market data stale"
        return (f"first heartbeat after the gap recorded '{reason}': the runner's polling loop was blocked "
                "(no restart recorded)")
    return "no cause recorded by the runner (no restart recorded; next heartbeat normal)"


CONNECTION_EVENTS = {"data_interruption", "mt5_error", "reconnect", "quote_source", "kline_gap"}


def alert_groups(cfg: dict, markets: dict, health: dict, risk: dict, now: datetime) -> dict:
    """Separate what needs attention now from what already resolved itself.

    active      - true right now (files present, runner down, data stale while open, guard failures that persist,
                  integrity problems, unresolved calendar/reconciliation failures, risk exceptions in the window);
    connection  - temporary disconnections/interruptions in the window (recovered if the runner is connected now);
    resolved    - warnings in the window that were followed by recovery (heartbeat gaps that ended, calendar refresh
                  or reconciliation failures followed by a success, housekeeping errors, clock offsets);
    stop_kill   - past STOP/KILL events whose file is no longer present (operator tests or stops; not active)."""
    hours = float(cfg.get("health_window_hours", 24))
    since = now - timedelta(hours=hours)
    groups = {"active": [], "connection": [], "resolved": [], "stop_kill": [], "window_hours": hours}
    for name, m in cfg["markets"].items():
        label = m["label"]
        ms = markets.get(name, {})
        h = health.get(name, {})
        r = risk.get(name, {})
        controls = ms.get("controls") or r.get("controls") or {}
        if not ms.get("available", False):
            groups["active"].append({"market": name, "text": f"{label}: runner data unavailable ({ms.get('runner')})",
                                     "severity": "high"})
            continue
        if ms.get("runner") != "running":
            groups["active"].append({"market": name, "severity": "high",
                                     "text": f"Runner {ms.get('runner')} (last heartbeat {ms.get('last_heartbeat_age_s')} s ago)"})
        if controls.get("kill_file"):
            groups["active"].append({"market": name, "severity": "high", "text": "KILL file present"})
        if controls.get("stop_file"):
            groups["active"].append({"market": name, "severity": "high", "text": "STOP file present"})
        for t in controls.get("breaker_trips") or []:
            groups["active"].append({"market": name, "severity": "high", "text": f"Circuit breaker tripped: {t}"})
        for acct in r.get("halted_accounts_today") or []:
            groups["active"].append({"market": name, "severity": "high", "text": f"Daily loss halt: {acct}"})
        if ms.get("connection") not in (None, "connected"):
            groups["active"].append({"market": name, "severity": "high", "text": f"Venue connection: {ms.get('connection')}"})
        if ms.get("data") == "stale" or (name != "btcusdt" and ms.get("market_open") and ms.get("data") not in ("fresh",)):
            groups["active"].append({"market": name, "severity": "medium", "text": f"Market data {ms.get('data')} while the market is open"})
        integ = h.get("integrity") or {}
        if integ and not integ.get("preregistration_consistent", True):
            groups["active"].append({"market": name, "severity": "high", "text": "Pre-registration hash mismatch"})
        for a in h.get("alerts", []):
            if a.startswith("risk exception") or "non-warm-up signals before" in a or a.startswith("health data unavailable"):
                groups["active"].append({"market": name, "severity": "high", "text": a})
        # -- recorded events --------------------------------------------------------------------------------------
        db_path = Path(m["state_dir"]) / "shadow.sqlite"
        try:
            events = _events_since(db_path, now - timedelta(days=7)) if db_path.exists() else []
        except Exception:  # noqa: BLE001
            events = []
        in_window = [(a, k, d) for a, k, d in events if a >= since.isoformat()]
        conn_counts: dict[str, int] = {}
        last_conn = None
        for a, k, d in in_window:
            if k in CONNECTION_EVENTS:
                conn_counts[k] = conn_counts.get(k, 0) + 1
                last_conn = a
        if conn_counts:
            recovered = ms.get("connection") == "connected"
            groups["connection"].append({
                "market": name, "counts": conn_counts, "last_at": last_conn, "recovered": recovered,
                "text": ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in sorted(conn_counts.items())),
                "status": "recovered: connected now" if recovered else "not recovered"})
        if ms.get("websocket_reconnects"):
            groups["connection"].append({"market": name, "counts": {"websocket_reconnects": ms["websocket_reconnects"]},
                                         "recovered": ms.get("connection") == "connected", "last_at": None,
                                         "text": f"{ms['websocket_reconnects']} WebSocket reconnect(s) since the runner started",
                                         "status": "recovered: connected now" if ms.get("connection") == "connected" else "not recovered"})
        for g in (h.get("uptime") or {}).get("gaps_over_3_min", []) or []:
            groups["resolved"].append({"market": name, "at": g["from"], "kind": "heartbeat_gap",
                                       "text": f"Heartbeat gap of {g['minutes']} min ({g['from']} → {g['to']})",
                                       "resolution": "heartbeats resumed",
                                       "evidence": gap_evidence(db_path, g, events)})

        def later_success(kind_ok: str, at: str, pred=lambda d: True) -> str | None:
            return next((a for a, k, d in events if k == kind_ok and a > at and pred(d)), None)

        for a, k, d in in_window:
            if k == "calendar_refresh_failed":
                ok_at = later_success("calendar_refresh", a)
                item = {"market": name, "at": a, "kind": k, "text": f"Calendar refresh failed ({', '.join(d.get('errors', {}) or [])})"}
                if ok_at:
                    groups["resolved"].append({**item, "resolution": f"later refresh succeeded at {ok_at}"})
                else:
                    groups["active"].append({**item, "severity": "high"})
            elif k == "reconciliation" and not d.get("ok", True):
                ok_at = later_success("reconciliation", a, lambda x: x.get("ok"))
                item = {"market": name, "at": a, "kind": k, "text": f"Reconciliation problem: {d.get('problems', [])[:1]}"}
                if ok_at:
                    groups["resolved"].append({**item, "resolution": f"reconciled at {ok_at}"})
                else:
                    groups["active"].append({**item, "severity": "high"})
            elif k in ("housekeeping_error", "clock_offset"):
                groups["resolved"].append({"market": name, "at": a, "kind": k,
                                           "text": f"{k.replace('_', ' ')}: {json.dumps(d)[:120]}",
                                           "resolution": "runner continued (heartbeats fresh)" if ms.get("runner") == "running" else "check runner"})
        for a, k, d in events:
            if k in ("stop_file", "kill_file"):
                present = controls.get("stop_file" if k == "stop_file" else "kill_file")
                if not present:
                    groups["stop_kill"].append({"market": name, "at": a, "kind": k,
                                                "text": f"{'STOP' if k == 'stop_file' else 'KILL'} file seen by the runner",
                                                "resolution": "file no longer present; runner running" if ms.get("runner") == "running" else "file no longer present"})
    for key in ("resolved", "stop_kill"):
        groups[key].sort(key=lambda x: x.get("at") or "", reverse=True)
    return groups


# --------------------------------------------------------------------------- recent activity

def recent_activity(cfg: dict, now: datetime, limit: int = 30) -> list[dict]:
    """Newest recorded items across runners: forward signals, risk decisions, simulated trades and notable events.
    Warm-up signals are summarised, never mixed into the feed."""
    from command_center.services import runners

    items = []
    for rid, r in runners(cfg).items():
        db = r["db"]
        if not db.exists():
            continue
        con = connect_ro(db)
        try:
            for sid, key, at, status, body in con.execute(
                    "SELECT signal_id, strategy_key, bar_close, status, body FROM signals WHERE status != 'warmup' "
                    "ORDER BY bar_close DESC LIMIT ?", (limit,)):
                b = json.loads(body)
                items.append({"at": at, "runner": rid, "type": "signal", "phase": "forward", "signal_id": sid,
                              "strategy": key, "text": f"{key} {b.get('side')} signal ({status})", "kind": "derived"})
            for book, sid, at, approved, reasons in con.execute(
                    "SELECT book_id, signal_id, evaluated_at, approved, reasons FROM decisions ORDER BY evaluated_at DESC LIMIT ?",
                    (limit,)):
                rs = json.loads(reasons) if reasons else []
                items.append({"at": at, "runner": rid, "type": "decision", "signal_id": sid, "book": book,
                              "text": f"{book}: {'approved' if approved else 'rejected'}" + (f" ({rs[0]})" if rs and not approved else ""),
                              "kind": "simulated"})
            for book, sid, at, pnl in con.execute(
                    "SELECT book_id, signal_id, closed_at, pnl FROM trades ORDER BY closed_at DESC LIMIT ?", (limit,)):
                items.append({"at": at, "runner": rid, "type": "trade", "signal_id": sid, "book": book,
                              "text": f"{book}: simulated trade closed, net {float(pnl):+.2f}", "kind": "simulated"})
            for at, kind, detail in con.execute(
                    "SELECT at, kind, detail FROM events WHERE kind IN ('start', 'stop_file', 'kill_file', "
                    "'calendar_refresh_failed', 'reconnect', 'housekeeping_error', 'warmup_loaded') "
                    "ORDER BY id DESC LIMIT ?", (limit,)):
                items.append({"at": at, "runner": rid, "type": "event", "text": kind.replace("_", " "),
                              "kind": "observed"})
        except Exception:  # noqa: BLE001 - skip an unreadable store; others still show
            pass
        finally:
            con.close()
    items.sort(key=lambda x: x["at"] or "", reverse=True)
    return items[:limit]
