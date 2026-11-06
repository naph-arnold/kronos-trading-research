"""Persistent circuit breaker and daily-loss accounting."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from trading.breaker import CircuitBreaker
from trading.clock import NY_CLOSE
from trading.common import Market, ValidationError
from trading.daily import DailyLossLedger, DayBoundary
from trading_helpers import account

XAU, BTC = Market.XAUUSD_MT5, Market.BTCUSDT_BINANCE_SPOT
UTC = timezone.utc


def utc(*args):
    return datetime(*args, tzinfo=UTC)


# --- Circuit breaker persistence ----------------------------------------------------

def test_trip_survives_restart_and_reset_is_recorded(tmp_path):
    path = tmp_path / "breaker.json"
    CircuitBreaker(XAU, state_path=path).trip("desync detected")
    restarted = CircuitBreaker(XAU, state_path=path)
    assert restarted.is_tripped and restarted.reasons() == ["desync detected"]
    restarted.reset(operator="alice")
    again = CircuitBreaker(XAU, state_path=path)
    assert not again.is_tripped
    events = [entry["event"] for entry in again.history()]
    assert events == ["trip", "reset"] and again.history()[-1]["operator"] == "alice"


@pytest.mark.parametrize("content", ["{not json", json.dumps({"version": 99, "market": "xauusd_mt5", "trips": []}),
                                     json.dumps({"version": 1, "market": "btcusdt_binance_spot", "trips": []}),
                                     json.dumps({"version": 1, "market": "xauusd_mt5", "trips": "x"})])
def test_corrupt_or_foreign_state_fails_closed(tmp_path, content):
    path = tmp_path / "breaker.json"
    path.write_text(content, encoding="utf-8")
    brk = CircuitBreaker(XAU, state_path=path)
    assert brk.is_tripped and "unreadable" in brk.reasons()[0]
    brk.reset(operator="ops")
    assert not CircuitBreaker(XAU, state_path=path).is_tripped


def test_missing_state_file_is_clear_and_markets_isolated(tmp_path):
    xau = CircuitBreaker(XAU, state_path=tmp_path / "xau.json")
    btc = CircuitBreaker(BTC, state_path=tmp_path / "btc.json")
    xau.trip("gold feed stale")
    assert xau.is_tripped and not btc.is_tripped


# --- Daily loss accounting -------------------------------------------------------------

def ledger(path=":memory:", market=XAU, boundary=None, halt="0.015"):
    return DailyLossLedger(path, market=market, boundary=boundary or DayBoundary.server(NY_CLOSE),
                           halt_fraction=D(halt))


def test_first_observation_sets_start_and_halt_latches():
    led = ledger()
    first = led.observe("demo-1", D("10000"), utc(2026, 11, 4, 10))
    assert first.start_equity == D("10000") and first.estimated_start and not first.halted
    assert not led.observe("demo-1", D("9851"), utc(2026, 11, 4, 11)).halted
    halted = led.observe("demo-1", D("9850"), utc(2026, 11, 4, 12))
    assert halted.halted and halted.loss_fraction == D("0.015")
    recovered = led.observe("demo-1", D("10300"), utc(2026, 11, 4, 13))
    assert recovered.halted, "halt must stay latched for the rest of the risk day"


def test_next_server_day_resets_latch_and_carries_conservative_start():
    led = ledger()
    led.observe("demo-1", D("10000"), utc(2026, 11, 4, 10))
    led.observe("demo-1", D("9800"), utc(2026, 11, 4, 21, 59))  # halted, server Wed 23:59
    new_day = led.observe("demo-1", D("9700"), utc(2026, 11, 4, 22, 1))  # server Thu 00:01
    assert new_day.trading_day == "2026-11-05" and new_day.trading_day != "2026-11-04"
    assert new_day.start_equity == D("9800")  # max(first obs today 9700, last obs yesterday 9800)
    assert not new_day.halted and not new_day.estimated_start


def test_server_versus_utc_day_boundaries():
    before, after = utc(2026, 11, 4, 21, 59), utc(2026, 11, 4, 22, 1)
    server = DayBoundary.server(NY_CLOSE)
    assert server.trading_day(before) != server.trading_day(after)
    assert DayBoundary.utc().trading_day(before) == DayBoundary.utc().trading_day(after)
    summer = (utc(2026, 7, 1, 20, 59), utc(2026, 7, 1, 21, 1))  # NY 17:00 = 21:00 UTC in summer
    assert server.trading_day(summer[0]) != server.trading_day(summer[1])


def test_cash_flows_are_not_counted_as_pnl():
    led = ledger(market=BTC, boundary=DayBoundary.utc())
    led.observe("tn-1", D("10000"), utc(2026, 11, 4, 1))
    led.record_cash_flow("tn-1", D("-1000"), utc(2026, 11, 4, 2))  # e.g. testnet balance change
    state = led.observe("tn-1", D("8950"), utc(2026, 11, 4, 3))
    assert state.start_equity == D("9000") and not state.halted
    unrecorded = ledger(market=BTC, boundary=DayBoundary.utc())
    unrecorded.observe("tn-1", D("10000"), utc(2026, 11, 4, 1))
    assert unrecorded.observe("tn-1", D("8950"), utc(2026, 11, 4, 3)).halted  # conservative if not recorded
    with pytest.raises(ValidationError):
        led.record_cash_flow("tn-1", D("-20000"), utc(2026, 11, 4, 4))


def test_out_of_order_and_invalid_observations_rejected():
    led = ledger()
    led.observe("demo-1", D("10000"), utc(2026, 11, 4, 10))
    with pytest.raises(ValidationError):
        led.observe("demo-1", D("10000"), utc(2026, 11, 4, 9))
    for bad in (D("0"), D("-5"), "NaN"):
        with pytest.raises(ValidationError):
            led.observe("demo-1", bad, utc(2026, 11, 4, 11))
    with pytest.raises(ValidationError):
        led.record_cash_flow("demo-1", D("5"), utc(2026, 11, 5, 23))  # no observation that day


def test_ledger_persists_across_restart_and_rejects_boundary_change(tmp_path):
    path = tmp_path / "daily.sqlite"
    first = ledger(path)
    first.observe("demo-1", D("10000"), utc(2026, 11, 4, 10))
    first.observe("demo-1", D("9800"), utc(2026, 11, 4, 11))
    first.close()
    reopened = ledger(path)
    state = reopened.observe("demo-1", D("10050"), utc(2026, 11, 4, 12))
    assert state.start_equity == D("10000") and state.halted
    reopened.close()
    # A UTC boundary maps 2026-11-04 12:00 to the same day key; mismatch must fail closed.
    with pytest.raises(ValidationError, match="boundary"):
        ledger(path, boundary=DayBoundary.utc()).observe("demo-1", D("10000"), utc(2026, 11, 4, 13))


def test_accounts_are_isolated():
    led = ledger()
    led.observe("a", D("10000"), utc(2026, 11, 4, 10))
    assert led.observe("a", D("9000"), utc(2026, 11, 4, 11)).halted
    assert not led.observe("b", D("9000"), utc(2026, 11, 4, 11)).halted


def test_apply_overrides_snapshot_fields():
    led = ledger()
    snap = account(XAU, snapshot_at=utc(2026, 11, 4, 10), day_start_equity=D("1"))
    applied = led.apply(snap)
    assert applied.day_start_equity == D("10000") and applied.daily_loss_halted is False
    with pytest.raises(ValidationError):
        led.apply(account(BTC, snapshot_at=utc(2026, 11, 4, 11)))


def test_invalid_halt_fraction():
    with pytest.raises(ValidationError):
        ledger(halt="1.5")
    with pytest.raises(ValidationError):
        ledger(halt="0")
