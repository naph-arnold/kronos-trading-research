"""XAUUSD research strategies (Pipeline A). Input: closed M15 bars in UTC.

Higher-timeframe (H1) context is built from completed M15 bars only, via
`Resampler`, so no strategy can use an unfinished hour. All rules are explicit
and parameterized; none is assumed profitable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trading.clock import LONDON, NEW_YORK, ServerTimeZone
from trading.common import Market, Side
from trading.indicators import ADX, ATR, EMA, Resampler, Swing, SwingDetector
from trading.marketdata import Bar
from trading.strategies.base import SignalDraft, StrategyBase, rr_target


class _H1Trend:
    """H1 trend state: up if close > EMA fast > EMA slow (and ADX >= threshold), down mirrored."""

    def __init__(self, fast: int, slow: int, adx_period: int, adx_min: float | None):
        self.resampler = Resampler("1h")
        self.fast, self.slow, self.adx = EMA(fast), EMA(slow), ADX(adx_period)
        self.adx_min = adx_min
        self.last_close: float | None = None

    def update(self, bar: Bar) -> None:
        for h1 in self.resampler.update(bar):
            close = float(h1.close)
            self.fast.update(close)
            self.slow.update(close)
            self.adx.update(float(h1.high), float(h1.low), close)
            self.last_close = close

    @property
    def direction(self) -> Side | None:
        f, s, c = self.fast.value, self.slow.value, self.last_close
        if f is None or s is None or c is None:
            return None
        if self.adx_min is not None and (self.adx.value is None or self.adx.value < self.adx_min):
            return None
        if c > f > s:
            return Side.LONG
        if c < f < s:
            return Side.SHORT
        return None

    def snapshot(self) -> dict:
        return {"h1_ema_fast": self.fast.value, "h1_ema_slow": self.slow.value, "h1_adx": self.adx.value,
                "h1_close": self.last_close}


# ---------------------------------------------------------------------------
# Strategy 1 — GoldPilot V1
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GoldPilotParams:
    h1_ema_fast: int = 50
    h1_ema_slow: int = 200
    adx_period: int = 14
    adx_min: float = 20.0
    m15_pullback_ema: int = 20
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    reward_to_risk: float = 2.0


class GoldPilotV1(StrategyBase):
    """H1 trend (close > EMA50 > EMA200, ADX >= adx_min) + M15 pullback entry.

    Long: H1 uptrend; the previous M15 bar's low touched or crossed the M15 pullback
    EMA; the current M15 bar closes back above that EMA and is bullish (close > open).
    Stop = entry - atr_stop_mult x ATR(M15); target = reward_to_risk x risk. Short mirrored.
    """

    strategy_id = "xau-goldpilot-v1"
    version = "1.0.0"
    market = Market.XAUUSD_MT5

    def __init__(self, params: GoldPilotParams = GoldPilotParams(), **kwargs):
        super().__init__(params, **kwargs)
        p = params
        self.trend = _H1Trend(p.h1_ema_fast, p.h1_ema_slow, p.adx_period, p.adx_min)
        self.ema, self.atr = EMA(p.m15_pullback_ema), ATR(p.atr_period)
        self.prev: Bar | None = None
        self.prev_ema: float | None = None

    def on_bar(self, bar: Bar) -> SignalDraft | None:
        p = self.params
        self.trend.update(bar)
        prev, prev_ema = self.prev, self.prev_ema
        ema = self.ema.update(float(bar.close))
        atr = self.atr.update(float(bar.high), float(bar.low), float(bar.close))
        self.prev, self.prev_ema = bar, ema
        direction = self.trend.direction
        if direction is None or prev is None or prev_ema is None or ema is None or atr is None:
            return None
        close = float(bar.close)
        if direction is Side.LONG:
            touched = float(prev.low) <= prev_ema
            confirmed = close > ema and bar.close > bar.open
            if touched and confirmed:
                stop = close - p.atr_stop_mult * atr
                return SignalDraft(Side.LONG, close, stop, rr_target(Side.LONG, close, stop, p.reward_to_risk),
                                   {"m15_ema": ema, "m15_atr": atr, **self.trend.snapshot()})
        else:
            touched = float(prev.high) >= prev_ema
            confirmed = close < ema and bar.close < bar.open
            if touched and confirmed:
                stop = close + p.atr_stop_mult * atr
                return SignalDraft(Side.SHORT, close, stop, rr_target(Side.SHORT, close, stop, p.reward_to_risk),
                                   {"m15_ema": ema, "m15_atr": atr, **self.trend.snapshot()})
        return None


# ---------------------------------------------------------------------------
# Strategy 2 — Order Block Retest
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OrderBlockParams:
    h1_ema_fast: int = 50
    h1_ema_slow: int = 200
    atr_period: int = 14
    impulse_atr_mult: float = 1.5
    zone_expiry_bars: int = 96  # 24h of M15 bars
    stop_buffer_atr: float = 0.25
    reward_to_risk: float = 2.0
    max_zone_atr: float = 2.0  # ignore zones wider than this many ATRs


@dataclass
class _Zone:
    side: Side
    low: float
    high: float
    created_at: datetime
    age: int = 0


class OrderBlockRetest(StrategyBase):
    """Explicit order-block definition (M15):

    Bullish block: the last bearish bar (close < open) immediately followed by an
    impulse bar whose range >= impulse_atr_mult x ATR and whose close exceeds the
    bearish bar's high. Zone = [bearish low, bearish high], active from the impulse
    bar's close. A zone is invalidated by any close below its low, by expiry after
    zone_expiry_bars, or after one trade. Entry: in an H1 uptrend, a later bar trades
    into the zone (low <= zone high), closes above the zone low and is bullish.
    Stop = zone low - stop_buffer_atr x ATR; target = reward_to_risk x risk.
    Bearish blocks are mirrored.
    """

    strategy_id = "xau-orderblock-retest"
    version = "1.0.0"
    market = Market.XAUUSD_MT5

    def __init__(self, params: OrderBlockParams = OrderBlockParams(), **kwargs):
        super().__init__(params, **kwargs)
        p = params
        self.trend = _H1Trend(p.h1_ema_fast, p.h1_ema_slow, 14, None)
        self.atr = ATR(p.atr_period)
        self.prev: Bar | None = None
        self.zones: list[_Zone] = []
        self.invalidated = 0

    def on_bar(self, bar: Bar) -> SignalDraft | None:
        p = self.params
        self.trend.update(bar)
        prev_atr = self.atr.value
        atr = self.atr.update(float(bar.high), float(bar.low), float(bar.close))
        hi, lo, close = float(bar.high), float(bar.low), float(bar.close)
        draft = None

        # 1. Try retests of zones that were active *before* this bar.
        direction = self.trend.direction
        surviving = []
        for zone in self.zones:
            zone.age += 1
            broken = (zone.side is Side.LONG and close < zone.low) or (zone.side is Side.SHORT and close > zone.high)
            if broken or zone.age > p.zone_expiry_bars:
                self.invalidated += broken
                continue
            if draft is None and atr is not None and direction is zone.side:
                if zone.side is Side.LONG and lo <= zone.high and close > zone.low and bar.close > bar.open:
                    stop = zone.low - p.stop_buffer_atr * atr
                    draft = SignalDraft(Side.LONG, close, stop, rr_target(Side.LONG, close, stop, p.reward_to_risk),
                                        {"zone": [zone.low, zone.high], "zone_created": zone.created_at.isoformat(),
                                         "atr": atr, **self.trend.snapshot()})
                    continue  # one trade per zone
                if zone.side is Side.SHORT and hi >= zone.low and close < zone.high and bar.close < bar.open:
                    stop = zone.high + p.stop_buffer_atr * atr
                    draft = SignalDraft(Side.SHORT, close, stop,
                                        rr_target(Side.SHORT, close, stop, p.reward_to_risk),
                                        {"zone": [zone.low, zone.high], "zone_created": zone.created_at.isoformat(),
                                         "atr": atr, **self.trend.snapshot()})
                    continue
            surviving.append(zone)
        self.zones = surviving

        # 2. Detect a new block formed by (prev bar, this impulse bar); usable from the next bar on.
        prev = self.prev
        if prev is not None and prev_atr is not None and hi - lo >= p.impulse_atr_mult * prev_atr:
            width = float(prev.high - prev.low)
            if width <= p.max_zone_atr * prev_atr:
                if prev.close < prev.open and close > float(prev.high):
                    self.zones.append(_Zone(Side.LONG, float(prev.low), float(prev.high), bar.close_time))
                elif prev.close > prev.open and close < float(prev.low):
                    self.zones.append(_Zone(Side.SHORT, float(prev.low), float(prev.high), bar.close_time))
        self.prev = bar
        return draft


# ---------------------------------------------------------------------------
# Strategy 3 — Liquidity Sweep + Market Structure Shift
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SweepParams:
    swing_left: int = 3
    swing_right: int = 3
    mss_window_bars: int = 12
    atr_period: int = 14
    stop_buffer_atr: float = 0.2
    reward_to_risk: float = 2.0


@dataclass
class _Sweep:
    side: Side  # trade direction after the sweep
    extreme: float  # sweep low (bullish) or high (bearish)
    structure_level: float  # swing high (bullish) / low (bearish) that must be closed beyond
    age: int = 0


class LiquiditySweepMSS(StrategyBase):
    """Swings are fractal pivots confirmed `swing_right` bars later (no lookahead).

    Bullish: a bar trades below the latest *confirmed* swing low but closes back above
    it (sweep). Within mss_window_bars a close above the latest confirmed swing high
    known at sweep time is the market-structure shift -> long. Invalidated if price
    closes below the sweep extreme first or the window expires. Stop = sweep extreme -
    stop_buffer_atr x ATR; target = reward_to_risk x risk. Bearish mirrored.
    """

    strategy_id = "xau-liquidity-sweep-mss"
    version = "1.0.0"
    market = Market.XAUUSD_MT5

    def __init__(self, params: SweepParams = SweepParams(), **kwargs):
        super().__init__(params, **kwargs)
        p = params
        self.swings = SwingDetector(p.swing_left, p.swing_right)
        self.atr = ATR(p.atr_period)
        self.last_high: Swing | None = None
        self.last_low: Swing | None = None
        self.pending: list[_Sweep] = []

    def on_bar(self, bar: Bar) -> SignalDraft | None:
        p = self.params
        atr = self.atr.update(float(bar.high), float(bar.low), float(bar.close))
        hi, lo, close = float(bar.high), float(bar.low), float(bar.close)
        draft = None
        # 1. Progress existing sweeps (MSS or invalidation).
        keep = []
        for sweep in self.pending:
            sweep.age += 1
            if sweep.side is Side.LONG:
                if close < sweep.extreme or sweep.age > p.mss_window_bars:
                    continue
                if draft is None and atr is not None and close > sweep.structure_level:
                    stop = sweep.extreme - p.stop_buffer_atr * atr
                    draft = SignalDraft(Side.LONG, close, stop, rr_target(Side.LONG, close, stop, p.reward_to_risk),
                                        {"sweep_low": sweep.extreme, "mss_level": sweep.structure_level, "atr": atr})
                    continue
            else:
                if close > sweep.extreme or sweep.age > p.mss_window_bars:
                    continue
                if draft is None and atr is not None and close < sweep.structure_level:
                    stop = sweep.extreme + p.stop_buffer_atr * atr
                    draft = SignalDraft(Side.SHORT, close, stop,
                                        rr_target(Side.SHORT, close, stop, p.reward_to_risk),
                                        {"sweep_high": sweep.extreme, "mss_level": sweep.structure_level, "atr": atr})
                    continue
            keep.append(sweep)
        self.pending = keep
        # 2. New sweeps against swings confirmed before this bar.
        if self.last_low and self.last_high and lo < self.last_low.price < close:
            self.pending.append(_Sweep(Side.LONG, lo, self.last_high.price))
            self.last_low = None  # a swing can be swept once
        if self.last_high and self.last_low and hi > self.last_high.price > close:
            self.pending.append(_Sweep(Side.SHORT, hi, self.last_low.price))
            self.last_high = None
        # 3. Swings confirmed by this bar become usable from the next bar.
        for swing in self.swings.update(bar):
            if swing.kind == "high":
                self.last_high = swing
            else:
                self.last_low = swing
        return draft


# ---------------------------------------------------------------------------
# Strategy 4 — Opening Range Breakout
# ---------------------------------------------------------------------------

SESSIONS: dict[str, tuple[ServerTimeZone, int]] = {
    "london": (LONDON, 8 * 60),     # 08:00 London local time
    "newyork": (NEW_YORK, 8 * 60 + 20),  # 08:20 New York local time (COMEX gold open outcry legacy)
}


@dataclass(frozen=True)
class OpeningRangeParams:
    session: str = "london"
    range_minutes: int = 30
    entry_window_minutes: int = 180
    atr_period: int = 14
    breakout_buffer_atr: float = 0.1
    min_range_atr: float = 0.5
    max_range_atr: float = 3.0
    stop_at: str = "opposite"  # "opposite" side of range or "mid"
    reward_to_risk: float = 1.5


class OpeningRangeBreakout(StrategyBase):
    """Opening range = high/low of M15 bars in [session open, open + range_minutes)
    in the session's local time (DST-aware via ServerTimeZone).

    Volatility filter: min_range_atr <= range width / ATR(M15) <= max_range_atr.
    Breakout: within entry_window_minutes after the range ends, the first M15 *close*
    beyond the range by breakout_buffer_atr x ATR (closing confirmation filters wicks;
    false breakouts are those that later hit the stop). One trade per session-day.
    Stop: opposite range side or midpoint. Target = reward_to_risk x risk.
    """

    version = "1.0.0"
    market = Market.XAUUSD_MT5

    def __init__(self, params: OpeningRangeParams = OpeningRangeParams(), **kwargs):
        super().__init__(params, **kwargs)
        if params.session not in SESSIONS or params.stop_at not in ("opposite", "mid"):
            raise ValueError("Unknown session or stop mode")
        self.strategy_id = f"xau-orb-{params.session}"
        self.tz, self.open_minute = SESSIONS[params.session]
        self.atr = ATR(params.atr_period)
        self.day = None
        self.range_high: float | None = None
        self.range_low: float | None = None
        self.traded = False

    def on_bar(self, bar: Bar) -> SignalDraft | None:
        p = self.params
        atr = self.atr.update(float(bar.high), float(bar.low), float(bar.close))
        local = self.tz.to_server(bar.open_time)
        if local.date() != self.day:
            self.day, self.range_high, self.range_low, self.traded = local.date(), None, None, False
        minute = local.hour * 60 + local.minute
        range_end = self.open_minute + p.range_minutes
        if self.open_minute <= minute < range_end:
            self.range_high = max(self.range_high or float(bar.high), float(bar.high))
            self.range_low = min(self.range_low or float(bar.low), float(bar.low))
            return None
        if (self.traded or atr is None or self.range_high is None
                or not range_end <= minute < range_end + p.entry_window_minutes):
            return None
        width = self.range_high - self.range_low
        if not p.min_range_atr * atr <= width <= p.max_range_atr * atr:
            return None
        close, buffer = float(bar.close), p.breakout_buffer_atr * atr
        mid = (self.range_high + self.range_low) / 2
        context = {"session": p.session, "range": [self.range_low, self.range_high], "atr": atr,
                   "local_time": local.isoformat()}
        if close > self.range_high + buffer:
            self.traded = True
            stop = self.range_low if p.stop_at == "opposite" else mid
            return SignalDraft(Side.LONG, close, stop, rr_target(Side.LONG, close, stop, p.reward_to_risk), context)
        if close < self.range_low - buffer:
            self.traded = True
            stop = self.range_high if p.stop_at == "opposite" else mid
            return SignalDraft(Side.SHORT, close, stop, rr_target(Side.SHORT, close, stop, p.reward_to_risk), context)
        return None


XAU_STRATEGIES = {
    "goldpilot": (GoldPilotV1, GoldPilotParams),
    "orderblock": (OrderBlockRetest, OrderBlockParams),
    "sweep": (LiquiditySweepMSS, SweepParams),
    "orb": (OpeningRangeBreakout, OpeningRangeParams),
}

