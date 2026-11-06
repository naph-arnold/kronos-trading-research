"""Recorded venue fixture and backtest reproducibility (deterministic, offline)."""
from __future__ import annotations

import json
from decimal import Decimal as D
from pathlib import Path

from phase2_helpers import btc_h1
from trading.common import Market
from trading.connectors.binance import parse_symbol
from trading.evaluation import config_hash, metrics, param_grid, walk_forward, walk_forward_splits
from trading.replay import run_replay
from trading.strategies.crypto import TrendFollowing, TrendParams
from trading_helpers import ENV, limits

FIXTURE = Path(__file__).parent / "data" / "binance_exchangeinfo_btcusdt_20261106.json"


def recorded_spec():
    return parse_symbol(json.loads(FIXTURE.read_text(encoding="utf-8"))["symbol"],
                        source="binance_public_market_data (RECORDED FIXTURE)")


def test_recorded_binance_filters_parse_and_size_through_risk_engine():
    discovery = recorded_spec()
    assert discovery.fields["status"] == "TRADING" and not discovery.missing and not discovery.problems
    assert not discovery.verified  # fee still assumed -> never treated as fully verified
    spec = discovery.to_instrument_spec(allow_assumed_fee=True)
    assert spec.tick_size == D("0.01") and spec.step_size == D("0.00001") and spec.min_notional == D("5")


def _run(bars):
    spec = recorded_spec().to_instrument_spec(allow_assumed_fee=True)
    strategy = TrendFollowing(TrendParams(ema_fast=10, ema_slow=30, adx_min=5, max_atr_pct=10), tick_size=spec.tick_size)
    return run_replay(bars, strategy, instrument=spec, limits=limits(Market.BTCUSDT_BINANCE_SPOT), environment=ENV,
                      initial_equity=D("10000"), spread=D("0.02"), close_at_end=True)


def test_replay_is_bit_for_bit_reproducible():
    bars = btc_h1(1500)
    a, b = _run(bars), _run(bars)
    assert a["trade_ledger"] == b["trade_ledger"] and a["equity_curve"] == b["equity_curve"]
    assert metrics(a, interval="1h") == metrics(b, interval="1h")


def test_walk_forward_selection_is_reproducible_and_config_hash_stable():
    bars = btc_h1(2000)
    spec = recorded_spec().to_instrument_spec(allow_assumed_fee=True)
    candidates = param_grid(TrendParams(adx_min=5, max_atr_pct=10), {"ema_fast": [5, 10], "ema_slow": [30]})

    def run(segment, strategy):
        return run_replay(segment, strategy, instrument=spec, limits=limits(Market.BTCUSDT_BINANCE_SPOT),
                          environment=ENV, initial_equity=D("10000"), spread=D("0.02"), close_at_end=True)

    kwargs = dict(make_strategy=lambda p: TrendFollowing(p, tick_size=spec.tick_size), candidates=candidates, run=run,
                  interval="1h", splits=walk_forward_splits(2000, train=800, test=400), warmup_bars=100, min_trades=1)
    first, second = walk_forward(bars, **kwargs), walk_forward(bars, **kwargs)
    strip = lambda wf: [(w["selected"], w["test_metrics"]) for w in wf["windows"]]  # noqa: E731
    assert strip(first) == strip(second)
    assert config_hash({"a": 1, "b": [1, 2]}) == config_hash({"b": [1, 2], "a": 1})
