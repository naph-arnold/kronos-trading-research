"""Offline no-lookahead, cost, drawdown and input-integrity tests."""
from datetime import datetime, timedelta, timezone

import pytest

from research.backtest import backtest
from research.market_data import DataValidationError


def fixtures(prices=(100, 102, 104, 99)):
    begin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = []
    for i, value in enumerate(prices):
        candles.append({
            "timestamps": (begin + timedelta(hours=i)).isoformat(),
            "open": str(value), "high": str(value + 3), "low": str(value - 3),
            "close": str(value + 1), "volume": "100", "amount": "1000",
        })
    signals = [
        {"timestamp": candles[i]["timestamps"],
         "previous_close": float(candles[i - 1]["close"]),
         "predicted_close": float(candles[i - 1]["close"]) * 1.01,
         "actual_close": float(candles[i]["close"])}
        for i in range(1, len(candles))
    ]
    return candles, signals


def test_trades_pay_roundtrip_costs():
    candles, signals = fixtures()
    result = backtest(candles, signals, fee_bps=10, spread_bps=2,
                      slippage_bps=3, min_forecast_edge_bps=0)
    assert result["trades"] == 3
    assert result["evaluated_candles"] == 3
    one_way = 4 / 10000
    first_factor = (103 * (1 - one_way) / (102 * (1 + one_way))) * 0.999 ** 2
    assert result["trade_ledger"][0]["net_return"] == pytest.approx(first_factor - 1)
    expected_equity = 10_000
    for row in result["trade_ledger"]:
        expected_equity *= 1 + row["net_return"]
    assert result["final_equity"] == pytest.approx(expected_equity)
    assert 0 <= result["max_drawdown_pct"] <= 100


def test_no_signal_stays_in_cash():
    candles, signals = fixtures()
    for signal in signals:
        signal["predicted_close"] = signal["previous_close"]
    result = backtest(candles, signals)
    assert result["trades"] == 0
    assert result["final_equity"] == 10_000
    assert result["net_return_pct"] == 0


def test_reject_forged_future_actual():
    candles, signals = fixtures()
    signals[0]["actual_close"] += 1
    with pytest.raises(DataValidationError, match="does not match"):
        backtest(candles, signals)


def test_reject_forged_previous_close():
    candles, signals = fixtures()
    signals[0]["previous_close"] += 10
    with pytest.raises(DataValidationError, match="does not match"):
        backtest(candles, signals)


def test_reject_duplicate_or_out_of_order_target():
    candles, signals = fixtures()
    with pytest.raises(DataValidationError, match="chronological"):
        backtest(candles, [signals[1], signals[0]])
    with pytest.raises(DataValidationError, match="chronological"):
        backtest(candles, [signals[0], signals[0]])


def test_reject_missing_target_or_nonfinite_data():
    candles, signals = fixtures()
    signals[0]["timestamp"] = "2040-01-01T00:00:00+00:00"
    with pytest.raises(DataValidationError, match="missing"):
        backtest(candles, signals)
    candles, signals = fixtures()
    signals[0]["predicted_close"] = float("nan")
    with pytest.raises(DataValidationError, match="Nonfinite"):
        backtest(candles, signals)


@pytest.mark.parametrize("kwargs", [
    {"fee_bps": -1}, {"spread_bps": float("nan")}, {"slippage_bps": float("inf")},
    {"initial_equity": 0}, {"fee_bps": 10_000},
])
def test_invalid_cost_inputs(kwargs):
    candles, signals = fixtures()
    with pytest.raises(ValueError):
        backtest(candles, signals, **kwargs)


def test_higher_costs_never_improve_same_trades():
    candles, signals = fixtures()
    no_cost = backtest(candles, signals, fee_bps=0, spread_bps=0, slippage_bps=0)
    high_cost = backtest(candles, signals, fee_bps=20, spread_bps=8, slippage_bps=5)
    assert high_cost["final_equity"] < no_cost["final_equity"]
