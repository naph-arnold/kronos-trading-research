"""Strategy signal schema shared by both pipelines.

A signal is a *candidate* only. It carries provenance but never authorizes an
order; the deterministic risk engine decides.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from trading.common import (MARKET_SYMBOLS, Market, Side, ValidationError, check_identifier,
                            require_utc, to_decimal)

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TradeSignal:
    signal_id: str
    market: Market
    symbol: str
    side: Side
    entry_price: Decimal
    stop_price: Decimal
    strategy_id: str
    strategy_version: str
    generated_at: datetime
    bar_close_time: datetime
    take_profit: Decimal | None = None
    provenance: dict = field(default_factory=dict, hash=False, compare=False)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def create(cls, **kwargs) -> "TradeSignal":
        for name in ("entry_price", "stop_price"):
            kwargs[name] = to_decimal(kwargs.get(name), name, positive=True)
        if kwargs.get("take_profit") is not None:
            kwargs["take_profit"] = to_decimal(kwargs["take_profit"], "take_profit", positive=True)
        kwargs["market"] = Market(kwargs["market"])
        kwargs["side"] = Side(kwargs["side"])
        signal = cls(**kwargs)
        signal.validate()
        return signal

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValidationError("Unsupported signal schema version")
        check_identifier(self.signal_id, "signal_id")
        check_identifier(self.strategy_id, "strategy_id")
        check_identifier(self.strategy_version, "strategy_version")
        if not isinstance(self.market, Market) or not isinstance(self.side, Side):
            raise ValidationError("Unknown market or side")
        if self.symbol != MARKET_SYMBOLS[self.market]:
            raise ValidationError(f"Symbol {self.symbol!r} not allowed for {self.market.value}")
        for name in ("entry_price", "stop_price"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValidationError(f"{name} must be a finite positive Decimal")
        if self.side is Side.LONG and not self.stop_price < self.entry_price:
            raise ValidationError("Long signal requires stop below entry")
        if self.side is Side.SHORT and not self.stop_price > self.entry_price:
            raise ValidationError("Short signal requires stop above entry")
        if self.take_profit is not None:
            if not isinstance(self.take_profit, Decimal) or not self.take_profit.is_finite():
                raise ValidationError("take_profit must be a finite Decimal")
            if (self.side is Side.LONG and self.take_profit <= self.entry_price) or (
                    self.side is Side.SHORT and self.take_profit >= self.entry_price):
                raise ValidationError("take_profit on wrong side of entry")
        require_utc(self.generated_at, "generated_at")
        require_utc(self.bar_close_time, "bar_close_time")
        if self.generated_at < self.bar_close_time:
            raise ValidationError("Signal generated before its source bar closed (lookahead)")
        if not isinstance(self.provenance, dict):
            raise ValidationError("provenance must be a dict")

    @property
    def dedup_key(self) -> str:
        """Same strategy/version/side on the same source bar is one signal, whatever its ID."""
        material = "|".join((self.market.value, self.symbol, self.strategy_id, self.strategy_version,
                             self.side.value, self.bar_close_time.isoformat()))
        return hashlib.sha256(material.encode()).hexdigest()

    @property
    def idempotency_key(self) -> str:
        """Stable client order ID material for execution adapters (Phase 3)."""
        return hashlib.sha256(f"{self.market.value}|{self.signal_id}|{self.dedup_key}".encode()).hexdigest()[:32]
