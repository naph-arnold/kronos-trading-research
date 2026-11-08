"""BTC/USDT research strategies (Pipeline B). Input: closed H1 bars in UTC. Spot = long/flat only.

Kronos forecasts are *precomputed* with `research.evaluate_kronos.evaluate_rows`
(each target bar predicted only from strictly earlier bars) and looked up by
target bar open time; a forecast for bar t+1 is consulted at the close of bar t.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading.common import Market, Side
from trading.indicators import ADX, ATR, EMA, RollingWindow
from trading.marketdata import Bar
from trading.strategies.base import SignalDraft, StrategyBase, rr_target


@dataclass(frozen=True)
class TrendParams:
    ema_fast: int = 20
    ema_slow: int = 100
    momentum_bars: int = 24
    adx_period: int = 14
    adx_min: float = 18.0
    atr_period: int = 14
    max_atr_pct: float = 3.0  # skip extreme-volatility regimes (ATR as % of price)
    atr_stop_mult: float = 2.0
    reward_to_risk: float | None = 3.0


class TrendFollowing(StrategyBase):
    """Long when EMA fast crosses above EMA slow, price above both, momentum
    (close / close[momentum_bars] - 1) > 0, ADX >= adx_min and ATR% <= max_atr_pct.
    Stop = entry - atr_stop_mult x ATR; optional R-multiple target."""

    strategy_id = "btc-trend"
    version = "1.0.0"
    market = Market.BTCUSDT_BINANCE_SPOT

    def __init__(self, params: TrendParams = TrendParams(), **kwargs):
        super().__init__(params, **kwargs)
        p = params
        self.fast, self.slow = EMA(p.ema_fast), EMA(p.ema_slow)
        self.adx, self.atr = ADX(p.adx_period), ATR(p.atr_period)
        self.closes = RollingWindow(p.momentum_bars + 1)
        self.prev_diff: float | None = None

    def entry_conditions(self, bar: Bar) -> dict | None:
        p = self.params
        close = float(bar.close)
        f, s = self.fast.update(close), self.slow.update(close)
        adx = self.adx.update(float(bar.high), float(bar.low), close)
        atr = self.atr.update(float(bar.high), float(bar.low), close)
        self.closes.update(close)
        if f is None or s is None or adx is None or atr is None or not self.closes.full:
            self.prev_diff = None if f is None or s is None else f - s
            return None
        diff, prev = f - s, self.prev_diff
        self.prev_diff = diff
        momentum = close / self.closes.values[0] - 1
        crossed = prev is not None and prev <= 0 < diff
        if crossed and close > f and momentum > 0 and adx >= p.adx_min and 100 * atr / close <= p.max_atr_pct:
            return {"ema_fast": f, "ema_slow": s, "adx": adx, "atr": atr, "momentum": momentum}
        return None

    def on_bar(self, bar: Bar) -> SignalDraft | None:
        context = self.entry_conditions(bar)
        if context is None:
            return None
        close = float(bar.close)
        stop = close - self.params.atr_stop_mult * context["atr"]
        return SignalDraft(Side.LONG, close, stop, rr_target(Side.LONG, close, stop, self.params.reward_to_risk),
                           context)


@dataclass(frozen=True)
class BreakoutParams:
    lookback: int = 48
    volume_lookback: int = 20
    volume_mult: float = 1.5
    atr_period: int = 14
    min_range_atr: float = 3.0  # range must be meaningful relative to ATR
    max_atr_pct: float = 3.0
    atr_stop_mult: float = 1.5
    reward_to_risk: float | None = 2.0


class Breakout(StrategyBase):
    """Long when the close exceeds the highest high of the previous `lookback` bars,
    volume > volume_mult x average volume of the previous `volume_lookback` bars,
    range width >= min_range_atr x ATR, and ATR% <= max_atr_pct. Stop = entry -
    atr_stop_mult x ATR (risk-sized by the engine); R-multiple target."""

    strategy_id = "btc-breakout"
    version = "1.0.0"
    market = Market.BTCUSDT_BINANCE_SPOT

    def __init__(self, params: BreakoutParams = BreakoutParams(), **kwargs):
        super().__init__(params, **kwargs)
        p = params
        self.highs, self.lows = RollingWindow(p.lookback), RollingWindow(p.lookback)
        self.volumes = RollingWindow(p.volume_lookback)
        self.atr = ATR(p.atr_period)

    def on_bar(self, bar: Bar) -> SignalDraft | None:
        p = self.params
        close, volume = float(bar.close), float(bar.volume)
        atr = self.atr.update(float(bar.high), float(bar.low), close)
        draft = None
        if self.highs.full and self.volumes.full and atr is not None:
            range_high, range_low = max(self.highs.values), min(self.lows.values)
            avg_volume = sum(self.volumes.values) / len(self.volumes.values)
            if (close > range_high and avg_volume > 0 and volume > p.volume_mult * avg_volume
                    and range_high - range_low >= p.min_range_atr * atr and 100 * atr / close <= p.max_atr_pct):
                stop = close - p.atr_stop_mult * atr
                draft = SignalDraft(Side.LONG, close, stop, rr_target(Side.LONG, close, stop, p.reward_to_risk),
                                    {"range": [range_low, range_high], "volume_ratio": volume / avg_volume,
                                     "atr": atr})
        # Window excludes the current bar when tested above, then includes it.
        self.highs.update(float(bar.high))
        self.lows.update(float(bar.low))
        self.volumes.update(volume)
        return draft


@dataclass(frozen=True)
class KronosParams:
    min_edge_bps: float = 20.0
    trend_ema: int | None = 200  # None disables the trend filter
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    reward_to_risk: float | None = None  # exit by time (forecast horizon) unless set


class KronosForecastStrategy(StrategyBase):
    """Long at the close of bar t when Kronos' forecast close for bar t+1 exceeds the
    close of bar t by min_edge_bps (and close > EMA trend filter, if enabled).
    Exit by stop (atr_stop_mult x ATR) or the replay time exit (forecast horizon)."""

    strategy_id = "btc-kronos"
    version = "1.0.0"
    market = Market.BTCUSDT_BINANCE_SPOT

    def __init__(self, params: KronosParams, *, forecasts: dict[datetime, float], **kwargs):
        super().__init__(params, **kwargs)
        self.forecasts = forecasts
        self.trend = EMA(params.trend_ema) if params.trend_ema else None
        self.atr = ATR(params.atr_period)
        self.missing_forecasts = 0

    def on_bar(self, bar: Bar) -> SignalDraft | None:
        p = self.params
        close = float(bar.close)
        trend = self.trend.update(close) if self.trend else None
        atr = self.atr.update(float(bar.high), float(bar.low), close)
        predicted = self.forecasts.get(bar.close_time)  # target = next bar, keyed by its open time
        if predicted is None:
            self.missing_forecasts += 1
            return None
        if atr is None or (self.trend is not None and (trend is None or close <= trend)):
            return None
        edge_bps = (predicted / close - 1) * 10_000
        if edge_bps <= p.min_edge_bps:
            return None
        stop = close - p.atr_stop_mult * atr
        return SignalDraft(Side.LONG, close, stop, rr_target(Side.LONG, close, stop, p.reward_to_risk),
                           {"forecast_close": predicted, "edge_bps": edge_bps, "atr": atr, "trend_ema": trend})


@dataclass(frozen=True)
class KronosFilteredTrendParams:
    trend: TrendParams = TrendParams()
    min_edge_bps: float = 0.0


class KronosFilteredTrend(StrategyBase):
    """TrendFollowing entries kept only when the Kronos forecast for the next bar is
    above the current close by more than min_edge_bps (tests Kronos as a trade filter)."""

    strategy_id = "btc-trend-kronos-filter"
    version = "1.0.0"
    market = Market.BTCUSDT_BINANCE_SPOT

    def __init__(self, params: KronosFilteredTrendParams, *, forecasts: dict[datetime, float], **kwargs):
        super().__init__(params, **kwargs)
        self.inner = TrendFollowing(params.trend, tick_size=self.tick, dataset_id=self.dataset_id)
        self.forecasts = forecasts
        self.filtered_out = 0

    def on_bar(self, bar: Bar) -> SignalDraft | None:
        draft = self.inner.on_bar(bar)
        if draft is None:
            return None
        predicted = self.forecasts.get(bar.close_time)
        if predicted is None or (predicted / float(bar.close) - 1) * 10_000 <= self.params.min_edge_bps:
            self.filtered_out += 1
            return None
        return SignalDraft(draft.side, draft.entry, draft.stop, draft.take_profit,
                           {**draft.context, "forecast_close": predicted})


def buy_and_hold(bars: list[Bar], *, initial_equity: Decimal, fee_rate: Decimal, half_spread: Decimal,
                 slippage: Decimal) -> dict:
    """Benchmark: buy at the first bar open (ask + slippage, taker fee), mark to market
    at the bid, sell at the last close. Fully invested; *not* risk-sized."""
    entry = bars[0].open + half_spread + slippage
    qty = initial_equity / (entry * (1 + fee_rate))
    curve, peak, worst = [], initial_equity, Decimal("0")
    for bar in bars:
        equity = qty * (bar.close - half_spread) * (1 - fee_rate)
        curve.append((bar.close_time, equity))
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
    exit_price = bars[-1].close - half_spread - slippage
    final = qty * exit_price * (1 - fee_rate)
    gross_final = initial_equity / bars[0].open * bars[-1].close
    return {"strategy_id": "btc-buy-and-hold", "initial_equity": initial_equity, "final_equity": final,
            "net_return_pct": (final / initial_equity - 1) * 100,
            "gross_return_pct": (gross_final / initial_equity - 1) * 100,
            "max_drawdown_pct": worst * 100, "trades": 1, "exposure_pct": Decimal("100"),
            "total_costs": gross_final - final, "equity_curve": curve,
            "note": "Fully invested benchmark, not sized by the 0.5% risk engine."}


def forecasts_by_target(report: dict) -> dict[datetime, float]:
    """Map evaluate_rows() output to {target bar open time (UTC): predicted close}."""
    out = {}
    for item in report["forecasts"]:
        moment = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
        out[moment] = float(item["predicted_close"])
    return out


# ---------------------------------------------------------------------------
# Phase 2E candidates (long/flat spot). Exits are expressed in ATR / R terms so that
# matched random-entry controls can reproduce the same exit structure.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DonchianParams:
    entry_lookback: int = 48
    atr_period: int = 14
    atr_stop_mult: float = 2.0
    reward_to_risk: float | None = 2.0


class DonchianBreakout(StrategyBase):
    """Long when the bar closes above the highest high of the previous `entry_lookback` bars
    (the current bar is excluded from the channel). Stop = entry - atr_stop_mult x ATR;
    target = reward_to_risk x risk; the runner applies the configured time exit."""

    strategy_id = "btc-donchian"
    version = "1.0.0"
    market = Market.BTCUSDT_BINANCE_SPOT

    def __init__(self, params: DonchianParams = DonchianParams(), **kwargs):
        super().__init__(params, **kwargs)
        self.highs = RollingWindow(params.entry_lookback)
        self.atr = ATR(params.atr_period)

    def on_bar(self, bar: Bar) -> SignalDraft | None:
        p = self.params
        close = float(bar.close)
        atr = self.atr.update(float(bar.high), float(bar.low), close)
        draft = None
        if self.highs.full and atr is not None:
            channel_high = max(self.highs.values)
            if close > channel_high:
                stop = close - p.atr_stop_mult * atr
                draft = SignalDraft(Side.LONG, close, stop, rr_target(Side.LONG, close, stop, p.reward_to_risk),
                                    {"channel_high": channel_high, "atr": atr})
        self.highs.update(float(bar.high))
        return draft


@dataclass(frozen=True)
class VwapReversionParams:
    vwap_bars: int = 24
    atr_period: int = 14
    entry_atr: float = 2.0  # enter when close <= VWAP - entry_atr x ATR
    atr_stop_mult: float = 1.5
    trend_ema: int | None = None  # optional: only when close > EMA(trend_ema)


class VwapMeanReversion(StrategyBase):
    """Rolling VWAP over the last `vwap_bars` closed bars (typical price x volume / volume).
    Long when the close is at least entry_atr x ATR below that VWAP (and above the trend EMA if
    enabled); target = the VWAP at signal time; stop = entry - atr_stop_mult x ATR."""

    strategy_id = "btc-vwap-reversion"
    version = "1.0.0"
    market = Market.BTCUSDT_BINANCE_SPOT

    def __init__(self, params: VwapReversionParams = VwapReversionParams(), **kwargs):
        super().__init__(params, **kwargs)
        self.pv, self.vol = RollingWindow(params.vwap_bars), RollingWindow(params.vwap_bars)
        self.atr = ATR(params.atr_period)
        self.trend = EMA(params.trend_ema) if params.trend_ema else None

    def on_bar(self, bar: Bar) -> SignalDraft | None:
        p = self.params
        high, low, close, volume = float(bar.high), float(bar.low), float(bar.close), float(bar.volume)
        atr = self.atr.update(high, low, close)
        trend = self.trend.update(close) if self.trend else None
        self.pv.update((high + low + close) / 3 * volume)
        self.vol.update(volume)
        if not self.pv.full or atr is None or sum(self.vol.values) <= 0:
            return None
        if self.trend is not None and (trend is None or close <= trend):
            return None
        vwap = sum(self.pv.values) / sum(self.vol.values)
        if close > vwap - p.entry_atr * atr:
            return None
        stop = close - p.atr_stop_mult * atr
        return SignalDraft(Side.LONG, close, stop, vwap, {"vwap": vwap, "atr": atr, "deviation_atr": (vwap - close) / atr,
                                                          "trend_ema": trend})


class BtcRandomEntry(StrategyBase):
    """Long-only random-entry control for BTC/USDT spot (seeded, reproducible), with an ATR stop and
    R target matching the candidate it controls. Not a trading candidate."""

    strategy_id = "btc-baseline-random"
    version = "1.0.0"
    market = Market.BTCUSDT_BINANCE_SPOT

    def __init__(self, params, **kwargs):
        from trading.strategies.baselines import RandomEntryBaseline
        super().__init__(params, **kwargs)
        if params.long_probability != 1.0:
            raise ValueError("Spot controls are long-only (long_probability must be 1.0)")
        self.inner = RandomEntryBaseline(params, tick_size=self.tick, dataset_id=self.dataset_id)

    def on_bar(self, bar: Bar) -> SignalDraft | None:
        return self.inner.on_bar(bar)
