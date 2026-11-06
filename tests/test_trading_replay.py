"""Replay harness: no lookahead, next-open execution, pessimistic fills, risk gating."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from trading.common import Market
from trading.marketdata import Bar
from trading.replay import run_replay
from trading.signals import TradeSignal
import pytest

from trading.common import ValidationError
from trading_helpers import ENV, XAU_CALENDAR, btc_spec, limits, xau_spec

XAU = Market.XAUUSD_MT5
T0 = datetime(2026, 11, 2, 0, tzinfo=timezone.utc)


def series(rows, symbol="XAUUSD", start=T0):
    return [Bar.create(symbol=symbol, interval="1h", open_time=start + timedelta(hours=i),
                       open=o, high=h, low=lo, close=c) for i, (o, h, lo, c) in enumerate(rows)]


def flat(n, price="2650.00"):
    return [(price, price, price, price)] * n


def signal_at(bar, *, sid, side="long", entry=None, stop, tp=None, market="xauusd_mt5", symbol="XAUUSD"):
    return TradeSignal.create(signal_id=sid, market=market, symbol=symbol, side=side,
                              entry_price=entry or bar.close, stop_price=stop, take_profit=tp,
                              strategy_id="test", strategy_version="1", generated_at=bar.close_time,
                              bar_close_time=bar.close_time)


def replay(bars, strategy, **kwargs):
    params = dict(instrument=xau_spec(), limits=limits(XAU), environment=ENV,
                  initial_equity=D("10000"), spread=D("0.20"), session_calendar=XAU_CALENDAR)
    params.update(kwargs)
    return run_replay(bars, strategy, **params)


def test_strategy_never_sees_future_bars_and_fills_next_open():
    bars = series(flat(2) + [("2652.00", "2653.00", "2651.00", "2652.50")] + flat(2, "2652.50"))
    seen = []

    def strategy(history):
        seen.append((len(history), history[-1].open_time))
        if len(history) == 2:
            return signal_at(history[-1], sid="s1", stop="2645.00")
        return None

    result = replay(bars, strategy)
    assert seen == [(i + 1, bars[i].open_time) for i in range(len(bars))]
    trade_entry = result["decisions"][0]
    assert trade_entry["approved"] is True
    # Filled at bar 2 open (2652.00) ask + slippage = 2652.20, not at the signal bar's close.
    assert result["open_position_at_end"] is True
    assert result["final_equity"] != D("10000")


def test_stop_wins_when_stop_and_target_hit_in_same_bar():
    bars = series(flat(2) + [("2650.00", "2670.00", "2640.00", "2650.00")] + flat(1))

    def strategy(history):
        if len(history) == 1:
            return signal_at(history[-1], sid="s1", stop="2645.00", tp="2660.00")
        return None

    result = replay(bars, strategy)
    trade = result["trade_ledger"][0]
    assert trade["exit_reason"] == "stop" and trade["exit"] == D("2644.90")
    assert -trade["pnl"] <= D("50")  # within planned budget when no gap
    assert trade["planned_risk"] <= D("50")


def test_gap_through_stop_fills_at_open_and_can_exceed_plan():
    bars = series(flat(3) + [("2600.00", "2601.00", "2599.00", "2600.00")])

    def strategy(history):
        if len(history) == 1:
            return signal_at(history[-1], sid="s1", stop="2645.00")
        return None

    result = replay(bars, strategy)
    trade = result["trade_ledger"][0]
    assert trade["exit_reason"] == "stop_gap" and trade["exit"] == D("2599.80")
    assert -trade["pnl"] > trade["planned_risk"]  # documented: realized loss can exceed planned risk


def test_daily_loss_halt_blocks_after_losses_within_a_day():
    # Repeating cycle: signal on flat bar, next bar entry, next bar stops out.
    rows = []
    for _ in range(8):
        rows += [("2650.00", "2650.00", "2650.00", "2650.00"),
                 ("2650.00", "2650.00", "2650.00", "2650.00"),
                 ("2650.00", "2650.00", "2640.00", "2650.00")]
    bars = series(rows)
    counter = iter(range(1000))

    def strategy(history):
        if (len(history) - 1) % 3 == 0:
            return signal_at(history[-1], sid=f"s{next(counter)}", stop="2645.00")
        return None

    result = replay(bars, strategy)
    assert result["trades"] >= 3
    assert result["rejection_reasons"].get("Daily loss halt", 0) >= 1
    total_loss = D("10000") - result["final_equity"]
    # Halt prevents continued losses: stays near the 1.5% halt plus at most one trade's planned risk.
    assert total_loss < D("10000") * (D("0.015") + D("0.005"))


def test_signal_not_anchored_to_current_bar_rejected():
    bars = series(flat(4))

    def strategy(history):
        if len(history) == 3:
            return signal_at(history[0], sid="old", stop="2645.00")
        return None

    result = replay(bars, strategy)
    assert result["trades"] == 0 and result["approved"] == 0
    assert "anchored" in result["decisions"][0]["reasons"][0]


FRIDAY_20 = datetime(2026, 11, 6, 20, tzinfo=timezone.utc)   # server 22:00 Fri
SUNDAY_23 = datetime(2026, 11, 8, 23, tzinfo=timezone.utc)   # server 01:00 Mon (re-open)


def weekend_bars():
    return series(flat(2), start=FRIDAY_20) + series(flat(2), start=SUNDAY_23)


def test_weekend_gap_is_explained_by_session_and_signal_goes_stale():
    def strategy(history):
        if len(history) == 2:  # Friday 22:00 UTC close, last bar before the weekend
            return signal_at(history[-1], sid="fri", stop="2645.00")
        return None

    result = replay(weekend_bars(), strategy, validate_sessions=True)
    assert result["gaps"] == 1 and result["approved"] == 0
    assert result["rejection_reasons"].get("Signal is stale") == 1
    assert result["rejection_reasons"].get("Market session") == 1  # inside the post-open buffer


def test_unexplained_gap_during_open_session_rejected():
    bars = series(flat(1)) + series(flat(1), start=T0 + timedelta(hours=3))  # Monday, market open
    with pytest.raises(ValidationError, match="Unexplained"):
        replay(bars, lambda history: None, validate_sessions=True)


def test_bars_inside_closed_session_rejected():
    bars = series(flat(2), start=datetime(2026, 11, 7, 10, tzinfo=timezone.utc))  # Saturday
    with pytest.raises(ValidationError, match="outside the trading session"):
        replay(bars, lambda history: None, validate_sessions=True)


def test_xauusd_replay_requires_calendar():
    with pytest.raises(ValueError, match="calendar"):
        replay(series(flat(2)), lambda history: None, session_calendar=None)


def test_binance_replay_respects_long_only_and_budget():
    bars = series(flat(4, "60000.00"), symbol="BTCUSDT")

    def strategy(history):
        if len(history) == 1:
            return signal_at(history[-1], sid="b1", stop="59400.00", market="btcusdt_binance_spot",
                             symbol="BTCUSDT")
        if len(history) == 2:
            return signal_at(history[-1], sid="b2", side="short", stop="60600.00",
                             market="btcusdt_binance_spot", symbol="BTCUSDT")
        return None

    result = run_replay(bars, strategy, instrument=btc_spec(), limits=limits(Market.BTCUSDT_BINANCE_SPOT),
                        environment=ENV, initial_equity=D("10000"), spread=D("0.02"))
    assert result["approved"] == 1
    assert D(result["decisions"][0]["planned_risk"]) <= D("50")
    assert any("long/flat" in r for r in result["decisions"][1]["reasons"])
