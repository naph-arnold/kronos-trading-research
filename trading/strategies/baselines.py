"""Non-AI baselines for XAUUSD robustness research (Phase 2C). Not trading candidates.

* `RandomEntryBaseline`: enters at random bars (seeded, reproducible) with a random side,
  using the same ATR stop / reward-to-risk exit structure as the strategies it is compared
  with. If a strategy does not beat random entries with identical exits and costs, its
  entry logic adds no demonstrated value.
* `H1TrendBaseline`: the simplest trend rule — H1 EMA fast/slow crossover, ATR stop, R target.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from trading.common import Market, Side
from trading.indicators import ATR, EMA, Resampler
from trading.marketdata import Bar
from trading.strategies.base import SignalDraft, StrategyBase, rr_target


@dataclass(frozen=True)
class RandomEntryParams:
    seed: int = 1
    entry_probability: float = 0.01  # per closed M15 bar
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    reward_to_risk: float = 2.0
    long_probability: float = 0.5


class RandomEntryBaseline(StrategyBase):
    strategy_id = "xau-baseline-random"
    version = "1.0.0"
    market = Market.XAUUSD_MT5

    def __init__(self, params: RandomEntryParams = RandomEntryParams(), **kwargs):
        super().__init__(params, **kwargs)
        self.rng = random.Random(params.seed)
        self.atr = ATR(params.atr_period)

    def on_bar(self, bar: Bar) -> SignalDraft | None:
        p = self.params
        atr = self.atr.update(float(bar.high), float(bar.low), float(bar.close))
        draw, side_draw = self.rng.random(), self.rng.random()  # always consume: reproducible stream
        if atr is None or draw >= p.entry_probability:
            return None
        side = Side.LONG if side_draw < p.long_probability else Side.SHORT
        close = float(bar.close)
        stop = close - p.atr_stop_mult * atr if side is Side.LONG else close + p.atr_stop_mult * atr
        return SignalDraft(side, close, stop, rr_target(side, close, stop, p.reward_to_risk), {"atr": atr})


@dataclass(frozen=True)
class H1TrendBaselineParams:
    ema_fast: int = 50
    ema_slow: int = 200
    atr_period: int = 14  # on M15
    atr_stop_mult: float = 3.0
    reward_to_risk: float = 2.0


class H1TrendBaseline(StrategyBase):
    """Enter in the direction of an H1 EMA fast/slow crossover at the close of the M15 bar that
    completes the crossing hour. Stop = atr_stop_mult x ATR(M15); target = R multiple."""

    strategy_id = "xau-baseline-h1-ema-cross"
    version = "1.0.0"
    market = Market.XAUUSD_MT5

    def __init__(self, params: H1TrendBaselineParams = H1TrendBaselineParams(), **kwargs):
        super().__init__(params, **kwargs)
        self.resampler = Resampler("1h")
        self.fast, self.slow = EMA(params.ema_fast), EMA(params.ema_slow)
        self.atr = ATR(params.atr_period)
        self.prev_diff: float | None = None

    def on_bar(self, bar: Bar) -> SignalDraft | None:
        p = self.params
        atr = self.atr.update(float(bar.high), float(bar.low), float(bar.close))
        draft = None
        for h1 in self.resampler.update(bar):
            f, s = self.fast.update(float(h1.close)), self.slow.update(float(h1.close))
            if f is None or s is None:
                continue
            diff, prev = f - s, self.prev_diff
            self.prev_diff = diff
            if prev is None or atr is None:
                continue
            close = float(bar.close)
            if prev <= 0 < diff:
                stop = close - p.atr_stop_mult * atr
                draft = SignalDraft(Side.LONG, close, stop, rr_target(Side.LONG, close, stop, p.reward_to_risk),
                                    {"ema_fast": f, "ema_slow": s, "atr": atr})
            elif prev >= 0 > diff:
                stop = close + p.atr_stop_mult * atr
                draft = SignalDraft(Side.SHORT, close, stop, rr_target(Side.SHORT, close, stop, p.reward_to_risk),
                                    {"ema_fast": f, "ema_slow": s, "atr": atr})
        return draft
