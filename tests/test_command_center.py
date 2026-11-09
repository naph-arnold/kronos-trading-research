"""Command Center V1: read-only data access, honest candles (recorded or aggregated only), strategy lifecycle,
overlays and the GET-only API. Synthetic stores in tmp_path; the running runners' files are never touched."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from command_center import services
from command_center.bars import CsvBars, aggregate, load_candles, parse_time, shadow_bars
from command_center.readonly import ReadOnlyShadowStore
from trading.marketdata import Bar
from trading.shadow.store import ShadowStore

UTC = timezone.utc
NOW = datetime.now(UTC).replace(second=0, microsecond=0)


def _csv(path: Path, start: datetime, n: int, step: int, layout: str = "mt5") -> Path:
    lines = (["open_time_utc,server_time,open,high,low,close,tick_volume,spread_points"] if layout == "mt5"
             else ["timestamps,open,high,low,close,volume,amount"])
    for i in range(n):
        t = (start + timedelta(seconds=step * i)).isoformat()
        p = 100 + i
        lines.append(f"{t},0,{p},{p + 2},{p - 1},{p + 1},10,5" if layout == "mt5"
                     else f"{t},{p},{p + 2},{p - 1},{p + 1},10,1000")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _store(state: Path, *, heartbeat_age=timedelta(seconds=30), bars_from: datetime | None = None, n_bars: int = 8,
           interval: str = "15m") -> Path:
    state.mkdir(parents=True, exist_ok=True)
    path = state / "shadow.sqlite"
    s = ShadowStore(path)
    step = 900 if interval == "15m" else 3600
    with s.transaction():
        start = bars_from or (NOW - timedelta(seconds=step * n_bars))
        start = datetime.fromtimestamp(int(start.timestamp()) // step * step, UTC)  # aligned bars
        for i in range(n_bars):
            p = D("2000") + i
            s.add_bar(Bar.create(symbol="XAUUSD", interval=interval, open_time=start + timedelta(seconds=step * i),
                                 open=p, high=p + 3, low=p - 3, close=p + 1, volume=D("5")), "live")
        s.ensure_book("orb_newyork.e10000.c7", "orb_newyork", D("10000"), D("7"))
        s.ensure_book("orb_newyork.e10000.c0", "orb_newyork", D("10000"), D("0"))
        s.ensure_book("random_s1.e10000.c7", "random_s1", D("10000"), D("7"))
        s.heartbeat(NOW - heartbeat_age, runner_status="running", mt5_connection="connected", market_data="fresh",
                    market_open=True, new_entries="allowed", calendar="broker_verified")
        s.set_meta("last_tick", f"{(NOW - timedelta(seconds=5)).isoformat()}|2001.5|2001.7")
    s.close()
    return path


def _cfg(tmp_path: Path, **strategy_a) -> dict:
    repo = tmp_path / "repo"
    (repo / "research/configs").mkdir(parents=True, exist_ok=True)
    risk = {"risk": {"risk_fraction": "0.005", "daily_loss_halt_fraction": "0.015", "aggregate_risk_fraction": "0.01"}}
    (repo / "research/configs/x.json").write_text(json.dumps(risk))
    (repo / "research/configs/a.json").write_text(json.dumps({**risk, "launch_approved": False}))
    hist = tmp_path / "hist"
    hist.mkdir(exist_ok=True)
    end = datetime.fromtimestamp(int((NOW - timedelta(hours=10)).timestamp()) // 3600 * 3600, UTC)
    h15 = _csv(hist / "x15.csv", end - timedelta(minutes=15 * 199), 200, 900)
    h1 = _csv(hist / "x1h.csv", end - timedelta(hours=199), 200, 3600)
    return {"health_window_hours": 6, "markets": {"xauusd": {
        "label": "XAUUSD", "venue": "test", "state_dir": str(tmp_path / "state_x"), "repo": str(repo),
        "config": "research/configs/x.json", "store_interval": "15m", "volume_label": "tick volume",
        "history": {"15m": str(h15), "1h": str(h1)}}},
        "strategies": [
            {"id": "xau_orb", "name": "ORB", "market": "xauusd", "strategy_key": "orb_newyork",
             "controls": ["random_s1"]},
            {"id": "xau_selective_a", "name": "Strategy A", "market": "xauusd", "strategy_key": "selective_a",
             "controls": ["random"], "state_dir": str(tmp_path / "state_a"), "repo": str(repo),
             "config": "research/configs/a.json", "store_interval": "5m", **strategy_a}],
        "grok": {"config": str(tmp_path / "missing.json"), "advisory_dirs": [str(tmp_path / "grok")]}}


# --------------------------------------------------------------------------- candles

@pytest.mark.parametrize("layout", ["mt5", "binance"])
def test_csv_bisection_returns_exact_tail_and_until_windows(tmp_path, layout):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    src = CsvBars(_csv(tmp_path / "b.csv", start, 5000, 60, layout))
    tail = src.last(10)
    assert [b.open for b in tail] == [100 + i for i in range(4990, 5000)] and tail[-1].volume == 10
    until = start + timedelta(minutes=1234, seconds=30)  # between bars
    win = src.last(5, until=until)
    assert win[-1].time == start + timedelta(minutes=1234) and len(win) == 5
    assert src.last(3, until=start - timedelta(days=1)) == []
    assert src.span() == (start, start + timedelta(minutes=4999))


def test_candles_merge_history_and_live_without_overlap_or_invention(tmp_path):
    cfg = _cfg(tmp_path)
    m = cfg["markets"]["xauusd"]
    # live bars overlap the end of the 15m history (they must not be double counted) and run to now
    _store(Path(m["state_dir"]), bars_from=NOW - timedelta(hours=11), n_bars=44)
    out = load_candles(m, "15m", limit=500)
    times = [c["time"] for c in out["candles"]]
    assert times == sorted(set(times))  # strictly increasing, no duplicate buckets
    hist_end = parse_time(out["segments"][0]["to"])
    assert out["segments"][1]["from"] > out["segments"][0]["to"]
    assert all(c["source"] == "history" for c in out["candles"] if c["time"] <= hist_end.timestamp())
    four = load_candles(m, "4h", limit=50)
    assert all(c["time"] % 14400 == 0 for c in four["candles"]) and any("aggregated" in n for n in four["notes"])
    one_m = load_candles(m, "1m", limit=50)  # no 1m source and the store is 15m: nothing is fabricated
    assert one_m["candles"] == [] and "Nothing is displayed rather than invented" in one_m["notes"][0]


def test_aggregation_preserves_ohlc_and_flags_forming_bucket():
    from command_center.bars import Candle
    t = datetime(2026, 1, 1, tzinfo=UTC)
    bars = [Candle(t + timedelta(minutes=15 * i), 10 + i, 20 + i, 5 - i, 11 + i, 1.0, "live") for i in range(4)]
    (one,), partial = aggregate(bars, 3600, 900)
    assert (one.open, one.high, one.low, one.close, one.volume) == (10, 23, 2, 14, 4.0) and not partial


# --------------------------------------------------------------------------- read-only guarantees

def test_read_only_store_refuses_writes_and_leaves_files_byte_identical(tmp_path):
    db = _store(tmp_path / "state")
    before = _sha(db)
    with ReadOnlyShadowStore(db) as s:
        assert set(s.books()) and s.heartbeats()
        with pytest.raises(PermissionError):
            s.event(NOW, "x")
        with pytest.raises(sqlite3.OperationalError):
            s._db.execute("DELETE FROM books")
    cfg = _cfg(tmp_path)
    cfg["markets"]["xauusd"]["state_dir"] = str(tmp_path / "state")
    services.overview(cfg)
    services.chart(cfg, "xauusd", "15m", limit=100)
    services.signals(cfg, "xau_orb")
    assert _sha(db) == before and not (tmp_path / "state" / "STOP").exists()
    assert shadow_bars(tmp_path / "nope.sqlite") == []


# --------------------------------------------------------------------------- strategies and status

def test_strategy_a_is_prepared_never_running_without_its_runner(tmp_path):
    cfg = _cfg(tmp_path)
    _store(Path(cfg["markets"]["xauusd"]["state_dir"]))
    panel = services.strategy_panel(cfg, cfg["strategies"][1])
    assert panel["lifecycle"] == "prepared" and panel["metrics"] is None and panel["launch_approved"] is False
    assert services.signals(cfg, "xau_selective_a")["available"] is False


@pytest.mark.parametrize("setup,expected", [
    (lambda st: None, "running"), (lambda st: (st / "STOP").write_text(""), "paused"),
    (lambda st: (st / "KILL").write_text(""), "blocked"),
    (lambda st: (st / "breaker.json").write_text(json.dumps({"trips": [{"reason": "x"}]})), "blocked"),
])
def test_lifecycle_reflects_heartbeats_and_operator_controls(tmp_path, setup, expected):
    cfg = _cfg(tmp_path)
    state = Path(cfg["markets"]["xauusd"]["state_dir"])
    _store(state)
    setup(state)
    assert services.strategy_panel(cfg, cfg["strategies"][0])["lifecycle"] == expected


def test_stale_heartbeat_is_not_shown_as_running(tmp_path):
    cfg = _cfg(tmp_path)
    _store(Path(cfg["markets"]["xauusd"]["state_dir"]), heartbeat_age=timedelta(minutes=30))
    assert services.strategy_panel(cfg, cfg["strategies"][0])["lifecycle"] == "stopped"
    assert services.market_status("xauusd", cfg["markets"]["xauusd"])["runner"] == "not responding"


def test_metrics_rejections_and_markers_come_from_recorded_simulation(tmp_path):
    cfg = _cfg(tmp_path)
    state = Path(cfg["markets"]["xauusd"]["state_dir"])
    db = _store(state, n_bars=40)
    close = NOW - timedelta(hours=5)
    close = close.replace(minute=(close.minute // 15) * 15)
    s = ShadowStore(db)
    body = {"side": "long", "entry_price": "2010", "stop_price": "2000", "take_profit": "2030",
            "provenance": {"context": {"session": "newyork", "range": [2000, 2008]},
                           "params": {"range_minutes": 30}, "source_bar_open": (close - timedelta(minutes=15)).isoformat()}}
    with s.transaction():
        s.record_signal("orb_newyork", "sig1", close, "evaluated", body)
        s.record_decision("orb_newyork.e10000.c7", "sig1", dedup_key="k", evaluated_at=close, approved=True,
                          size=D("0.1"), planned_risk=D("50"), reasons=[], context={"equity": "10000"})
        s.record_decision("orb_newyork.e10000.c0", "sig1", dedup_key="k0", evaluated_at=close, approved=False,
                          size=None, planned_risk=None, reasons=["Spread 0.9 exceeds max 0.5"], context={})
        trade = {"book_id": "orb_newyork.e10000.c7", "signal_id": "sig1", "side": "long", "entry": "2010",
                 "stop": "2000", "take_profit": "2030", "exit": "2030", "opened_at": close.isoformat(),
                 "closed_at": (close + timedelta(hours=1)).isoformat(), "pnl": "95", "gross_pnl": "100",
                 "spread_slippage_cost": "1", "commission": "4", "swap": "0", "r_multiple": "1.9",
                 "planned_risk": "50", "risk_budget": "50", "loss_beyond_planned": "0", "exit_reason": "take_profit"}
        s.open_position("orb_newyork.e10000.c7", "sig1", {})
        s.close_position("orb_newyork.e10000.c7", "sig1", close + timedelta(hours=1), D("95"), trade)
        s.add_equity("orb_newyork.e10000.c7", close + timedelta(hours=1), D("10095"))
    s.close()
    panel = services.strategy_panel(cfg, cfg["strategies"][0])
    m = panel["metrics"]
    assert panel["primary_book"] == "orb_newyork.e10000.c7"  # higher cost scenario is the headline
    assert (m["trades"], m["wins"], m["losses"], m["net_pnl"], m["win_rate_pct"]) == (1, 1, 0, 95.0, 100.0)
    assert panel["rejections"][0]["categories"] == ["spread_limit"] and panel["signals"]["forward"] == 1
    chart = services.chart(cfg, "xauusd", "15m", limit=200)
    (sig,) = chart["signals"]
    assert sig["time"] == int(close.timestamp()) - 900 and sig["decisions"] == {"approved": 1, "rejected": 1}
    (tr,) = chart["trades"]
    assert tr["exit_reason"] == "take_profit" and tr["kind"] == "simulated"
    (rng,) = chart["opening_ranges"] or [None] if chart["opening_ranges"] else [None]
    if rng is not None:  # 08:20 New York local -> 13:20 UTC (EST) or 12:20 UTC (EDT)
        start = datetime.fromtimestamp(rng["from"], UTC)
        assert (start.hour, start.minute) in ((13, 15), (12, 15)) and (rng["low"], rng["high"]) == (2000.0, 2008.0)


def test_opening_range_uses_the_strategy_session_definition():
    sig = {"signal_id": "s", "body": {"provenance": {
        "context": {"session": "newyork", "range": [1.0, 2.0]}, "params": {"range_minutes": 30},
        "source_bar_open": "2026-11-05T14:30:00+00:00"}}}
    rng = services._opening_range(sig, 900)
    assert datetime.fromtimestamp(rng["from"], UTC) == datetime(2026, 11, 5, 13, 15, tzinfo=UTC)  # floor(13:20)
    assert datetime.fromtimestamp(rng["to"], UTC) == datetime(2026, 11, 5, 13, 50, tzinfo=UTC)
    summer = {**sig, "body": {"provenance": {**sig["body"]["provenance"], "source_bar_open": "2026-07-01T14:30:00+00:00"}}}
    assert datetime.fromtimestamp(services._opening_range(summer, 900)["to"], UTC).hour == 12  # EDT


def test_grok_room_reports_not_activated_without_reading_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-SECRET")
    room = services.grok_room(_cfg(tmp_path))
    assert room["status"] == "not activated" and room["xai_key_present"] is True
    assert "xai-SECRET" not in json.dumps(room) and len(room["roles"]) == 6 and room["stores"] == []


# --------------------------------------------------------------------------- API

def test_api_is_get_only_and_serves_every_view(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from command_center.api import create_app
    cfg = _cfg(tmp_path)
    _store(Path(cfg["markets"]["xauusd"]["state_dir"]))
    path = tmp_path / "cc.json"
    path.write_text(json.dumps({**cfg, "bind": {"host": "127.0.0.1", "port": 1}}))
    client = TestClient(create_app(path))
    for url in ("/api/meta", "/api/overview", "/api/strategies", "/api/strategies/xau_orb/signals",
                "/api/chart/xauusd?tf=1h&limit=50", "/api/availability/xauusd", "/api/grok"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert r.headers["cache-control"] == "no-store"
    assert client.get("/api/meta").json()["orders_possible"] is False
    assert client.get("/api/chart/xauusd?tf=2h").status_code == 400
    assert client.get("/api/chart/eurusd").status_code == 404
    for method in ("post", "put", "delete", "patch"):
        assert getattr(client, method)("/api/overview").status_code == 405


# --------------------------------------------------------------------------- V1.1: history, resilience, security

def _with_trade(cfg) -> Path:
    state = Path(cfg["markets"]["xauusd"]["state_dir"])
    db = _store(state, n_bars=40)
    close = NOW - timedelta(hours=5)
    close = close.replace(minute=(close.minute // 15) * 15)
    s = ShadowStore(db)
    with s.transaction():
        s.record_signal("orb_newyork", "sig1", close, "evaluated", {"side": "long", "entry_price": "2010",
                        "stop_price": "2000", "take_profit": "2030", "provenance": {"interval": "15m",
                        "context": {"atr": 4.2}, "params_hash": "abc"}})
        s.record_signal("random_s1", "rnd1", close, "evaluated", {"side": "short"})
        s.record_decision("orb_newyork.e10000.c0", "sig1", dedup_key="k0", evaluated_at=close, approved=False,
                          size=None, planned_risk=None, reasons=["Spread 0.9 exceeds max 0.5"], context={})
        s.record_decision("orb_newyork.e10000.c7", "sig1", dedup_key="k", evaluated_at=close, approved=True,
                          size=D("0.1"), planned_risk=D("50"), reasons=[], context={"equity": "10000"})
        trade = {"side": "long", "entry": "2010", "exit": "2000", "stop": "2000", "take_profit": "2030",
                 "opened_at": close.isoformat(), "closed_at": (close + timedelta(hours=1)).isoformat(),
                 "gross_pnl": "-46", "spread_slippage_cost": "2", "commission": "2", "swap": "0", "pnl": "-50",
                 "r_multiple": "-1", "planned_risk": "50", "risk_budget": "50", "loss_beyond_planned": "0",
                 "exit_reason": "stop", "signal_id": "sig1"}
        s.open_position("orb_newyork.e10000.c7", "sig1", {})
        s.close_position("orb_newyork.e10000.c7", "sig1", close + timedelta(hours=1), D("-50"), trade)
        s.add_equity("orb_newyork.e10000.c7", close, D("10000"))
        s.add_equity("orb_newyork.e10000.c7", close + timedelta(hours=1), D("9950"))
        s.event(close, "data_interruption", consecutive=1, error="Mt5Unavailable")
        s.event(close, "mt5_account_check", balance=3000.0)
    s.close()
    return db


def test_history_is_searchable_across_signals_decisions_trades_and_events(tmp_path):
    cfg = _cfg(tmp_path)
    _with_trade(cfg)
    sig = services.history(cfg, "xauusd", "signals", strategy="orb_newyork")
    assert sig["total"] == 1 and sig["rows"][0]["approved"] == 1 and sig["rows"][0]["traded"] is True
    rej = services.history(cfg, "xauusd", "decisions", status="rejected")
    assert rej["total"] == 1 and rej["rows"][0]["categories"] == ["spread_limit"]
    assert services.history(cfg, "xauusd", "decisions", q="spread")["total"] == 1
    tr = services.history(cfg, "xauusd", "trades")["rows"][0]
    assert tr["pnl"] == -50 and tr["costs"]["commission"] == 2 and tr["exit_reason"] == "stop"
    ev = services.history(cfg, "xauusd", "events", status="interruption")
    assert ev["total"] == 1 and ev["rows"][0]["event"] == "data_interruption"
    assert services.history(cfg, "xau_selective_a", "signals")["available"] is False
    eq = services.equity(cfg, "xauusd", strategy="orb_newyork")
    book = next(b for b in eq["books"] if b["book"] == "orb_newyork.e10000.c7")
    assert book["last"] == 9950 and book["max_drawdown_pct"] == pytest.approx(-0.5)
    with pytest.raises(ValueError):
        services.history(cfg, "xauusd", "orders")


def test_signal_detail_links_context_chart_decisions_execution_and_reviews(tmp_path):
    cfg = _cfg(tmp_path)
    _with_trade(cfg)
    d = services.signal_detail(cfg, "xauusd", "sig1")
    assert d["context"] == {"atr": 4.2} and d["params_hash"] == "abc" and d["interval"] == "15m"
    assert {x["book"] for x in d["decisions"]} == {"orb_newyork.e10000.c0", "orb_newyork.e10000.c7"}
    (t,) = d["trades"]
    assert (t["gross_pnl"], t["spread_slippage_cost"], t["commission"], t["pnl"]) == (-46, 2, 2, -50)
    assert d["chart"]["candles"] and d["grok_reviews"] == [] and d["evidence_note"] is None
    with pytest.raises(KeyError):
        services.signal_detail(cfg, "xauusd", "nope")


def test_chart_filters_controls_and_flags_off_market_prints(tmp_path):
    cfg = _cfg(tmp_path)
    _with_trade(cfg)
    both = services.chart(cfg, "xauusd", "15m", limit=200)
    assert {s["strategy"] for s in both["signals"]} == {"orb_newyork", "random_s1"}
    only = services.chart(cfg, "xauusd", "15m", limit=200, include_controls=False)
    assert {s["strategy"] for s in only["signals"]} == {"orb_newyork"}
    candles = [{"time": i, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5} for i in range(30)]
    candles[10] = {"time": 10, "open": 100.0, "high": 101.0, "low": 60.0, "close": 100.5}
    assert [o["time"] for o in services._outlier_wicks(candles)] == [10]


def test_unreadable_database_is_reported_not_crashed(tmp_path):
    cfg = _cfg(tmp_path)
    state = Path(cfg["markets"]["xauusd"]["state_dir"])
    state.mkdir(parents=True)
    (state / "shadow.sqlite").write_bytes(b"this is not a database" * 100)
    o = services.overview(cfg)
    assert o["markets"]["xauusd"]["available"] is False and o["markets"]["xauusd"]["runner"] == "data unavailable"
    xau = next(s for s in o["strategies"] if s["id"] == "xau_orb")
    assert xau["lifecycle"] == "unavailable" and xau["metrics"] is None
    text = json.dumps(o)
    assert str(tmp_path) not in text and str(tmp_path).replace("\\", "/") not in text  # no paths leaked


def _client(tmp_path, cfg, **extra):
    from fastapi.testclient import TestClient

    from command_center.api import create_app
    path = tmp_path / "cc.json"
    path.write_text(json.dumps({**cfg, "bind": {"host": "127.0.0.1", "port": 1}, **extra}))
    return TestClient(create_app(path))


def test_api_security_headers_hosts_errors_and_no_paths(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    cfg = _cfg(tmp_path)
    _with_trade(cfg)
    c = _client(tmp_path, cfg)
    r = c.get("/api/overview")
    assert r.status_code == 200 and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert str(tmp_path) not in r.text and str(tmp_path).replace("\\", "/") not in r.text  # no filesystem paths
    for url in ("/api/history/xauusd/trades?q=stop", "/api/equity/xauusd", "/api/signal/xauusd/sig1",
                "/api/history/xauusd/events?status=interruption", "/api/health"):
        assert c.get(url).status_code == 200, url
    assert c.get("/api/signal/xauusd/missing").status_code == 404
    assert c.get("/api/history/xauusd/orders").status_code == 404
    assert c.get("/api/meta", headers={"host": "evil.example"}).status_code == 400  # DNS rebinding
    assert c.post("/api/health").status_code == 405
    health = c.get("/api/health").json()
    assert health["service"] == "kronos-command-center" and health["read_only"] and not health["orders_possible"]


def test_optional_token_protects_the_api_for_future_remote_use(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    monkeypatch.setenv("KRONOS_CC_TOKEN", "s3cret-token")
    c = _client(tmp_path, _cfg(tmp_path))
    assert c.get("/api/meta").status_code == 401
    assert c.get("/api/meta", headers={"Authorization": "Bearer s3cret-token"}).status_code == 200
    assert c.get("/api/health").status_code == 200  # liveness only; no runner data
    assert "s3cret-token" not in c.get("/api/health").text


def test_server_refuses_public_bind_and_duplicate_instance(tmp_path, monkeypatch, capsys):
    import socket

    from command_center import __main__ as m
    monkeypatch.delenv("KRONOS_CC_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["cc", "--host", "0.0.0.0", "--allow-remote-bind"])
    with pytest.raises(SystemExit) as exc:
        m.main()
    assert exc.value.code == m.EXIT_UNSAFE_BIND
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        monkeypatch.setattr("sys.argv", ["cc", "--port", str(port)])
        with pytest.raises(SystemExit) as exc:
            m.main()
    assert exc.value.code == m.EXIT_PORT_IN_USE and "already in use" in capsys.readouterr().err


def test_scrub_removes_local_paths_in_both_slash_styles(tmp_path):
    cfg = _cfg(tmp_path)
    state = cfg["markets"]["xauusd"]["state_dir"]
    value = {"a": f"{state}/shadow.sqlite", "b": f"OSError: {state.replace('/', chr(92))}{chr(92)}x",
             "c": f"{Path.home()}{chr(92)}secret.txt", "d": "plain"}
    text = json.dumps(services.scrub(value, cfg))
    assert str(tmp_path) not in text and str(tmp_path).replace(chr(92), "/") not in text
    assert str(Path.home()) not in text and "plain" in text and "<xauusd state>" in text


# --------------------------------------------------------------------------- V1.2: decision status, alerts, equity

def _bars(start: datetime, n: int, step: int, symbol="XAUUSD", interval="15m", shape=None):
    out = []
    for i in range(n):
        t = start + timedelta(seconds=step * i)
        lo, hi = shape(t) if shape else (D("2000"), D("2004"))
        out.append(Bar.create(symbol=symbol, interval=interval, open_time=t, open=lo + 1, high=hi, low=lo, close=lo + 2,
                              volume=D("1")))
    return out


def test_orb_windows_follow_frozen_params_and_skip_holidays_and_weekends():
    from command_center import decisions
    from trading.marketcal.holidays import build_xauusd_calendar
    from trading.sessions import XAUUSD_NY_MARKET_HOURS

    now = datetime(2026, 12, 24, 20, 0, tzinfo=UTC)
    cal, _ = build_xauusd_calendar(XAUUSD_NY_MARKET_HOURS, now.date() - timedelta(days=3), now.date() + timedelta(days=20))
    w = decisions.orb_windows({"session": "newyork", "range_minutes": 30, "entry_window_minutes": 180}, now, cal)
    assert w[0]["date"] == "2026-12-28"  # Christmas (Fri) and the weekend have no opening range
    assert w[0]["range_from"] == "2026-12-28T13:20:00+00:00"  # 08:20 New York (EST)
    assert w[0]["entry_to"] == "2026-12-28T16:50:00+00:00"
    summer = decisions.orb_windows({"session": "newyork"}, datetime(2026, 7, 7, 0, 0, tzinfo=UTC), None)
    assert summer[0]["range_from"] == "2026-07-07T12:20:00+00:00"  # EDT


def test_orb_replay_uses_the_frozen_strategy_on_recorded_bars():
    from command_center import decisions

    def shape(t):  # the two opening-range bars (13:30, 13:45 UTC = 08:30, 08:45 NY) span 1995-2010
        return (D("1995"), D("2010")) if t.hour == 13 and t.minute in (30, 45) else (D("2000"), D("2004"))
    bars = _bars(datetime(2026, 11, 10, 6, 0, tzinfo=UTC), 32, 900, shape=shape)
    s = decisions.replay_orb(bars, {"session": "newyork"}, bars[-1].close_time)
    assert s["available"] and s["range_low"] == 1995 and s["range_high"] == 2010
    assert s["session_day"] == "2026-11-10" and s["signal_today"] is False
    assert s["long_trigger"] == pytest.approx(2010 + 0.1 * s["atr"])
    assert s["range_filter_ok"] == (0.5 <= 15 / s["atr"] <= 3.0)


def test_donchian_replay_reports_the_channel_the_next_close_must_exceed():
    from command_center import decisions

    bars = _bars(datetime(2026, 11, 1, tzinfo=UTC), 30, 3600, symbol="BTCUSDT", interval="1h",
                 shape=lambda t: (D("100") + t.hour, D("110") + t.hour))
    s = decisions.replay_donchian(bars, {"entry_lookback": 10})
    assert s["channel_high"] == max(float(b.high) for b in bars[-10:])
    assert s["last_close"] == float(bars[-1].close) and s["distance"] > 0


def test_decision_status_is_built_from_recorded_state_only(tmp_path):
    cfg = _cfg(tmp_path)
    _store(Path(cfg["markets"]["xauusd"]["state_dir"]), n_bars=40)
    o = services.overview(cfg)
    d = {x["id"]: x for x in o["decisions"]}
    assert d["xau_selective_a"]["state"] == "not_launched"
    orb = d["xau_orb"]
    assert orb["state"] in ("waiting_for_session", "monitoring", "waiting_for_signal", "blocked")
    assert {c["check"] for c in orb["checks"]} >= {"Runner heartbeat", "Market data", "Deterministic guards"}
    assert orb["signals"]["forward_count"] == 0 and orb["signals"]["warmup_count"] == 0
    Path(cfg["markets"]["xauusd"]["state_dir"], "STOP").write_text("")
    services.CACHE._items.clear()
    o = services.overview(cfg)
    orb = next(x for x in o["decisions"] if x["id"] == "xau_orb")
    assert orb["state"] == "blocked" and "STOP file present" in orb["blocking_reasons"]
    assert any(a["text"] == "STOP file present" for a in o["alert_groups"]["active"])


def test_alerts_separate_active_connection_resolved_and_past_stop_kill(tmp_path):
    cfg = _cfg(tmp_path)
    db = _store(Path(cfg["markets"]["xauusd"]["state_dir"]))
    s = ShadowStore(db)
    with s.transaction():
        s.event(NOW - timedelta(hours=3), "stop_file")
        s.event(NOW - timedelta(hours=2), "data_interruption", error="x")
        s.event(NOW - timedelta(hours=2), "calendar_refresh_failed", errors={"bls": "timeout"})
        s.event(NOW - timedelta(hours=1), "calendar_refresh", errors={})
    s.close()
    g = services.overview(cfg)["alert_groups"]
    # the synthetic store has no pre-registration hash (correctly active); nothing historical is active
    assert [a["text"] for a in g["active"]] == ["Pre-registration hash mismatch"]
    assert [x["kind"] for x in g["stop_kill"]] == ["stop_file"]
    assert g["connection"][0]["counts"] == {"data_interruption": 1} and g["connection"][0]["recovered"] is True
    assert any(x["kind"] == "calendar_refresh_failed" and "succeeded" in x["resolution"] for x in g["resolved"])


def test_equity_is_per_book_from_recorded_trades_and_warmup_is_separated(tmp_path):
    cfg = _cfg(tmp_path)
    db = _with_trade(cfg)
    s = ShadowStore(db)
    with s.transaction():
        s.record_signal("orb_newyork", "warm1", NOW - timedelta(days=3), "warmup", {"side": "long"})
    s.close()
    e = {b["book"]: b for b in services.equity(cfg, "xauusd")["books"]}
    b = e["orb_newyork.e10000.c7"]
    assert b["trades"] == 1 and b["realized_pnl"] == -50 and b["last"] == 9950 and b["max_drawdown_pct"] == -0.5
    assert e["orb_newyork.e10000.c0"]["trades"] == 0 and e["orb_newyork.e10000.c0"]["last"] == 10000  # never summed
    fwd = services.history(cfg, "xauusd", "signals", status="forward")
    warm = services.history(cfg, "xauusd", "signals", status="warmup")
    assert {r["signal_id"] for r in fwd["rows"]} == {"sig1", "rnd1"} and all(r["phase"] == "forward" for r in fwd["rows"])
    assert [r["signal_id"] for r in warm["rows"]] == ["warm1"]
