"""Unified read-only health report and Windows ops scripts (static safety checks). SYNTHETIC state only."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

from research.dual_health_report import build, to_markdown
from trading.shadow.store import ShadowStore

UTC = timezone.utc
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


def _repo(tmp: Path, name: str, unseen="2026-11-09T00:00:00+00:00") -> tuple[Path, str]:
    repo = tmp / f"repo_{name}"
    (repo / "research/configs").mkdir(parents=True)
    prereg = repo / "prereg.json"
    prereg.write_text(json.dumps({"unseen_window_starts": unseen}), encoding="utf-8")
    sha = hashlib.sha256(prereg.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    (repo / "research/configs/c.json").write_text(json.dumps({"preregistration": "prereg.json",
                                                              "preregistration_sha256": sha}), encoding="utf-8")
    return repo, sha


def _state(tmp: Path, name: str, sha: str, heartbeat) -> Path:
    state = tmp / f"state_{name}"
    store = ShadowStore(state / "shadow.sqlite")
    store.set_meta("preregistration_sha256", sha)
    store.ensure_book(f"{name}.book", "cand", D("10000"), D("0"))
    for minute in range(0, 24 * 60, 1):
        at = NOW - timedelta(hours=24) + timedelta(minutes=minute)
        if timedelta(hours=5) <= at - (NOW - timedelta(hours=24)) < timedelta(hours=6):
            continue  # one-hour outage of the runner itself
        store.heartbeat(at, **heartbeat(minute))
    store.event(NOW - timedelta(hours=2), "data_interruption", error="Mt5Unavailable")
    store.event(NOW - timedelta(hours=1), "reconciliation", ok=True)
    store.record_signal("cand", "warm-1", datetime(2026, 11, 8, tzinfo=UTC), "warmup", {})
    store.record_signal("cand", "live-1", NOW - timedelta(hours=3), "evaluated", {})
    store.close()
    return state


def _xau_beat(minute):
    disconnected = 600 <= minute < 630
    return {"runner_status": "running", "mt5_connection": "disconnected" if disconnected else "connected",
            "market_open": True, "market_data": "unavailable (MT5 not connected)" if disconnected else "fresh",
            "new_entries": "blocked" if disconnected else "allowed"}


def _btc_beat(minute):
    return {"status": "ok", "data": {"quotes": "websocket", "klines": "ok"},
            "stream": {"connected": True, "reconnects": minute // 500}}


def _markets(tmp):
    xrepo, xsha = _repo(tmp, "x")
    brepo, bsha = _repo(tmp, "b")
    return {"xauusd": {"state": _state(tmp, "x", xsha, _xau_beat), "repo": xrepo, "config": "research/configs/c.json"},
            "btcusdt": {"state": _state(tmp, "b", bsha, _btc_beat), "repo": brepo, "config": "research/configs/c.json"}}


def test_report_uptime_disconnections_signals_and_integrity(tmp_path):
    markets = _markets(tmp_path)
    before = {n: hashlib.sha256((m["state"] / "shadow.sqlite").read_bytes()).hexdigest() for n, m in markets.items()}
    report = build(markets, now=NOW, hours=24)
    x, b = report["markets"]["xauusd"], report["markets"]["btcusdt"]
    assert x["uptime"]["heartbeats"] == 23 * 60 and abs(x["uptime"]["runner_uptime_pct"] - 95.83) < 0.2
    assert len(x["uptime"]["gaps_over_3_min"]) == 1 and x["uptime"]["gaps_over_3_min"][0]["minutes"] == 61.0
    assert x["uptime"]["heartbeats_disconnected"] == 30 and x["disconnections"]["data_interruption"] == 1
    assert x["uptime"]["fresh_data_pct_while_open"] < 100
    assert b["disconnections"]["websocket_reconnects_in_window"] == 2
    assert x["signals"]["total_by_status"] == {"warmup": 1, "evaluated": 1}
    assert x["integrity"]["preregistration_consistent"] and x["integrity"]["non_warmup_signals_before_unseen_start"] == 0
    assert x["books"]["x.book"]["equity"] == 10000.0 and report["orders_possible"] is False
    assert "XAUUSD" in to_markdown(report) and "BTCUSDT" in to_markdown(report)
    after = {n: hashlib.sha256((m["state"] / "shadow.sqlite").read_bytes()).hexdigest() for n, m in markets.items()}
    assert before == after  # read-only


def test_alerts_kill_breaker_stale_heartbeat_and_hash_mismatch_stay_per_market(tmp_path):
    markets = _markets(tmp_path)
    (markets["xauusd"]["state"] / "KILL").write_text("x", encoding="utf-8")
    (markets["xauusd"]["state"] / "breaker.json").write_text(json.dumps({"trips": [{"reason": "test trip"}]}),
                                                             encoding="utf-8")
    (markets["xauusd"]["repo"] / "prereg.json").write_text(json.dumps({"unseen_window_starts": "2027-01-01"}),
                                                           encoding="utf-8")
    report = build(markets, now=NOW + timedelta(minutes=30), hours=24)
    alerts = " | ".join(report["markets"]["xauusd"]["alerts"])
    assert "KILL file present" in alerts and "circuit breaker tripped" in alerts
    assert "no heartbeat for" in alerts and "pre-registration hash mismatch" in alerts
    btc_alerts = report["markets"]["btcusdt"]["alerts"]
    assert not any("KILL" in a or "breaker" in a or "mismatch" in a for a in btc_alerts)  # independent markets


def test_missing_state_is_reported_without_affecting_the_other_market(tmp_path):
    markets = _markets(tmp_path)
    markets["xauusd"]["state"] = tmp_path / "nowhere"
    report = build(markets, now=NOW, hours=24)
    assert report["markets"]["xauusd"]["alerts"] == ["state database missing"]
    assert report["markets"]["btcusdt"]["uptime"]["heartbeats"] > 0


# --------------------------------------------------------------------------- ops scripts (static safety checks)

def _ps(name):
    return (ROOT / "ops/windows" / name).read_text(encoding="utf-8")


def test_supervisor_respects_stop_and_never_removes_stop_or_kill():
    text = _ps("kronos_supervisor.ps1")
    lowered = text.lower()
    for verb in ("remove-item", "del ", "rename-item", "move-item", "set-content $stop", "new-item $stop"):
        assert verb not in lowered
    loop = text[text.index("while ($true)"):]
    assert loop.index("Test-Path $stop") < loop.index("starting runner")  # STOP checked before every start
    assert "Mutex" in text and "-m $Module run" in text  # single instance + running-runner scan
    assert loop.index("Wait-Adopted") < loop.index("starting runner")  # adopt, never duplicate
    assert "AdoptGraceSeconds" in text and "STOP present while adopting" in text
    for api in ("order_send", "/api/v3/order", "orderSend", "OrderSend"):  # no order APIs anywhere
        assert all(api not in _ps(n) for n in ("kronos_supervisor.ps1", "scheduled_tasks.ps1", "power_settings.ps1"))


def test_scheduled_tasks_and_power_settings_are_dry_run_by_default():
    tasks, power = _ps("scheduled_tasks.ps1"), _ps("power_settings.ps1")
    assert "if ($Apply)" in tasks and "MultipleInstances IgnoreNew" in tasks and "DRY RUN" in tasks
    assert "Register-ScheduledTask" in tasks.split("if ($Apply)")[1]
    assert "if (-not $Apply)" in power and "DRY RUN" in power and "-Revert" in power
    assert "setdcvalueindex" not in power.lower()  # battery settings untouched


def test_optional_third_runner_is_not_started_without_alerts(tmp_path):
    from research.dual_health_report import DEFAULT_MARKETS, to_markdown
    assert DEFAULT_MARKETS["xauusd_selective_a"]["optional"] is True
    assert "kronos-selective-a-shadow" in str(DEFAULT_MARKETS["xauusd_selective_a"]["repo"])
    markets = {**_markets(tmp_path), "xauusd_selective_a": {"state": tmp_path / "none", "repo": tmp_path,
                                                            "config": "x.json", "optional": True}}
    report = build(markets, now=NOW, hours=24)
    assert report["markets"]["xauusd_selective_a"] == {**report["markets"]["xauusd_selective_a"],
                                                       "status": "not started", "alerts": []}
    assert "Not started" in to_markdown(report) and report["markets"]["btcusdt"]["uptime"]["heartbeats"] > 0


def test_selective_a_task_is_dry_run_and_gated_on_reviewed_launch_approval():
    text = _ps("selective_a_task.ps1")
    assert "if ($Apply)" in text and "DRY RUN" in text and "MultipleInstances IgnoreNew" in text
    applied = text.split("if ($Apply)")[1].split("} else {")[0]
    assert applied.index("throw") < applied.index("Register-ScheduledTask")  # checks before registering
    assert "launch_approved -ne $true" in text and "preregistration_sha256" in text
    assert "kronos-selective-a-shadow" in text and "-Name xauselectivea" in text
    assert '"xauselectivea"' in _ps("kronos_supervisor.ps1")
    for api in ("order_send", "OrderSend", "/api/v3/order"):
        assert api not in text
