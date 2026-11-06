"""Deterministic risk engine acceptance tests (ROADMAP Phase 1)."""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from trading.common import Market, Side, ValidationError, is_multiple
from trading.risk import HARD_MAX_RISK_FRACTION, evaluate
from trading.state import OpenPosition
from trading_helpers import (ENV, NOW, XAU_CALENDAR, account, at, breaker, btc_quote, btc_signal, btc_spec, limits, xau_quote,
                             xau_signal, xau_spec)

XAU, BTC = Market.XAUUSD_MT5, Market.BTCUSDT_BINANCE_SPOT


def run_xau(signal=None, *, acct=None, spec=None, quote=None, lim=None, brk=None, now=NOW, env=ENV, dup=None,
            calendar=XAU_CALENDAR):
    return evaluate(signal or xau_signal(), account=acct or account(XAU), instrument=spec or xau_spec(),
                    quote=quote or xau_quote(), limits=lim or limits(XAU), environment=env,
                    breaker=brk or breaker(XAU), now=now, duplicate_check=dup, session_calendar=calendar)


def run_btc(signal=None, *, acct=None, spec=None, quote=None, lim=None, brk=None, now=NOW, env=ENV):
    return evaluate(signal or btc_signal(), account=acct or account(BTC), instrument=spec or btc_spec(),
                    quote=quote or btc_quote(), limits=lim or limits(BTC), environment=env,
                    breaker=brk or breaker(BTC), now=now)


def reasons_text(decision) -> str:
    return " | ".join(decision.reasons)


# --- Sizing -----------------------------------------------------------------

def test_xauusd_size_uses_tick_value_spread_slippage_and_commission():
    decision = run_xau()
    assert decision.approved, decision.reasons
    # entry ask 2650.10 + 0.10 slip; stop 2645.00 - 0.10 slip -> 5.30 price units
    # 1 lot: 5.30 / 0.01 * $1 = $530 + $7 commission = $537; budget $50 -> 0.0931 -> 0.09 lots
    assert decision.loss_per_unit == D("537")
    assert decision.risk_budget == D("50.000")
    assert decision.size == D("0.09")
    assert decision.planned_risk == D("48.33")
    assert decision.planned_risk <= D("10000") * D("0.005")


def test_binance_size_respects_step_size_fees_and_slippage():
    decision = run_btc()
    assert decision.approved, decision.reasons
    # adverse entry 60005.01, adverse stop 59395.00 -> 610.01; fees 0.001*(60005.01+59395) = 119.40001
    assert decision.loss_per_unit == D("729.41001")
    assert decision.size == D("0.06854")
    assert is_multiple(decision.size, D("0.00001"))
    assert decision.planned_risk <= decision.risk_budget == D("50.000")


@pytest.mark.parametrize("equity", ["731.37", "10000", "12345.67", "250000"])
@pytest.mark.parametrize("stop", ["2649.40", "2647.13", "2640.00", "2600.55", "2500.00"])
def test_xauusd_size_never_exceeds_half_percent_of_equity(equity, stop):
    eq = D(equity)
    decision = run_xau(xau_signal(stop_price=stop, take_profit=None),
                       acct=account(XAU, equity=eq, balance=eq, available_funds=eq * 100, day_start_equity=eq))
    if decision.approved:
        assert decision.planned_risk <= eq * HARD_MAX_RISK_FRACTION
        assert is_multiple(decision.size, D("0.01")) and decision.size >= D("0.01")
    else:
        assert "Minimum size" in reasons_text(decision)


@pytest.mark.parametrize("equity", ["120", "5000", "98765.43"])
@pytest.mark.parametrize("stop", ["59990.00", "59000.00", "54000.00"])
def test_binance_size_never_exceeds_half_percent_of_equity(equity, stop):
    eq = D(equity)
    decision = run_btc(btc_signal(stop_price=stop),
                       acct=account(BTC, equity=eq, balance=eq, available_funds=eq, day_start_equity=eq))
    if decision.approved:
        assert decision.planned_risk <= eq * HARD_MAX_RISK_FRACTION
        assert is_multiple(decision.size, D("0.00001"))
    else:
        # Tight stops can require more notional than spot balance allows: rejected, not shrunk.
        assert any(k in reasons_text(decision) for k in ("Minimum size", "Notional", "Insufficient free USDT"))


def test_under_minimum_lot_is_rejected_not_rounded_up():
    # 0.01 lot with a ~50 price-unit stop risks $50.37 > $50 budget.
    decision = run_xau(xau_signal(stop_price="2600.00", take_profit=None))
    assert not decision.approved and decision.size == 0
    assert "Minimum size 0.01 would risk" in reasons_text(decision)


def test_costs_alone_can_push_minimum_size_over_budget():
    decision = run_xau(spec=xau_spec(commission_per_lot_round_trip=D("5000")))
    assert not decision.approved and "Minimum size" in reasons_text(decision)


def test_binance_min_notional_rejected():
    eq = D("50")  # budget $0.25 -> 0.00004 BTC -> ~$2.40 notional < $5 minimum
    decision = run_btc(btc_signal(stop_price="54000.00"),
                       acct=account(BTC, equity=eq, balance=eq, available_funds=eq, day_start_equity=eq))
    assert not decision.approved and "Notional" in reasons_text(decision)


def test_size_capped_at_venue_maximum_rounded_down():
    decision = run_xau(spec=xau_spec(volume_max=D("0.05")))
    assert decision.approved and decision.size == D("0.05")


def test_risk_fraction_above_hard_cap_is_rejected():
    decision = run_xau(lim=limits(XAU, risk_fraction=D("0.0051")))
    assert not decision.approved and "risk_fraction" in reasons_text(decision)


def test_lower_risk_fraction_is_allowed():
    decision = run_xau(lim=limits(XAU, risk_fraction=D("0.0025")))
    assert decision.approved and decision.size == D("0.04")


# --- Stops -------------------------------------------------------------------

def test_missing_or_wrong_side_stop_cannot_form_a_signal():
    with pytest.raises(ValidationError):
        xau_signal(stop_price=None)
    with pytest.raises(ValidationError):
        xau_signal(stop_price="2655.00")
    with pytest.raises(ValidationError):
        xau_signal(side="short", stop_price="2645.00", take_profit=None)


def test_market_already_through_stop_rejected():
    decision = run_xau(quote=xau_quote(bid="2644.90", ask="2645.10"))
    assert not decision.approved
    assert "through stop" in reasons_text(decision)


def test_stop_inside_broker_stops_level_rejected():
    # stops level 20 points = 0.20; bid 2649.90 - stop 2649.80 = 0.10
    decision = run_xau(xau_signal(stop_price="2649.80", take_profit=None))
    assert not decision.approved and "below venue minimum" in reasons_text(decision)


def test_stop_not_on_tick_grid_rejected():
    decision = run_xau(xau_signal(stop_price="2645.005", take_profit=None))
    assert not decision.approved and "tick size" in reasons_text(decision)


def test_short_xauusd_supported_with_stop_above():
    signal = xau_signal(side="short", entry_price="2650.00", stop_price="2655.00", take_profit="2640.00")
    decision = run_xau(signal)
    assert decision.approved and decision.effective_entry == D("2649.90")
    assert decision.planned_risk <= D("50")


def test_binance_short_rejected_spot_is_long_flat():
    signal = btc_signal(side="short", entry_price="60000.00", stop_price="60600.00")
    decision = run_btc(signal)
    assert not decision.approved and "long/flat" in reasons_text(decision)


# --- Freshness ----------------------------------------------------------------

def test_stale_signal_rejected():
    decision = run_xau(now=at(minutes=6), quote=xau_quote(time=at(minutes=6)),
                       acct=account(XAU, snapshot_at=at(minutes=6)))
    assert not decision.approved and "Signal is stale" in reasons_text(decision)


def test_stale_quote_rejected():
    decision = run_xau(quote=xau_quote(time=at(seconds=-11)))
    assert not decision.approved and "Quote is stale" in reasons_text(decision)


def test_future_dated_quote_rejected():
    decision = run_xau(quote=xau_quote(time=at(seconds=30)))
    assert not decision.approved and "future-dated" in reasons_text(decision)


def test_stale_account_snapshot_rejected():
    decision = run_xau(acct=account(XAU, snapshot_at=at(seconds=-31)))
    assert not decision.approved and "Account snapshot is stale" in reasons_text(decision)


def test_wide_spread_rejected():
    decision = run_xau(quote=xau_quote(bid="2649.50", ask="2650.10"))
    assert not decision.approved and "Spread" in reasons_text(decision)


def test_price_moved_away_from_signal_entry_rejected():
    decision = run_xau(quote=xau_quote(bid="2656.90", ask="2657.10"))
    assert not decision.approved and "moved" in reasons_text(decision)


# --- Account state, breakers, guardrails --------------------------------------

def test_unreconciled_account_rejected():
    decision = run_xau(acct=account(XAU, reconciled=False))
    assert not decision.approved and "not reconciled" in reasons_text(decision)


def test_non_demo_account_rejected():
    decision = run_xau(acct=account(XAU, is_demo=False))
    assert not decision.approved and "not a demo account" in reasons_text(decision)


def test_circuit_breaker_and_kill_file_block(tmp_path):
    brk = breaker(XAU, kill_file=tmp_path / "KILL")
    assert run_xau(brk=brk).approved
    (tmp_path / "KILL").write_text("halt")
    assert "Kill file" in reasons_text(run_xau(brk=brk))
    (tmp_path / "KILL").unlink()
    brk.trip("feed dropped")
    assert "feed dropped" in reasons_text(run_xau(brk=brk))
    with pytest.raises(ValueError):
        brk.reset(operator=" ")
    brk.reset(operator="ops")
    assert run_xau(brk=brk).approved


def test_daily_loss_halt_at_one_point_five_percent():
    halted = run_xau(acct=account(XAU, equity=D("9850"), day_start_equity=D("10000")))
    assert not halted.approved and "Daily loss halt" in reasons_text(halted)
    allowed = run_xau(acct=account(XAU, equity=D("9851"), day_start_equity=D("10000")))
    assert allowed.approved


def _position(risk):
    return OpenPosition("p1", "XAUUSD", Side.LONG, D("0.05"), D("2600"), D("2590"), risk)


def test_aggregate_open_stop_risk_cap_shrinks_then_blocks():
    lim = limits(XAU, max_open_positions=5)
    full = run_xau(lim=lim, acct=account(XAU, open_positions=(_position(D("50")),)))
    assert full.approved and full.risk_budget == D("50.000")
    shrunk = run_xau(lim=lim, acct=account(XAU, open_positions=(_position(D("80")),)))
    assert shrunk.approved and shrunk.risk_budget == D("20.00") and shrunk.size == D("0.03")
    blocked = run_xau(lim=lim, acct=account(XAU, open_positions=(_position(D("100")),)))
    assert not blocked.approved and "Aggregate" in reasons_text(blocked)


def test_open_position_without_known_stop_blocks_new_orders():
    lim = limits(XAU, max_open_positions=5)
    pos = OpenPosition("p1", "XAUUSD", Side.LONG, D("0.05"), D("2600"), None, None)
    decision = run_xau(lim=lim, acct=account(XAU, open_positions=(pos,)))
    assert not decision.approved and "without a known stop" in reasons_text(decision)


def test_concurrent_position_limit():
    decision = run_xau(acct=account(XAU, open_positions=(_position(D("10")),)))
    assert not decision.approved and "Maximum concurrent positions" in reasons_text(decision)


def test_pending_orders_block_new_orders():
    decision = run_xau(acct=account(XAU, pending_order_ids=("ord-1",)))
    assert not decision.approved and "Pending orders" in reasons_text(decision)


def test_insufficient_margin_rejected():
    decision = run_xau(acct=account(XAU, available_funds=D("400")))
    assert not decision.approved and "Margin" in reasons_text(decision)


def test_insufficient_usdt_balance_rejected():
    decision = run_btc(acct=account(BTC, available_funds=D("1000")))
    assert not decision.approved and "Insufficient free USDT" in reasons_text(decision)


def test_duplicate_check_rejects():
    decision = run_xau(dup=lambda signal: True)
    assert not decision.approved and "Duplicate" in reasons_text(decision)


# --- Fail-closed behaviour ----------------------------------------------------

def test_mixed_market_inputs_rejected():
    decision = evaluate(xau_signal(), account=account(BTC), instrument=xau_spec(), quote=xau_quote(),
                        limits=limits(XAU), environment=ENV, breaker=breaker(XAU), now=NOW)
    assert not decision.approved and "different markets" in reasons_text(decision)


def test_account_currency_mismatch_rejected():
    decision = run_xau(acct=account(XAU, currency="EUR"))
    assert not decision.approved and "account currency" in reasons_text(decision)


@pytest.mark.parametrize("env", [None, object(), {"EXECUTION_MODE": "demo"}])
def test_unverified_environment_rejected(env):
    decision = run_xau(env=env)
    assert not decision.approved and "demo environment" in reasons_text(decision)


@pytest.mark.parametrize("field,value", [("account", None), ("quote", "bad"), ("spec", object())])
def test_garbage_inputs_never_raise_and_reject(field, value):
    if field == "account":
        decision = evaluate(xau_signal(), account=None, instrument=xau_spec(), quote=xau_quote(),
                            limits=limits(XAU), environment=ENV, breaker=breaker(XAU), now=NOW)
    else:
        decision = run_xau(**{field: value})
    assert not decision.approved and decision.size == 0 and decision.reasons


def test_invalid_instrument_metadata_rejected():
    decision = run_xau(spec=xau_spec(tick_value_loss=D("0")))
    assert not decision.approved and "Invalid instrument" in reasons_text(decision)


def test_decision_record_is_json_ready_and_never_live():
    record = run_xau().to_record()
    assert record["live_trading_enabled"] is False
    assert record["size"] == "0.09"


# --- Sessions and latched daily halt -----------------------------------------

def test_xauusd_without_session_calendar_rejected():
    decision = run_xau(calendar=None)
    assert not decision.approved and "calendar required" in reasons_text(decision)


def test_xauusd_rejected_when_session_closed():
    # Saturday 2026-11-07 12:00 UTC: weekend.
    saturday = NOW.replace(day=7, hour=12)
    signal = xau_signal(generated_at=saturday, bar_close_time=saturday)
    decision = run_xau(signal, now=saturday, quote=xau_quote(time=saturday),
                       acct=account(XAU, snapshot_at=saturday))
    assert not decision.approved and "Market session" in reasons_text(decision)


def test_xauusd_rejected_inside_daily_break_and_close_buffer():
    # Thursday 22:30 UTC = server Fri 00:30 (daily break); Thursday 21:50 UTC = 10 min before the break.
    for moment in (NOW.replace(day=5, hour=22, minute=30, second=0), NOW.replace(day=5, hour=21, minute=50, second=0)):
        signal = xau_signal(generated_at=moment, bar_close_time=moment.replace(minute=0))
        decision = run_xau(signal, now=moment, quote=xau_quote(time=moment), acct=account(XAU, snapshot_at=moment))
        assert not decision.approved and "Market session" in reasons_text(decision), moment


def test_wrong_symbol_calendar_rejected():
    from trading.sessions import BTCUSDT_ALWAYS_OPEN
    decision = run_xau(calendar=BTCUSDT_ALWAYS_OPEN)
    assert not decision.approved and "calendar does not match" in reasons_text(decision)


def test_latched_daily_halt_rejects_even_after_recovery():
    decision = run_xau(acct=account(XAU, equity=D("10200"), daily_loss_halted=True))
    assert not decision.approved and "latched" in reasons_text(decision)
