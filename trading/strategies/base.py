"""Shared strategy plumbing: parameter hashing and standardized signal construction."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal

from trading.common import MARKET_SYMBOLS, Market, Side
from trading.indicators import round_price
from trading.marketdata import Bar
from trading.signals import TradeSignal


def params_hash(params: object) -> str:
    payload = json.dumps(asdict(params) if hasattr(params, "__dataclass_fields__") else params,
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class SignalDraft:
    side: Side
    entry: float
    stop: float
    take_profit: float | None
    context: dict


class StrategyBase:
    strategy_id = "base"
    version = "0.0.0"
    market: Market = Market.XAUUSD_MT5

    def __init__(self, params, *, tick_size: Decimal, dataset_id: str = "unspecified"):
        self.params = params
        self.tick = tick_size
        self.dataset_id = dataset_id
        self.signals_emitted = 0

    def __call__(self, history) -> TradeSignal | None:
        bar = history[-1]
        draft = self.on_bar(bar)
        if draft is None:
            return None
        return self._to_signal(bar, draft)

    def on_bar(self, bar: Bar) -> SignalDraft | None:  # pragma: no cover - interface
        raise NotImplementedError

    def _to_signal(self, bar: Bar, draft: SignalDraft) -> TradeSignal | None:
        long = draft.side is Side.LONG
        entry = round_price(draft.entry, self.tick, up=not long)
        # Stops snap *away* from entry (never tighter than designed); targets snap *toward* entry.
        stop = round_price(draft.stop, self.tick, up=not long)
        target = None if draft.take_profit is None else round_price(draft.take_profit, self.tick, up=not long)
        if (long and not stop < entry) or (not long and not stop > entry):
            return None
        if target is not None and ((long and target <= entry) or (not long and target >= entry)):
            target = None
        self.signals_emitted += 1
        return TradeSignal.create(
            signal_id=f"{self.strategy_id}-{bar.close_time:%Y%m%dT%H%M}-{draft.side.value}",
            market=self.market, symbol=MARKET_SYMBOLS[self.market], side=draft.side,
            entry_price=entry, stop_price=stop, take_profit=target,
            strategy_id=self.strategy_id, strategy_version=self.version,
            generated_at=bar.close_time, bar_close_time=bar.close_time,
            provenance={"dataset_id": self.dataset_id, "interval": bar.interval,
                        "source_bar_open": bar.open_time.isoformat(), "params_hash": params_hash(self.params),
                        "params": asdict(self.params), "context": draft.context},
        )


def rr_target(side: Side, entry: float, stop: float, reward_to_risk: float | None) -> float | None:
    if reward_to_risk is None:
        return None
    risk = abs(entry - stop)
    return entry + reward_to_risk * risk if side is Side.LONG else entry - reward_to_risk * risk
