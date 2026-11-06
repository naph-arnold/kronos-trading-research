"""Indicators and strategies: causality (no lookahead), explicit rules, standardized signals."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from phase2_helpers import btc_h1, xau_m15
from trading.common import Side
from trading.indicators import ADX, ATR, EMA, Resampler, SwingDetector
from trading.marketdata import Bar
from trading.replay import HistoryView
from trading.strategies.crypto import (Breakout, BreakoutParams, KronosFilteredTrend, KronosFilteredTrendParams,
                                       KronosForecastStrategy, KronosParams, TrendFollowing, TrendParams,
                                       buy_and_hold)
from trading.strategies.xauusd import (GoldPilotParams, GoldPilotV1, LiquiditySweepMSS, OpeningRangeBreakout,
                                       OpeningRangeParams, OrderBlockParams, OrderBlockRetest, SweepParams)

UTC = timezone.utc
TICK = D("0.01")


def bar(t, o, h, lo, c, interval="15m", symbol="XAUUSD", v="100"):
    return Bar.create(symbol=symbol, interval=interval, open_time=t, open=o, high=h, low=lo, close=c, volume=v)


# --- Indicators ------------------------------------------------------------------

def test_ema_seeds_with_sma_then_smooths():
    ema = EMA(3)
    assert [ema.update(x) for x in (1, 2, 3)] == [None, None, 2.0]
    assert ema.update(6) == pytest.approx(2.0 + 0.5 * (6 - 2.0))


def test_atr_and_adx_warm_up_and_stay_finite():
    atr, adx = ATR(14), ADX(14)
    bars = btc_h1(200)
    for b in bars:
        atr.update(float(b.high), float(b.low), float(b.close))
        adx.update(float(b.high), float(b.low), float(b.close))
    assert atr.value > 0 and 0 <= adx.value <= 100


def test_swing_confirmed_only_after_right_bars():
    t0 = datetime(2026, 9, 7, 2, tzinfo=UTC)
    highs = [10, 11, 12, 15, 12, 11, 10]
    det = SwingDetector(3, 3)
    found = []
    for i, h in enumerate(highs):
        b = bar(t0 + timedelta(minutes=15 * i), h - 0.5, h, h - 1, h - 0.5)
        found.append(det.update(b))
    assert all(not f for f in found[:6])
    assert found[6][0].kind == "high" and found[6][0].price == 15
    assert found[6][0].confirmed_at == t0 + timedelta(minutes=15 * 7)  # close of the 7th bar


def test_resampler_emits_only_completed_hours_and_partial_on_gap():
    t0 = datetime(2026, 9, 7, 2, tzinfo=UTC)
    rs = Resampler("1h")
    out = [rs.update(bar(t0 + timedelta(minutes=15 * i), 100, 101 + i, 99, 100 + i)) for i in range(4)]
    assert out[:3] == [[], [], []] and out[3][0].high == D("104") and out[3][0].close == D("103")
    rs.update(bar(t0 + timedelta(hours=1), 100, 101, 99, 100))
    emitted = rs.update(bar(t0 + timedelta(hours=2, minutes=45), 100, 101, 99, 100))  # gap -> partial + complete
    assert len(emitted) == 2 and emitted[0].open_time == t0 + timedelta(hours=1)


def test_history_view_blocks_future_access():
    bars = btc_h1(5)
    view = HistoryView(bars, 3)
    assert len(view) == 3 and view[-1] is bars[2] and view[1:] == bars[1:3]
    with pytest.raises(IndexError):
        view[3]
    assert list(view) == bars[:3]


# --- No lookahead: prefix invariance ---------------------------------------------------

def _signals(factory, bars):
    strategy = factory()
    out = {}
    for i in range(len(bars)):
        s = strategy(HistoryView(bars, i + 1))
        if s is not None:
            out[s.signal_id] = (s.side, s.entry_price, s.stop_price, s.take_profit)
    return out


XAU_FACTORIES = [
    lambda: GoldPilotV1(GoldPilotParams(h1_ema_fast=10, h1_ema_slow=30, adx_min=10), tick_size=TICK),
    lambda: OrderBlockRetest(OrderBlockParams(h1_ema_fast=10, h1_ema_slow=30, impulse_atr_mult=1.2), tick_size=TICK),
    lambda: LiquiditySweepMSS(SweepParams(), tick_size=TICK),
    lambda: OpeningRangeBreakout(OpeningRangeParams(session="london", min_range_atr=0.1, max_range_atr=10),
                                 tick_size=TICK),
    lambda: OpeningRangeBreakout(OpeningRangeParams(session="newyork", min_range_atr=0.1, max_range_atr=10),
                                 tick_size=TICK),
]


@pytest.mark.parametrize("factory", XAU_FACTORIES)
def test_xau_strategies_are_causal_and_emit_valid_signals(factory):
    bars = xau_m15(2500)
    full = _signals(factory, bars)
    assert full, "synthetic data should trigger at least one signal"
    cut = len(bars) * 2 // 3
    prefix = _signals(factory, bars[:cut])
    cutoff = bars[cut - 1].close_time
    expected = {k: v for k, v in full.items() if datetime.strptime(k.rsplit("-", 2)[-2], "%Y%m%dT%H%M")
                .replace(tzinfo=UTC) <= cutoff}
    assert prefix == expected  # appending future bars never changes past signals
    for side, entry, stop, tp in full.values():
        assert (stop < entry) if side is Side.LONG else (stop > entry)
        assert stop % TICK == 0 and (tp is None or tp % TICK == 0)


CRYPTO_FACTORIES = [
    lambda: TrendFollowing(TrendParams(ema_fast=10, ema_slow=30, adx_min=5, max_atr_pct=10), tick_size=TICK),
    lambda: Breakout(BreakoutParams(lookback=24, volume_mult=1.0, min_range_atr=1.0, max_atr_pct=10),
                     tick_size=TICK),
]


@pytest.mark.parametrize("factory", CRYPTO_FACTORIES)
def test_crypto_strategies_are_causal_and_long_only(factory):
    bars = btc_h1(1500)
    full = _signals(factory, bars)
    assert full
    prefix = _signals(factory, bars[:900])
    cutoff = bars[899].close_time
    assert prefix == {k: v for k, v in full.items() if datetime.strptime(k.rsplit("-", 2)[-2], "%Y%m%dT%H%M")
                      .replace(tzinfo=UTC) <= cutoff}
    assert all(side is Side.LONG for side, *_ in full.values())


def test_goldpilot_signal_carries_provenance():
    strategy = XAU_FACTORIES[0]()
    bars = xau_m15(2500)
    for i in range(len(bars)):
        signal = strategy(HistoryView(bars, i + 1))
        if signal:
            assert signal.generated_at == signal.bar_close_time == bars[i].close_time
            assert signal.provenance["params"]["h1_ema_fast"] == 10
            assert "h1_ema_fast" in signal.provenance["context"]
            assert signal.strategy_id == "xau-goldpilot-v1"
            return
    pytest.fail("no signal")


def test_orb_uses_local_session_time_across_dst():
    # London 08:00 local = 07:00 UTC in summer (BST), 08:00 UTC in winter (GMT).
    strat = OpeningRangeBreakout(OpeningRangeParams(session="london"), tick_size=TICK)
    assert strat.tz.to_server(datetime(2026, 7, 1, 7, tzinfo=UTC)).hour == 8
    assert strat.tz.to_server(datetime(2026, 12, 1, 8, tzinfo=UTC)).hour == 8
    ny = OpeningRangeBreakout(OpeningRangeParams(session="newyork"), tick_size=TICK)
    assert ny.tz.to_server(datetime(2026, 7, 1, 12, 20, tzinfo=UTC)).hour == 8
    with pytest.raises(ValueError):
        OpeningRangeBreakout(OpeningRangeParams(session="tokyo"), tick_size=TICK)


def test_order_block_rules_explicit_scenario():
    """Bearish candle, then bullish impulse closing above its high, then a retest that holds."""
    t0 = datetime(2026, 9, 7, 2, tzinfo=UTC)
    strat = OrderBlockRetest(OrderBlockParams(h1_ema_fast=2, h1_ema_slow=3, impulse_atr_mult=1.5, atr_period=3),
                             tick_size=TICK)
    rows = []
    price = 100.0
    for i in range(20):  # gentle uptrend for H1 context and ATR warm-up
        rows.append((price, price + 0.6, price - 0.4, price + 0.3))
        price += 0.3
    rows += [(price, price + 0.1, price - 0.9, price - 0.8),          # bearish OB candle
             (price - 0.8, price + 3.5, price - 0.9, price + 3.2),    # impulse closing above OB high
             (price + 3.2, price + 3.3, price + 1.0, price + 1.2),    # pulls back, still above zone
             (price + 0.3, price + 1.3, price - 0.2, price + 0.9)]    # trades into zone, closes bullish above low
    bars = [bar(t0 + timedelta(minutes=15 * i), *r) for i, r in enumerate(rows)]
    signals = [strat(HistoryView(bars, i + 1)) for i in range(len(bars))]
    hit = [s for s in signals if s is not None]
    assert hit and hit[-1].side is Side.LONG
    assert hit[-1].stop_price < D(repr(price - 0.9))  # below zone low minus buffer


def test_kronos_uses_forecast_for_next_bar_only():
    bars = btc_h1(300)
    # Forecast for bar i+1 (keyed by its open time) made from bars <= i.
    forecasts = {nxt.open_time: float(cur.close) * 1.01 for cur, nxt in zip(bars, bars[1:])}
    strat = KronosForecastStrategy(KronosParams(min_edge_bps=20, trend_ema=None), forecasts=forecasts,
                                   tick_size=TICK)
    seen = None
    for i in range(len(bars) - 1):
        s = strat(HistoryView(bars, i + 1))
        if s:
            seen = (i, s)
            break
    i, s = seen
    assert s.provenance["context"]["forecast_close"] == forecasts[bars[i].close_time]
    assert bars[i].close_time == bars[i + 1].open_time
    no_forecast = KronosForecastStrategy(KronosParams(trend_ema=None), forecasts={}, tick_size=TICK)
    assert all(no_forecast(HistoryView(bars, j + 1)) is None for j in range(50))
    assert no_forecast.missing_forecasts == 50


def test_kronos_filter_removes_trend_entries_with_negative_forecast():
    bars = btc_h1(1500)
    params = TrendParams(ema_fast=10, ema_slow=30, adx_min=5, max_atr_pct=10)
    bearish = {nxt.open_time: float(cur.close) * 0.99 for cur, nxt in zip(bars, bars[1:])}
    strat = KronosFilteredTrend(KronosFilteredTrendParams(trend=params), forecasts=bearish, tick_size=TICK)
    assert all(strat(HistoryView(bars, i + 1)) is None for i in range(len(bars)))
    assert strat.filtered_out > 0


def test_buy_and_hold_includes_costs():
    bars = btc_h1(100)
    result = buy_and_hold(bars, initial_equity=D("10000"), fee_rate=D("0.001"), half_spread=D("1"),
                          slippage=D("5"))
    assert result["final_equity"] < D("10000") * bars[-1].close / bars[0].open
    assert result["total_costs"] > 0 and result["exposure_pct"] == 100
