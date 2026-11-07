"""Phase 2D shadow engine: simulated execution, timing, sessions, restart recovery, duplicates,
reconciliation, costs, circuit breakers, emergency stop, demo-only. All market data is SYNTHETIC."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from trading.breaker import CircuitBreaker
from trading.clock import EET_EU
from trading.common import Market
from trading.config import LiveTradingForbidden, load_demo_environment
from trading.daily import DailyLossLedger, DayBoundary
from trading.health import MarketHealthMonitor
from trading.instruments import Mt5InstrumentSpec
from trading.marketdata import Bar, Quote
from trading.risk import RiskLimits
from trading.sessions import SessionWindow, TradingCalendar
from trading.shadow.costs import SwapSpec
from trading.shadow.engine import BookSpec, EngineSettings, ShadowEngine
from trading.shadow.report import build_report, preregistered_criteria, uptime
from trading.shadow.store import ShadowStore
from trading.signals import TradeSignal

UTC = timezone.utc
MARKET = Market.XAUUSD_MT5
T0 = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday 10:00 New York
M15 = timedelta(minutes=15)
ROOT = Path(__file__).resolve().parents[1]
OPEN_ALL_WEEK = TradingCalendar(symbol="XAUUSD", server_tz=EET_EU,
                                windows=tuple(SessionWindow(d, 0, 1440) for d in range(7)),
                                open_buffer=timedelta(0), close_buffer=timedelta(0), verification="broker_verified")


def spec(commission="0") -> Mt5InstrumentSpec:
    return Mt5InstrumentSpec(symbol="XAUUSD", broker_symbol="XAUUSD", account_currency="USD", contract_size=D("100"),
                             point=D("0.01"), tick_size=D("0.01"), tick_value_loss=D("1.0"), volume_min=D("0.01"),
                             volume_max=D("50"), volume_step=D("0.01"), stops_level_points=0,
                             margin_per_lot=D("4000"), commission_per_lot_round_trip=D(commission))


SWAP = SwapSpec(D("-12.6"), D("-4.6"), D("1.0"), "Wednesday", EET_EU, "test", True)


def bar(i: int, price: float = 4000.0, *, start: datetime = T0) -> Bar:
    p = D(str(price))
    return Bar.create(symbol="XAUUSD", interval="15m", open_time=start + i * M15, open=p, high=p + 1, low=p - 1,
                      close=p)


def tick(at: datetime, bid: str, ask: str | None = None) -> Quote:
    return Quote.create(symbol="XAUUSD", bid=D(bid), ask=D(ask) if ask else D(bid) + D("0.20"), time=at)


class Scripted:
    """Emits planned signals at given bar close times (deterministic, restart-safe)."""

    def __init__(self, key: str, plan: dict):
        self.key, self.plan = key, plan

    def __call__(self, history):
        b = history[-1]
        if b.close_time not in self.plan:
            return None
        side, entry, stop, tp = self.plan[b.close_time]
        return TradeSignal.create(signal_id=f"{self.key}-{b.close_time:%Y%m%dT%H%M}-{side}", market=MARKET.value,
                                  symbol="XAUUSD", side=side, entry_price=D(entry), stop_price=D(stop),
                                  take_profit=D(tp) if tp else None, strategy_id=f"test-{self.key}",
                                  strategy_version="1", generated_at=b.close_time, bar_close_time=b.close_time)


def make_engine(tmp_path: Path, plan: dict, *, books=None, calendar=OPEN_ALL_WEEK, guard=None,
                unseen=T0 - timedelta(days=1), commissions=("0", "7")) -> ShadowEngine:
    books = books or [BookSpec("s.e10000.c0", "s", D("10000"), D("0"), 4)]
    health = MarketHealthMonitor(MARKET, max_status_age=timedelta(days=60), max_quote_gap=timedelta(days=60))
    health.record_status("trading", T0 - timedelta(days=2), "test")
    limits = RiskLimits(market=MARKET, max_spread=D("1.00"), slippage_allowance=D("0.10"))
    return ShadowEngine(
        store=ShadowStore(tmp_path / "shadow.sqlite"), books=books,
        strategy_factory=lambda key: Scripted(key, plan.get(key, {})),
        instruments={D(c): spec(c) for c in commissions}, spec_retrieved_at=T0 - timedelta(hours=1), limits=limits,
        environment=load_demo_environment({"EXECUTION_MODE": "demo"}),
        breaker=CircuitBreaker(MARKET, kill_file=tmp_path / "KILL", state_path=tmp_path / "breaker.json"),
        daily=DailyLossLedger(tmp_path / "daily.sqlite", market=MARKET, boundary=DayBoundary.server(EET_EU),
                              halt_fraction=D("0.015")),
        calendar=calendar, event_guard=guard or (lambda now: None), swap=SWAP,
        settings=EngineSettings(unseen_start=unseen), health=health)


C0 = T0 + M15  # close of bar 0 = signal time
LONG = {"s": {C0: ("long", "4000.00", "3990.00", "4015.00")}}


def _open_long(engine):
    engine.process([bar(0)], [], C0 + timedelta(seconds=1))
    engine.process([], [tick(C0 + timedelta(milliseconds=500), "4000.00", "4000.20")], C0 + timedelta(seconds=2))


# --------------------------------------------------------------------------- simulated execution

def test_entry_fills_at_first_tick_after_signal_close_on_ask_plus_slippage(tmp_path):
    engine = make_engine(tmp_path, LONG)
    _open_long(engine)
    position = engine.positions["s.e10000.c0"]
    # budget 50 USD; loss/lot = 100 x ((4000.20+0.10) - (3990-0.10)) = 1040 -> 0.048 -> rounded DOWN to 0.04
    assert position.entry == D("4000.30") and position.size == D("0.04") and position.planned_risk == D("41.6")
    assert position.opened_at == C0 + timedelta(milliseconds=500) and position.signal_bar_close == C0
    decision = engine.store.decisions()[0]
    assert decision["approved"] and decision["evaluated_at"] == (C0 + timedelta(milliseconds=500)).isoformat()
    assert decision["context"]["spread"] == "0.20" and decision["context"]["commission_verified"] is False


def test_take_profit_fills_at_limit_without_positive_slippage(tmp_path):
    engine = make_engine(tmp_path, LONG)
    _open_long(engine)
    engine.process([], [tick(C0 + timedelta(minutes=3), "4016.00")], C0 + timedelta(minutes=3, seconds=1))
    (trade,) = engine.store.trades()
    assert trade["exit_reason"] == "take_profit" and trade["exit"] == "4015.00"
    assert D(trade["pnl"]) == D("0.04") * 100 * (D("4015.00") - D("4000.30"))
    assert engine.realized["s.e10000.c0"] == D(trade["pnl"])


def test_stop_fills_at_triggering_tick_minus_slippage_and_reports_overshoot(tmp_path):
    engine = make_engine(tmp_path, LONG)
    _open_long(engine)
    engine.process([], [tick(C0 + timedelta(minutes=2), "3985.00")], C0 + timedelta(minutes=2, seconds=1))
    (trade,) = engine.store.trades()
    assert trade["exit_reason"] == "stop" and D(trade["exit"]) == D("3984.90")
    assert D(trade["stop_overshoot"]) == D("5.00")
    assert D(trade["loss_beyond_planned"]) > 0  # reported as a risk-limit exception, not hidden


def test_stop_after_a_feed_gap_is_labelled_stop_gap(tmp_path):
    engine = make_engine(tmp_path, LONG)
    engine.health.max_quote_gap = timedelta(seconds=60)
    engine.health.record_status("trading", C0, "test")
    _open_long(engine)
    engine.process([], [tick(C0 + timedelta(minutes=5), "3985.00")], C0 + timedelta(minutes=5, seconds=1))
    assert engine.store.trades()[0]["exit_reason"] == "stop_gap"


def test_short_stop_uses_ask_and_tp_uses_ask(tmp_path):
    plan = {"s": {C0: ("short", "4000.00", "4010.00", "3985.00")}}
    engine = make_engine(tmp_path, plan)
    engine.process([bar(0)], [], C0 + timedelta(seconds=1))
    engine.process([], [tick(C0 + timedelta(seconds=1), "4000.00", "4000.20")], C0 + timedelta(seconds=2))
    assert engine.positions["s.e10000.c0"].entry == D("3999.90")  # bid - slippage
    engine.process([], [tick(C0 + timedelta(minutes=1), "3984.70", "3984.90")], C0 + timedelta(minutes=1, seconds=1))
    assert engine.store.trades()[0]["exit_reason"] == "take_profit"


def test_time_exit_after_max_holding_bars_at_last_tick_of_bar(tmp_path):
    engine = make_engine(tmp_path, LONG)
    _open_long(engine)
    for i in range(1, 5):
        engine.process([], [tick(C0 + (i - 1) * M15 + timedelta(minutes=7), "4001.00")], C0 + (i - 1) * M15
                       + timedelta(minutes=7, seconds=1))
        engine.process([bar(i)], [], bar(i).close_time + timedelta(seconds=1))
    (trade,) = engine.store.trades()
    assert trade["exit_reason"] == "time_exit" and trade["bars_held"] == 4
    assert D(trade["exit"]) == D("4000.90") and trade["closed_at"] == bar(4).close_time.isoformat()


# --------------------------------------------------------------------------- timestamps and integrity

def test_late_processing_rejects_entry_as_system_not_live(tmp_path):
    engine = make_engine(tmp_path, LONG)
    engine.process([bar(0)], [], C0 + timedelta(seconds=1))
    engine.process([], [tick(C0 + timedelta(seconds=1), "4000.00")], C0 + timedelta(minutes=5))
    (decision,) = engine.store.decisions()
    assert not decision["approved"] and any(r.startswith("System not live") for r in decision["reasons"])
    assert not engine.positions


def test_signals_before_unseen_window_are_warmup_only(tmp_path):
    engine = make_engine(tmp_path, LONG, unseen=C0 + timedelta(seconds=1))
    _open_long(engine)
    assert [s["status"] for s in engine.store.signals()] == ["warmup"]
    assert engine.store.decisions() == [] and not engine.positions


def test_forming_bars_and_ticks_beyond_the_next_bar_are_deferred(tmp_path):
    engine = make_engine(tmp_path, LONG)
    counts = engine.process([bar(0), bar(1)], [tick(C0 + 2 * M15, "4000.00")], C0 + timedelta(minutes=5))
    assert counts["bars"] == 1  # bar 1 closes at C0+15m, still forming at wall time C0+5m
    assert counts["deferred_ticks"] == 1 and engine.last_tick is None


def test_pending_signal_expires_without_a_tick_in_the_next_bar(tmp_path):
    engine = make_engine(tmp_path, LONG)
    engine.process([bar(0)], [], C0 + timedelta(seconds=1))
    engine.process([bar(1)], [], bar(1).close_time + timedelta(seconds=1))
    (decision,) = engine.store.decisions()
    assert not decision["approved"] and decision["reasons"][0].startswith("No executable tick")


def test_session_restriction_rejects_entries_while_market_closed(tmp_path):
    closed = replace(OPEN_ALL_WEEK, closures=((C0, C0 + timedelta(hours=1)),))
    engine = make_engine(tmp_path, LONG, calendar=closed)
    _open_long(engine)
    reasons = engine.store.decisions()[0]["reasons"]
    assert any(r.startswith("Market session: XAUUSD closed") for r in reasons)


def test_unverified_session_calendar_fails_closed(tmp_path):
    engine = make_engine(tmp_path, LONG, calendar=replace(OPEN_ALL_WEEK, verification="rule_based"))
    _open_long(engine)
    assert any("not verified against the broker" in r for r in engine.store.decisions()[0]["reasons"])


def test_event_blackout_and_abnormal_spread_reject_without_any_ai(tmp_path):
    engine = make_engine(tmp_path, LONG, guard=lambda now: "Consumer Price Index (USD, high) at 13:30")
    _open_long(engine)
    assert any(r.startswith("Event blackout: Consumer Price Index") for r in engine.store.decisions()[0]["reasons"])
    engine = make_engine(tmp_path / "b", LONG)
    engine.process([bar(0)], [tick(T0 + timedelta(seconds=i), "4000.00") for i in range(40)],
                   C0 + timedelta(seconds=1))
    engine.process([], [tick(C0 + timedelta(seconds=1), "4000.00", "4000.95")], C0 + timedelta(seconds=2))
    assert any("abnormal spread" in r for r in engine.store.decisions()[0]["reasons"])


# --------------------------------------------------------------------------- costs

def test_commission_scenario_reduces_size_and_pnl(tmp_path):
    books = [BookSpec("s.e10000.c0", "s", D("10000"), D("0"), 4), BookSpec("s.e10000.c7", "s", D("10000"), D("7"), 4)]
    engine = make_engine(tmp_path, LONG, books=books)
    _open_long(engine)
    engine.process([], [tick(C0 + timedelta(minutes=3), "4016.00")], C0 + timedelta(minutes=3, seconds=1))
    trades = {t["book_id"]: t for t in engine.store.trades()}
    c0, c7 = trades["s.e10000.c0"], trades["s.e10000.c7"]
    assert D(c7["commission"]) == D(c7["size"]) * 7 and D(c0["commission"]) == 0
    assert D(c7["pnl"]) == D(c7["size"]) * 100 * (D("4015.00") - D("4000.30")) - D(c7["size"]) * 7


def test_swap_charged_per_server_rollover_with_wednesday_triple():
    tue = datetime(2026, 11, 10, 20, tzinfo=UTC)  # server Tue 22:00 (EET)
    assert SWAP.charge(SWAP_SIDE_LONG, D("0.10"), tue, tue + timedelta(hours=1)) == (D("0"), 0)
    cost, nights = SWAP.charge(SWAP_SIDE_LONG, D("0.10"), tue, tue + timedelta(days=2))
    assert nights == 4 and cost == D("0.10") * D("-12.6") * 4


SWAP_SIDE_LONG = __import__("trading.common", fromlist=["Side"]).Side.LONG


def test_spread_comes_from_the_live_tick(tmp_path):
    engine = make_engine(tmp_path, LONG)
    engine.process([bar(0)], [], C0 + timedelta(seconds=1))
    engine.process([], [tick(C0 + timedelta(seconds=1), "4000.00", "4000.55")], C0 + timedelta(seconds=2))
    position = engine.positions["s.e10000.c0"]
    assert position.entry_spread == D("0.55") and position.entry == D("4000.65")


# --------------------------------------------------------------------------- persistence, duplicates, reconciliation

def _scenario():
    plan = {"s": {C0: ("long", "4000.00", "3990.00", "4015.00"), C0 + 2 * M15: ("short", "4004.00", "4014.00",
                                                                                 "3989.00")}}
    steps = [([bar(0)], [], C0 + timedelta(seconds=1)),
             ([], [tick(C0 + timedelta(seconds=1), "4000.00")], C0 + timedelta(seconds=2)),
             ([bar(1)], [tick(C0 + timedelta(minutes=10), "4003.00")], C0 + M15 + timedelta(seconds=1)),
             ([bar(2, 4004.0)], [tick(C0 + M15 + timedelta(minutes=1), "4004.00")], C0 + 2 * M15 + timedelta(seconds=1)),
             ([], [tick(C0 + 2 * M15 + timedelta(seconds=2), "3989.50")], C0 + 2 * M15 + timedelta(seconds=3))]
    return plan, steps


def _snapshot(engine):
    return (engine.store.trades(), engine.store.decisions(), engine.store.positions(), dict(engine.realized),
            [(s["signal_id"], s["status"]) for s in engine.store.signals()])


def test_restart_recovery_matches_uninterrupted_run(tmp_path):
    plan, steps = _scenario()
    one = make_engine(tmp_path / "one", plan)
    for step in steps:
        one.process(*step)
    two = make_engine(tmp_path / "two", plan)
    for step in steps[:2]:
        two.process(*step)
    assert two.positions  # crash with an open simulated position
    two.store.close()
    restarted = make_engine(tmp_path / "two", plan)  # new process: same files
    assert restarted.positions and restarted.reconcile()["ok"] and not restarted.breaker.reasons()
    for step in steps[2:]:
        restarted.process(*step)
    assert _snapshot(restarted) == _snapshot(one)


def test_reprocessing_the_same_data_creates_no_duplicates(tmp_path):
    plan, steps = _scenario()
    engine = make_engine(tmp_path, plan)
    for step in steps + steps:
        engine.process(*step)
    trades = engine.store.trades()
    assert len(trades) == len({(t["book_id"], t["signal_id"]) for t in trades})
    assert len(engine.store.decisions()) == 2 and engine.reconcile()["ok"]
    decision = engine.store.decisions()[0]
    with pytest.raises(sqlite3.IntegrityError):
        engine.store.record_decision(decision["book_id"], decision["signal_id"], dedup_key="x", evaluated_at=T0,
                                     approved=False, size=None, planned_risk=None, reasons=[], context={})


def test_failed_batch_rolls_back_and_state_is_rebuilt(tmp_path):
    engine = make_engine(tmp_path, LONG)
    engine.process([bar(0)], [], C0 + timedelta(seconds=1))
    bad = Quote(symbol="XAUUSD", bid=D("4000"), ask=D("3999"), time=C0 + timedelta(seconds=2))  # crossed
    with pytest.raises(Exception):
        engine.process([], [tick(C0 + timedelta(seconds=1), "4000.00"), bad], C0 + timedelta(seconds=3))
    assert not engine.positions and engine.store.decisions() == [] and len(engine.pending) == 1


def test_reconciliation_detects_tampering_and_trips_breaker(tmp_path):
    engine = make_engine(tmp_path, LONG)
    _open_long(engine)
    engine.process([], [tick(C0 + timedelta(minutes=3), "4016.00")], C0 + timedelta(minutes=3, seconds=1))
    engine.store.set_realized("s.e10000.c0", D("999"))
    report = engine.reconcile()
    assert not report["ok"] and engine.breaker.is_tripped
    assert "reconciliation failed" in engine.breaker.reasons()[0]


def test_book_configuration_is_frozen_across_restarts(tmp_path):
    make_engine(tmp_path, LONG).store.close()
    with pytest.raises(Exception, match="different configuration"):
        make_engine(tmp_path, LONG, books=[BookSpec("s.e10000.c0", "s", D("25000"), D("0"), 4)])


# --------------------------------------------------------------------------- circuit breakers and emergency stop

def test_daily_loss_halt_latches_for_the_rest_of_the_risk_day(tmp_path):
    c2 = C0 + 2 * M15
    plan = {"s": {C0: ("long", "4000.00", "3990.00", "4015.00"), c2: ("long", "3950.00", "3940.00", "3965.00")}}
    engine = make_engine(tmp_path, plan)
    _open_long(engine)
    engine.process([bar(1, 3950.0)], [tick(C0 + timedelta(minutes=5), "3950.00")], C0 + M15 + timedelta(seconds=1))
    loss = -D(engine.store.trades()[0]["pnl"])
    assert loss / D("10000") >= D("0.015")  # 0.04 lot gapped 50 USD past the stop
    engine.process([bar(2, 3950.0)], [tick(C0 + M15 + timedelta(minutes=3), "3950.00")], c2 + timedelta(seconds=1))
    engine.process([], [tick(c2 + timedelta(seconds=1), "3950.00")], c2 + timedelta(seconds=2))
    second = engine.store.decisions("s.e10000.c0")[-1]
    assert not second["approved"] and any(r.startswith("Daily loss halt") for r in second["reasons"])


def test_kill_file_blocks_entries_but_open_positions_keep_their_stops(tmp_path):
    plan = {"s": {C0: ("long", "4000.00", "3990.00", "4015.00"), C0 + M15: ("long", "4001.00", "3991.00", None)}}
    books = [BookSpec("s.e10000.c0", "s", D("10000"), D("0"), None)]
    engine = make_engine(tmp_path, plan, books=books)
    _open_long(engine)
    (tmp_path / "KILL").write_text("stop", encoding="utf-8")
    engine.process([], [tick(C0 + timedelta(minutes=4), "3989.00")], C0 + timedelta(minutes=4, seconds=1))
    assert engine.store.trades()[0]["exit_reason"].startswith("stop")  # simulated broker-side stop still works
    engine.process([bar(1)], [], C0 + M15 + timedelta(seconds=1))
    engine.process([], [tick(C0 + M15 + timedelta(seconds=1), "4001.00")], C0 + M15 + timedelta(seconds=2))
    last = engine.store.decisions()[-1]
    assert not last["approved"] and any("Kill file present" in r for r in last["reasons"])


def test_aggregate_and_concurrency_limits_apply_per_book(tmp_path):
    plan = {"s": {C0: ("long", "4000.00", "3990.00", None), C0 + M15: ("long", "4001.00", "3991.00", None)}}
    engine = make_engine(tmp_path, plan, books=[BookSpec("s.e10000.c0", "s", D("10000"), D("0"), None)])
    _open_long(engine)
    engine.process([bar(1)], [], C0 + M15 + timedelta(seconds=1))
    engine.process([], [tick(C0 + M15 + timedelta(seconds=1), "4001.00")], C0 + M15 + timedelta(seconds=2))
    assert any(r.startswith("Maximum concurrent positions") for r in engine.store.decisions()[-1]["reasons"])


def test_three_thousand_book_rejects_wide_stops_on_minimum_lot(tmp_path):
    plan = {"s": {C0: ("long", "4000.00", "3980.00", None)}}  # 20 USD stop > 14.67 max at 3,000 USD
    books = [BookSpec("s.e3000.c0", "s", D("3000"), D("0"), 4), BookSpec("s.e10000.c0", "s", D("10000"), D("0"), 4)]
    engine = make_engine(tmp_path, plan, books=books)
    _open_long(engine)
    decisions = {d["book_id"]: d for d in engine.store.decisions()}
    assert decisions["s.e10000.c0"]["approved"]
    assert decisions["s.e3000.c0"]["reasons"][0].startswith("Minimum size 0.01 would risk")


# --------------------------------------------------------------------------- demo-only and pre-registration

def test_shadow_code_has_no_order_routing():
    sources = list((ROOT / "trading" / "shadow").glob("*.py")) + [ROOT / "research" / "phase2d_shadow.py"]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for forbidden in ("order_send", "order_check", "order_modify", "positions_close", "api.x.ai"):
            assert forbidden not in text, f"{forbidden} in {path.name}"


def test_read_only_proxy_and_demo_account_are_enforced():
    from test_phase2_connectors import FULL_INFO, FakeMt5
    from research.phase2d_shadow import Mt5Feed
    from trading.connectors.mt5 import Mt5ReadOnlyConnector
    feed = Mt5Feed(Mt5ReadOnlyConnector(server_tz=EET_EU, module=FakeMt5(symbol_info=FULL_INFO)))
    feed.connect()
    for name in ("order_" + "send", "order_check", "Buy", "Sell", "Close", "market_book_add"):
        with pytest.raises(PermissionError):  # incl. the package's convenience order helpers
            getattr(feed.conn.mt5, name)
    real = Mt5Feed(Mt5ReadOnlyConnector(server_tz=EET_EU, module=FakeMt5(trade_mode=2, symbol_info=FULL_INFO)))
    with pytest.raises(LiveTradingForbidden):
        real.connect()
    with pytest.raises(LiveTradingForbidden):
        load_demo_environment({})
    with pytest.raises(LiveTradingForbidden):
        load_demo_environment({"EXECUTION_MODE": "demo", "LIVE_TRADING_ENABLED": "true"})


def test_config_matches_preregistration_and_drift_is_refused(tmp_path):
    from research.phase2d_shadow import build_books, check_preregistration, load_config
    from trading.common import ValidationError
    config, prereg = load_config(ROOT / "research/configs/phase2d_shadow_xauusd.json")
    assert len(build_books(config)) == 7 * 2 * 2 and config["risk"]["risk_fraction"] == "0.005"
    drift = json.loads(json.dumps(config))
    drift["strategies"]["orb_newyork"]["params"]["reward_to_risk"] = 2.0
    with pytest.raises(ValidationError, match="frozen"):
        check_preregistration(drift, prereg)
    drift = json.loads(json.dumps(config))
    drift["risk"]["risk_fraction"] = "0.01"
    with pytest.raises(ValidationError):
        check_preregistration(drift, prereg)
    changed = tmp_path / "prereg.json"
    changed.write_text(json.dumps({**prereg, "unseen_window_starts": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({**config, "preregistration": str(changed)}), encoding="utf-8")
    with pytest.raises(ValidationError, match="Pre-registration file changed"):
        load_config(cfg)


def test_cpi_nfp_calendar_file_is_reproducible_and_timed_in_new_york():
    from research.phase2d_shadow import CPI_NFP_SCHEDULE, cpi_nfp_events
    path = ROOT / "research/forward_test/calendar/us_cpi_nfp_bls_2026_2027.json"
    recorded = json.loads(path.read_text(encoding="utf-8"))
    assert recorded == [e.to_json() for e in cpi_nfp_events("bls.gov/schedule")]
    times = {r["event_id"]: r["scheduled_at"] for r in recorded}
    assert times["us_cpi-2026-10"] == "2026-11-10T13:30:00+00:00"  # 08:30 EST
    assert times["us_cpi-2027-06"] == "2027-07-14T12:30:00+00:00"  # 08:30 EDT
    assert sum(len(rows) for _, _, rows in CPI_NFP_SCHEDULE.values()) == len(recorded) == 28


def test_swap_from_symbol_info_maps_mql_weekday_and_falls_back_unverified():
    from research.phase2d_shadow import swap_from_symbol_info
    fallback = {"long_points_per_lot_night": "-12.6", "short_points_per_lot_night": "-4.6",
                "usd_per_point_per_lot": "1.0", "triple_weekday_server": "Wednesday", "source": "cfg"}
    live = swap_from_symbol_info({"swap_mode": 1, "swap_long": -12.6, "swap_short": -4.6, "swap_rollover3days": 3},
                                 server_tz=EET_EU, usd_per_point=D("1"), fallback=fallback)
    assert live.verified and live.triple_weekday_server == "Wednesday" and live.long_points_per_lot_night == D("-12.6")
    other = swap_from_symbol_info({"swap_mode": 2}, server_tz=EET_EU, usd_per_point=D("1"), fallback=fallback)
    assert not other.verified and "fallback" in other.source


# --------------------------------------------------------------------------- reporting

def _prereg():
    return json.loads((ROOT / "research/forward_test/phase2c_xauusd_preregistration.json").read_text(encoding="utf-8"))


def test_report_without_forward_data_reports_insufficient_data(tmp_path):
    engine = make_engine(tmp_path, {}, books=[BookSpec("orb_newyork.e10000.c7", "orb_newyork", D("10000"), D("7"),
                                                       16)])
    report = build_report(engine.store, _prereg(), now=T0, poll_seconds=5)
    crit = report["preregistered_criteria"]
    assert crit["trades"] == 0 and crit["verdict"].startswith("in progress")
    assert {c["status"] for c in crit["criteria"]} == {"insufficient_data"}
    assert report["orders_placed"] == 0


def test_report_metrics_and_risk_exceptions(tmp_path):
    books = [BookSpec("orb_newyork.e10000.c7", "orb_newyork", D("10000"), D("7"), 16),
             BookSpec("random_s1.e10000.c7", "random_s1", D("10000"), D("7"), 16)]
    plan = {"orb_newyork": LONG["s"], "random_s1": {C0: ("short", "4000.00", "4010.00", "3985.00")}}
    engine = make_engine(tmp_path, plan, books=books)
    _open_long(engine)
    engine.process([], [tick(C0 + timedelta(minutes=3), "4016.00", "4016.20")], C0 + timedelta(minutes=3, seconds=1))
    report = build_report(engine.store, _prereg(), now=C0 + timedelta(hours=1), poll_seconds=5)
    orb, rnd = report["books"]["orb_newyork.e10000.c7"], report["books"]["random_s1.e10000.c7"]
    assert orb["trades"] == 1 and orb["win_rate_pct"] == 100.0 and orb["costs"]["commission"] > 0
    assert rnd["trades"] == 1 and rnd["risk_limit_violations"]  # short stopped through a gap
    crit = preregistered_criteria(engine.store, engine.store.books(), _prereg(), now=C0 + timedelta(hours=1))
    assert crit["criteria"][2]["value"]["random_books_with_trades"] == 1


def test_uptime_counts_heartbeat_gaps(tmp_path):
    store = ShadowStore(tmp_path / "s.sqlite")
    for minute in (0, 1, 2, 30, 31):
        store.heartbeat(T0 + timedelta(minutes=minute))
    result = uptime(store, poll_seconds=5, now=T0 + timedelta(minutes=32))
    assert len(result["downtime"]) == 1 and result["downtime"][0]["minutes"] == 28.0
    assert result["uptime_pct"] == pytest.approx(100 * (1 - 28 / 31), abs=1e-3)


# --------------------------------------------------------------------------- runner (fake read-only feed)

def _event(provider, series, at, category="macro"):
    from trading.marketcal.events import EconomicEvent
    return EconomicEvent(event_id=f"{series.lower()}-{at:%Y%m%d}", name=series, country="US", currency="USD",
                         scheduled_at=at, impact="high", provider=provider, retrieved_at=T0, updated_at=T0,
                         category=category, series=series)


def _calendar_service(tmp_path, *, with_cpi_nfp=True):
    from trading.marketcal.events import EventStore
    from trading.marketcal.service import CalendarProvider, CalendarService, structured_file_provider
    day = timedelta(days=14)
    providers = [
        CalendarProvider("fake-fomc", lambda: [_event("fake-fomc", "FOMC_DECISION", datetime(2026, 12, 9, 19,
                                                                                            tzinfo=UTC), "central_bank")],
                         frozenset({"FOMC_DECISION"}), day),
        CalendarProvider("fake-bea", lambda: [_event("fake-bea", "US_GDP", datetime(2026, 12, 3, 13, 30, tzinfo=UTC)),
                                              _event("fake-bea", "US_PCE", datetime(2026, 12, 4, 15, tzinfo=UTC))],
                         frozenset({"US_GDP", "US_PCE"}), day)]
    if with_cpi_nfp:
        providers.append(structured_file_provider(ROOT / "research/forward_test/calendar/us_cpi_nfp_bls_2026_2027.json",
                                                  provider="bls.gov/schedule", series={"US_CPI", "US_NFP"}))
    return CalendarService(EventStore(tmp_path / "cal.sqlite"), providers)


def test_calendar_guard_fails_closed_without_cpi_nfp_coverage(tmp_path):
    service = _calendar_service(tmp_path, with_cpi_nfp=False)
    service.refresh(T0)
    assert "US_CPI, US_NFP" in service.guard(MARKET)(T0)
    covered = _calendar_service(tmp_path / "b")
    covered.refresh(T0)
    guard = covered.guard(MARKET)
    assert guard(datetime(2026, 11, 10, 13, 45, tzinfo=UTC)).startswith("Consumer Price Index")  # CPI + 15 min
    assert guard(datetime(2026, 11, 10, 16, 0, tzinfo=UTC)) is None


class FakeFeed:
    """SYNTHETIC read-only feed: bars on the NY gold calendar, two ticks per bar. No order methods exist."""

    def __init__(self, bars, *, clock_ok=True):
        self.bars, self.clock_ok, self.connected = bars, clock_ok, False

    def connect(self):
        self.connected = True
        return {"server": "Fake-Demo", "is_demo": True, "currency": "USD", "leverage": 100}

    def reconnect(self):
        return self.connect()

    def specification(self):
        from test_phase2_connectors import FULL_INFO
        from trading.connectors.mt5 import SpecDiscovery
        fields = {"account_currency": "USD", "contract_size": 100.0, "point": 0.01, "tick_size": 0.01,
                  "tick_value_loss": 1.0, "tick_value_loss_used": 1.0, "volume_min": 0.01, "volume_max": 50.0,
                  "volume_step": 0.01, "stops_level_points": 0, "margin_per_lot": 4000.0,
                  "commission_per_lot_round_trip": None}
        assert FULL_INFO  # same broker shape as the connector tests
        return SpecDiscovery("XAUUSD", T0.isoformat(), fields, ["commission_per_lot_round_trip (not exposed)"], [])

    def symbol_info(self):
        return {"trade_mode": 4, "swap_mode": 1, "swap_long": -12.6, "swap_short": -4.6, "swap_rollover3days": 3}

    def verify_clock(self):
        if not self.clock_ok:
            raise ValueError("No fresh tick (market closed)")
        return {"method": "fresh_tick"}

    def closed_bars(self, start, end):
        return [b for b in self.bars if b.open_time >= start and b.close_time <= end]

    def ticks(self, start, end):
        out = []
        for b in self.bars:
            for offset in (timedelta(seconds=1), timedelta(minutes=7)):
                at = b.open_time + offset
                if start <= at < end:
                    out.append(Quote.create(symbol="XAUUSD", bid=b.close, ask=b.close + D("0.20"), time=at))
        return out

    def account_state(self):
        return {"balance": 3000.0, "equity": 3000.0, "trade_mode": 0, "positions_total": 0, "orders_total": 0}

    def shutdown(self):
        self.connected = False


def _feed_bars(until):
    from phase2_helpers import random_walk
    from trading.marketcal.holidays import build_xauusd_calendar
    from trading.sessions import XAUUSD_NY_MARKET_HOURS
    calendar, _ = build_xauusd_calendar(XAUUSD_NY_MARKET_HOURS, datetime(2026, 8, 1).date(), until.date())
    start = datetime(2026, 8, 3, tzinfo=UTC)
    bars = random_walk("XAUUSD", "15m", start, 9000, price=4000.0, vol=0.0008, seed=3, calendar=calendar)
    return [b for b in bars if b.close_time <= until]


@pytest.mark.parametrize("clock_ok", [True, False])
def test_runner_start_warmup_and_poll_end_to_end(tmp_path, monkeypatch, clock_ok):
    from research.phase2d_shadow import Runner, load_config
    monkeypatch.setenv("EXECUTION_MODE", "demo")
    config, prereg = load_config(ROOT / "research/configs/phase2d_shadow_xauusd.json")
    now = [datetime(2026, 11, 10, 15, 7, 30, tzinfo=UTC)]
    feed = FakeFeed(_feed_bars(datetime(2026, 11, 11, tzinfo=UTC)), clock_ok=clock_ok)
    runner = Runner(config, prereg, feed, tmp_path / "state", clock=lambda: now[0],
                    calendar_service=_calendar_service(tmp_path))
    started = runner.start()
    assert runner.engine.calendar.verification == ("broker_verified" if clock_ok else "broker_observed")
    assert runner.store.bar_count("warmup_history") > 5000 and runner.store.bar_count("live") > 0
    assert runner.engine.swap.verified and len(runner.engine.books) == 28 and not runner.breaker.reasons()
    statuses = {s["status"] for s in runner.store.signals()}
    assert "warmup" in statuses
    for _ in range(12):  # three hours of 15-minute polls through the New York session
        now[0] += timedelta(minutes=15)
        runner.poll()
        runner.housekeeping()
    assert runner.engine.reconcile()["ok"]
    decisions = runner.store.decisions()
    live = [s for s in runner.store.signals() if s["status"] != "warmup"]
    assert all(datetime.fromisoformat(s["bar_close"]) >= datetime.fromisoformat(prereg["unseen_window_starts"])
               for s in live)
    if not clock_ok:  # offset not verified -> calendar unverified -> every live entry rejected (fail closed)
        assert decisions and not any(d["approved"] for d in decisions)
    events = {e["kind"] for e in runner.store.events()}
    assert {"mt5_account_baseline", "specification", "calendar_refresh", "warmup_loaded", "session_calendar",
            "mt5_account_check", "reconciliation"} <= events
    assert runner.store.heartbeats() and started["restored"]["signals_mismatch"] == 0
    # Restart: a new runner on the same state resumes without duplicates or a breaker trip.
    runner.store.close()
    again = Runner(config, prereg, FakeFeed(feed.bars, clock_ok=clock_ok), tmp_path / "state", clock=lambda: now[0],
                   calendar_service=_calendar_service(tmp_path / "c2"))
    restored = again.start()["restored"]
    assert restored["signals_mismatch"] == 0 and restored["reconciliation"]["ok"] and not again.breaker.reasons()
