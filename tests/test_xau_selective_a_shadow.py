"""XAUUSD Selective Strategy A shadow preparation: adapter parity with the research generator, the fill-time
net reward-to-risk rule, frozen pre-registration, independent books/state, launch gate, runner mechanics and
criteria. All market data is SYNTHETIC; no terminal, no network, no orders."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pandas as pd
import pytest
from test_phase2d_shadow import (MARKET, OPEN_ALL_WEEK, SWAP, FakeFeed, _calendar_service, spec, tick)

from trading.breaker import CircuitBreaker
from trading.common import ValidationError
from trading.config import load_demo_environment
from trading.daily import DailyLossLedger, DayBoundary
from trading.health import MarketHealthMonitor
from trading.marketdata import Bar
from trading.mtf.frames import Frame, resample
from trading.mtf.shadow_adapter import SelectiveAShadow, net_rr_check
from trading.mtf.strategies import SelectiveParams, selective_candidates
from trading.replay import HistoryView
from trading.risk import RiskLimits
from trading.shadow.engine import BookSpec, EngineSettings
from trading.shadow.store import ShadowStore
from trading.signals import TradeSignal

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "research/configs/xau_selective_a_shadow.json"
M5 = timedelta(minutes=5)
FAST = SelectiveParams(dir_ema=5, dir_slope_bars=2, struct_ema=8, pull_ema=8, hold_ema=10, confirm_bars=4,
                       confirm_ema=8, reward_to_risk=2.5, min_stop_atr=0.3)


def _m5_walk(days: int, seed: int = 11) -> list[Bar]:
    from phase2_helpers import random_walk
    return random_walk("XAUUSD", "5m", datetime(2026, 1, 5, tzinfo=UTC), days * 288, price=4000.0, vol=0.0012,
                       seed=seed, step=M5)


def _research_candidates(bars: list[Bar], p: SelectiveParams) -> pd.DataFrame:
    idx = pd.DatetimeIndex([b.open_time for b in bars]).tz_convert("UTC")
    df = pd.DataFrame([(float(b.open), float(b.high), float(b.low), float(b.close)) for b in bars],
                      columns=["open", "high", "low", "close"], index=idx).assign(volume=0.0)
    base = Frame("5m", df)
    return selective_candidates({"5m": base, **{iv: resample(base, iv) for iv in ("15m", "1h", "4h")}}, p,
                                allow_short=True)


# --------------------------------------------------------------------------- adapter

def test_adapter_reproduces_research_candidates_bar_by_bar():
    bars = _m5_walk(24)
    adapter = SelectiveAShadow.__new__(SelectiveAShadow)
    SelectiveAShadow.__init__(adapter, FAST, tick_size=D("0.01"), window_days=45)
    adapter.window = timedelta(days=12)  # short window is enough for the fast EMAs (test only)
    start = bars[0].open_time + timedelta(days=12)
    got = []
    for n in range(1, len(bars) + 1):
        s = adapter(HistoryView(bars, n))
        if s is not None and s.bar_close_time >= start:
            got.append((s.bar_close_time, s.side.value, s.stop_price))
    ref = _research_candidates(bars, FAST)
    ref = ref[ref.time_ns >= pd.Timestamp(start).value]
    expected = [(pd.Timestamp(t, tz="UTC").to_pydatetime(), "long" if sd == 1 else "short") for t, sd in zip(ref.time_ns, ref.side)]
    assert len(expected) >= 10
    assert [(t, sd) for t, sd, _ in got] == expected
    assert adapter.full_evaluations < len(bars) / 3  # the exact pre-check skips most bars


def test_adapter_signal_prices_follow_research_rounding_and_target():
    bars = _m5_walk(24)
    adapter = SelectiveAShadow(FAST, tick_size=D("0.01"), window_days=45)
    signals = [s for n in range(1, len(bars) + 1) if (s := adapter(HistoryView(bars, n))) is not None]
    assert signals
    for s in signals:
        risk = abs(s.entry_price - s.stop_price)
        assert s.entry_price == bars[[b.close_time for b in bars].index(s.bar_close_time)].close
        assert abs(abs(s.take_profit - s.entry_price) - D("2.5") * risk) <= D("0.01")
        assert s.generated_at == s.bar_close_time and s.provenance["params"]["reward_to_risk"] == 2.5


def test_adapter_rejects_other_intervals_and_short_windows():
    with pytest.raises(ValueError):
        SelectiveAShadow(FAST, tick_size=D("0.01"), window_days=10)
    bar15 = Bar.create(symbol="XAUUSD", interval="15m", open_time=datetime(2026, 1, 5, tzinfo=UTC), open=1, high=2,
                       low=1, close=2)
    with pytest.raises(ValueError):
        SelectiveAShadow(FAST, tick_size=D("0.01"))(HistoryView([bar15], 1))


# --------------------------------------------------------------------------- fill-time net reward-to-risk rule

def _signal(side="long", entry="4000.00", stop="3990.00", tp="4025.00") -> TradeSignal:
    t = datetime(2026, 11, 17, 15, tzinfo=UTC)
    return TradeSignal.create(signal_id=f"a-{side}", market=MARKET.value, symbol="XAUUSD", side=side,
                              entry_price=D(entry), stop_price=D(stop), take_profit=D(tp), strategy_id="t",
                              strategy_version="1", generated_at=t, bar_close_time=t)


def test_net_rr_check_matches_the_research_formula():
    limits = RiskLimits(market=MARKET, max_spread=D("1.00"), slippage_allowance=D("0.10"))
    check = net_rr_check(2.0)
    at = datetime(2026, 11, 17, 15, 0, 1, tzinfo=UTC)
    # fill 4000.30 (ask 4000.20 + 0.10); stop fill 3989.90; commission 0: reward 24.70, loss 10.40 -> 2.375 >= 2
    assert check(_signal(), tick(at, "4000.00", "4000.20"), spec("0"), limits) is None
    # commission 7/lot = 0.07 per oz-unit each way of the formula: (24.70-0.07)/(10.40+0.07) = 2.352 -> still ok
    assert check(_signal(), tick(at, "4000.00", "4000.20"), spec("7"), limits) is None
    # a wide spread at the fill pushes the ratio below 2.0
    reason = check(_signal(), tick(at, "4000.00", "4003.50"), spec("0"), limits)
    assert reason and reason.startswith("Net reward-to-risk after costs below minimum")
    short = _signal("short", "4000.00", "4010.00", "3975.00")
    assert check(short, tick(at, "4000.00", "4000.20"), spec("0"), limits) is None
    assert check(short, tick(at, "3996.00", "3996.20"), spec("0"), limits)  # filled far below entry


def _engine(tmp_path, plan, checks):
    from research.xau_selective_a_shadow import CheckedShadowEngine
    from test_phase2d_shadow import Scripted
    health = MarketHealthMonitor(MARKET, max_status_age=timedelta(days=60), max_quote_gap=timedelta(days=60))
    health.record_status("trading", datetime(2026, 11, 1, tzinfo=UTC), "test")
    cls = type("E", (CheckedShadowEngine,), {"checks": checks})
    return cls(store=ShadowStore(tmp_path / "s.sqlite"), books=[BookSpec("s.e10000.c0", "s", D("10000"), D("0"), 144)],
               strategy_factory=lambda key: Scripted(key, plan.get(key, {})), instruments={D("0"): spec("0")},
               spec_retrieved_at=datetime(2026, 11, 17, tzinfo=UTC),
               limits=RiskLimits(market=MARKET, max_spread=D("5.00"), slippage_allowance=D("0.10")),
               environment=load_demo_environment({"EXECUTION_MODE": "demo"}),
               breaker=CircuitBreaker(MARKET, kill_file=tmp_path / "KILL", state_path=tmp_path / "b.json"),
               daily=DailyLossLedger(tmp_path / "d.sqlite", market=MARKET, boundary=DayBoundary.server(OPEN_ALL_WEEK.server_tz),
                                     halt_fraction=D("0.015")),
               calendar=OPEN_ALL_WEEK, event_guard=lambda now: None, swap=SWAP,
               settings=EngineSettings(unseen_start=datetime(2026, 11, 16, tzinfo=UTC), interval="5m"), health=health)


@pytest.mark.parametrize("ask,approved", [("4000.20", True), ("4003.50", False)])
def test_engine_entry_check_can_only_reject_before_the_risk_engine(tmp_path, ask, approved):
    t0 = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)
    close = t0 + M5
    plan = {"s": {close: ("long", "4000.00", "3990.00", "4025.00")}}
    engine = _engine(tmp_path, plan, {"s": net_rr_check(2.0)})
    b = Bar.create(symbol="XAUUSD", interval="5m", open_time=t0, open=D("4000"), high=D("4001"), low=D("3999"),
                   close=D("4000"))
    engine.process([b], [], close + timedelta(seconds=1))
    engine.process([], [tick(close + timedelta(milliseconds=500), "4000.00", ask)], close + timedelta(seconds=2))
    (decision,) = engine.store.decisions()
    assert decision["approved"] is approved
    if not approved:
        assert decision["reasons"][0].startswith("Net reward-to-risk after costs below minimum")
    # without a check the same wide-spread signal reaches the unchanged risk engine (which approves it here)
    plain = _engine(tmp_path / "plain", plan, {})
    plain.process([b], [], close + timedelta(seconds=1))
    plain.process([], [tick(close + timedelta(milliseconds=500), "4000.00", ask)], close + timedelta(seconds=2))
    assert plain.store.decisions()[0]["approved"]


# --------------------------------------------------------------------------- pre-registration, books, launch gate

def test_config_matches_frozen_preregistration_and_drift_is_refused(tmp_path):
    from research.xau_selective_a_shadow import build_books, check_preregistration, entry_checks, load_config
    config, prereg = load_config(CONFIG)
    cand = prereg["candidates"][0]
    assert cand["params"]["reward_to_risk"] == 2.5 and cand["params"]["pull_atr"] == 0.25
    assert cand["params"]["confirm_bars"] == 6 and cand["params"]["min_net_rr"] == 2.0
    assert prereg["unseen_window_starts"] >= "2026-11-16" and "not chronological" not in prereg["label"]
    assert (config["risk"]["risk_fraction"], config["risk"]["daily_loss_halt_fraction"],
            config["risk"]["aggregate_risk_fraction"]) == ("0.005", "0.015", "0.01")
    books = build_books(config)
    assert len(books) == 6 * 2 * 2 and all(b.max_holding_bars == 144 for b in books)
    assert set(entry_checks(config)) == {b.strategy_key for b in books}
    assert config["state_dir"] == "state/xau_selective_a_shadow" and config["launch_approved"] is False
    for mutate in (lambda c: c["strategies"]["selective_a"]["params"].update(reward_to_risk=3.0),
                   lambda c: c["strategies"]["selective_a"].update(window_days=30),
                   lambda c: c["strategies"]["random"]["params"].update(entry_probability=0.05),
                   lambda c: c["risk"].update(risk_fraction="0.01"),
                   lambda c: c.update(state_dir="state/phase2d_shadow")):
        drift = json.loads(json.dumps(config))
        mutate(drift)
        with pytest.raises(ValidationError):
            check_preregistration(drift, prereg)
    changed = tmp_path / "prereg.json"
    changed.write_text(json.dumps({**prereg, "unseen_window_starts": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({**config, "preregistration": str(changed)}), encoding="utf-8")
    with pytest.raises(ValidationError, match="changed"):
        load_config(cfg)


def test_running_experiments_are_untouched_by_this_module():
    from research.phase2d_shadow import normalized_sha256
    phase2d = json.loads((ROOT / "research/configs/phase2d_shadow_xauusd.json").read_text())
    assert normalized_sha256(ROOT / phase2d["preregistration"]) == phase2d["preregistration_sha256"] ==         "6f743af6fd7c11befe251fd00bf267ddfce4d3fbc8f0c83d4604a12a9dc3f723"
    assert "selective_a" not in phase2d["strategies"] and phase2d["state_dir"] == "state/phase2d_shadow"


def test_run_refuses_without_explicit_launch_approval():
    import argparse

    from research.xau_selective_a_shadow import cmd_run
    with pytest.raises(SystemExit, match="NOT approved"):
        cmd_run(argparse.Namespace(i_have_launch_approval=False, config=str(CONFIG), state_dir=None, once=True,
                                   max_minutes=None))


def test_no_order_routing_or_ai_calls_in_strategy_a_code():
    for path in (ROOT / "research/xau_selective_a_shadow.py", ROOT / "trading/mtf/shadow_adapter.py"):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("order_send", "order_check", "order_modify", "positions_close", "api.x.ai", "XAI_API_KEY"):
            assert forbidden not in text, f"{forbidden} in {path.name}"


# --------------------------------------------------------------------------- runner mechanics and criteria

class M5FakeFeed(FakeFeed):
    def ticks(self, start, end):
        out = []
        for b in self.bars:
            for offset in (timedelta(seconds=1), timedelta(minutes=3)):
                at = b.open_time + offset
                if start <= at < end:
                    out.append(tick(at, str(b.close), str(b.close + D("0.20"))))
        return out


def test_runner_starts_on_m5_with_independent_state_and_restarts_cleanly(tmp_path, monkeypatch):
    from phase2_helpers import random_walk

    from research.xau_selective_a_shadow import SelectiveARunner, load_config
    from trading.marketcal.holidays import build_xauusd_calendar
    from trading.sessions import XAUUSD_NY_MARKET_HOURS
    monkeypatch.setenv("EXECUTION_MODE", "demo")
    config, prereg = load_config(CONFIG)
    config = {**config, "warmup_start": "2026-11-06T00:00:00+00:00"}
    calendar, _ = build_xauusd_calendar(XAUUSD_NY_MARKET_HOURS, datetime(2026, 11, 1).date(), datetime(2026, 11, 30).date())
    bars = random_walk("XAUUSD", "5m", datetime(2026, 11, 6, tzinfo=UTC), 3200, price=4000.0, vol=0.0008, seed=5,
                       calendar=calendar, step=M5)
    now = [datetime(2026, 11, 16, 15, 2, 30, tzinfo=UTC)]
    feed = M5FakeFeed([b for b in bars if b.close_time <= datetime(2026, 11, 18, tzinfo=UTC)])
    runner = SelectiveARunner(config, prereg, feed, tmp_path / "state", clock=lambda: now[0],
                              calendar_service=_calendar_service(tmp_path))
    started = runner.start()
    assert runner.engine.settings.interval == "5m" and len(runner.engine.books) == 24
    assert runner.store.bar_count("warmup_history") > 1000 and not runner.breaker.reasons()
    assert type(runner.engine).__name__ == "SelectiveAEngine" and set(runner.engine.checks) == {
        "selective_a", "random_s1", "random_s2", "random_s3", "random_s4", "random_s5"}
    for _ in range(12):
        now[0] += timedelta(minutes=5)
        runner.poll()
        runner.housekeeping()
    assert runner.store.bar_count("live") >= 12 and runner.engine.reconcile()["ok"]
    assert runner.store.heartbeats() and started["restored"]["signals_mismatch"] == 0
    runner.store.close()
    again = SelectiveARunner(config, prereg, M5FakeFeed(feed.bars), tmp_path / "state", clock=lambda: now[0],
                             calendar_service=_calendar_service(tmp_path / "c2"))
    restored = again.start()["restored"]
    assert restored["signals_mismatch"] == 0 and restored["reconciliation"]["ok"] and not again.breaker.reasons()
    other = {"preregistration_sha256": "0" * 64}
    with pytest.raises(ValidationError, match="different pre-registration"):
        SelectiveARunner({**config, **other}, prereg, M5FakeFeed(feed.bars), tmp_path / "state",
                         clock=lambda: now[0], calendar_service=_calendar_service(tmp_path / "c3")).start()


def test_criteria_report_no_verdict_before_final_checkpoint(tmp_path):
    from research.xau_selective_a_shadow import selective_a_criteria
    _, prereg = __import__("research.xau_selective_a_shadow", fromlist=["load_config"]).load_config(CONFIG)
    store = ShadowStore(tmp_path / "s.sqlite")
    with store.transaction():
        store.ensure_book("selective_a.e10000.c7", "selective_a", D("10000"), D("7"))
    out = selective_a_criteria(store, store.books(), prereg, now=datetime(2026, 12, 1, tzinfo=UTC))
    assert out["trades"] == 0 and out["verdict"].startswith("in progress")
    assert [c["status"] for c in out["criteria"]][:3] == ["insufficient_data"] * 3
    later = selective_a_criteria(store, store.books(), prereg, now=datetime(2028, 6, 1, tzinfo=UTC))
    assert later["verdict"].startswith("retire")  # hard end reached without the evidence


def test_random_control_calibration_is_frozen():
    _, prereg = __import__("research.xau_selective_a_shadow", fromlist=["load_config"]).load_config(CONFIG)
    rnd = prereg["controls"][0]
    assert rnd["params"] == {"entry_probability": 0.0174, "atr_period": 14, "atr_stop_mult": 3.4,
                             "reward_to_risk": 2.5, "long_probability": 0.5}
    assert rnd["seeds"] == [1, 2, 3, 4, 5] and "DEVELOPMENT period" in rnd["calibration"]
    assert replace(FAST, reward_to_risk=2.5).reward_to_risk == 2.5
