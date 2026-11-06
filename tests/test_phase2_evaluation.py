"""Walk-forward/holdout protocol, metrics, and replay cost accounting."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from phase2_helpers import btc_h1, xau_m15
from trading.common import Market
from trading.evaluation import (TradingWindow, by_regime, chronological_split, classify, metrics, param_grid,
                                regime_labels, walk_forward, walk_forward_splits)
from trading.replay import run_replay
from trading.signals import TradeSignal
from trading.strategies.crypto import TrendFollowing, TrendParams
from trading.strategies.xauusd import GoldPilotParams, GoldPilotV1
from trading_helpers import ENV, XAU_CALENDAR, btc_spec, limits, xau_spec

UTC = timezone.utc
BTC = Market.BTCUSDT_BINANCE_SPOT


def btc_replay(bars, strategy, **kw):
    return run_replay(bars, strategy, instrument=btc_spec(), limits=limits(BTC), environment=ENV,
                      initial_equity=D("10000"), spread=D("0.02"), close_at_end=True, **kw)


def test_holdout_and_walk_forward_splits_never_overlap():
    research_end, n = chronological_split(1000, holdout_fraction=0.2)
    assert (research_end, n) == (800, 1000)
    splits = walk_forward_splits(research_end, train=300, test=100)
    assert len(splits) == 5
    for s in splits:
        assert s.train[1] == s.test[0] and s.test[1] <= research_end
    assert walk_forward_splits(800, train=300, test=100, anchored=True)[-1].train[0] == 0
    with pytest.raises(ValueError):
        chronological_split(1000, holdout_fraction=0.6)
    with pytest.raises(ValueError):
        walk_forward_splits(100, train=90, test=20)


def test_trading_window_blocks_signals_outside_test_period():
    bars = btc_h1(600)
    strat = TrendFollowing(TrendParams(ema_fast=10, ema_slow=30, adx_min=5, max_atr_pct=10), tick_size=D("0.01"))
    window = TradingWindow(strat, bars[400].open_time, bars[500].open_time)
    result = btc_replay(bars, window)
    for trade in result["trade_ledger"]:
        assert bars[400].open_time <= trade["opened_at"] <= bars[501].open_time


def test_param_grid_and_walk_forward_select_on_train_only():
    bars = btc_h1(2400)
    base = TrendParams(adx_min=5, max_atr_pct=10)
    candidates = param_grid(base, {"ema_fast": [5, 10], "ema_slow": [30, 50]})
    assert len(candidates) == 4 and {c.ema_fast for c in candidates} == {5, 10}
    seen_lengths = []

    def run(segment, strategy):
        seen_lengths.append((segment[0].open_time, segment[-1].close_time))
        return btc_replay(segment, strategy)

    splits = walk_forward_splits(2400, train=1000, test=400)
    wf = walk_forward(bars, make_strategy=lambda p: TrendFollowing(p, tick_size=D("0.01")), candidates=candidates,
                      run=run, interval="1h", splits=splits, warmup_bars=100, min_trades=1)
    assert wf["oos_windows"] == len(splits)
    for w, split in zip(wf["windows"], splits):
        # Every training run used only bars from the training segment.
        assert w["train"][1] == bars[split.train[1] - 1].close_time.isoformat()
        assert w["selected"] in candidates
    train_runs = [r for r in seen_lengths if r[1] <= bars[splits[0].train[1] - 1].close_time]
    assert len(train_runs) >= len(candidates)


def test_metrics_net_gross_costs_and_annualization():
    bars = btc_h1(2400)
    strat = TrendFollowing(TrendParams(ema_fast=10, ema_slow=30, adx_min=5, max_atr_pct=10), tick_size=D("0.01"))
    result = btc_replay(bars, strat)
    m = metrics(result, interval="1h")
    assert m["trades"] == result["trades"] > 0
    costs = sum(t["costs"] for t in result["trade_ledger"])
    assert costs > 0 and m["total_costs"] == pytest.approx(float(costs), rel=1e-6)
    net_from_trades = sum(float(t["pnl"]) for t in result["trade_ledger"])
    assert m["net_return_pct"] == pytest.approx(100 * net_from_trades / 10000, abs=1e-4)  # rounded to 4 dp
    assert m["gross_return_pct"] >= m["net_return_pct"]
    assert m["annualized_return_pct"] is not None and m["span_days"] >= 90
    short = metrics(btc_replay(bars[:500], strat.__class__(strat.params, tick_size=D("0.01"))), interval="1h")
    assert short["annualized_return_pct"] is None  # not meaningful under 90 days


def test_every_trade_respects_half_percent_planned_risk():
    bars = btc_h1(2400)
    strat = TrendFollowing(TrendParams(ema_fast=10, ema_slow=30, adx_min=5, max_atr_pct=10), tick_size=D("0.01"))
    result = btc_replay(bars, strat)
    equity = {d["signal_id"]: D(d["risk_budget"]) for d in result["decisions"] if d.get("approved")}
    for trade in result["trade_ledger"]:
        assert trade["planned_risk"] <= equity[trade["signal_id"]]
        assert equity[trade["signal_id"]] <= D("10000") * 3 * D("0.005")  # budget is 0.5% of current equity


def test_time_exit_and_event_guard_in_replay():
    bars = btc_h1(50)

    def every_bar(history):
        b = history[-1]
        if len(history) in (10, 20):
            return TradeSignal.create(signal_id=f"s{len(history)}", market="btcusdt_binance_spot", symbol="BTCUSDT",
                                      side="long", entry_price=b.close, stop_price=(b.close * D("0.97")).quantize(
                                          D("0.01")), strategy_id="t", strategy_version="1",
                                      generated_at=b.close_time, bar_close_time=b.close_time)
        return None

    blocked_time = bars[20].open_time
    result = btc_replay(bars, every_bar, max_holding_bars=2,
                        event_guard=lambda now: "test event" if now == blocked_time else None)
    assert result["trades"] == 1 and result["trade_ledger"][0]["exit_reason"] in ("time_exit", "stop")
    assert result["trade_ledger"][0]["bars_held"] <= 2
    assert result["rejection_reasons"].get("Event blackout") == 1


def test_regime_labels_are_causal():
    bars = btc_h1(800)
    full, prefix = regime_labels(bars), regime_labels(bars[:500])
    assert all(full[k] == v for k, v in prefix.items())
    assert {v.split("-")[0] for v in full.values() if v != "warmup"} <= {"up", "down"}
    summary = by_regime([{"opened_at": bars[300].close_time, "pnl": D("5")}], full)
    assert sum(v["trades"] for v in summary.values()) == 1


def test_classification_never_execution_ready():
    good_holdout = {"net_return_pct": 5.0, "trades": 40}
    good_wf = {"oos_windows": 4, "oos_windows_profitable": 3, "oos_compounded_return_pct": 4.0}
    assert classify(good_holdout, good_wf, broker_spec_verified=False).startswith("experimental")
    assert "not authorized" in classify(good_holdout, good_wf, broker_spec_verified=True)
    assert classify({**good_holdout, "trades": 10}, good_wf, broker_spec_verified=True).startswith("experimental")


def test_xau_replay_with_session_calendar_and_spread_model():
    bars = xau_m15(1500)
    strat = GoldPilotV1(GoldPilotParams(h1_ema_fast=10, h1_ema_slow=30, adx_min=10), tick_size=D("0.01"))
    result = run_replay(bars, strat, instrument=xau_spec(), limits=limits(Market.XAUUSD_MT5), environment=ENV,
                        initial_equity=D("10000"), spread=lambda bar: D("0.30"), session_calendar=XAU_CALENDAR,
                        validate_sessions=True, allow_gaps=True, close_at_end=True)
    assert result["gaps"] > 0
    for trade in result["trade_ledger"]:
        assert trade["planned_risk"] <= D("10000") * 2 * D("0.005")
        assert XAU_CALENDAR.is_open(trade["opened_at"])
    assert metrics(result, interval="15m")["trades"] == result["trades"]
    assert datetime(2026, 9, 7, tzinfo=UTC) <= result["start"] < result["end"]
    assert result["end"] - result["start"] > timedelta(days=5)
