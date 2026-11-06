"""Instrument contract specifications and their price/cost arithmetic.

All money values are in the *account currency*. Specs must be loaded from the
broker/exchange (MT5 symbol_info + order_calc_margin; Binance exchangeInfo +
account commission) — never guessed. Missing values fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading.common import Market, ValidationError, is_multiple


def _require_decimal(spec: object, names: tuple[str, ...], *, positive: bool) -> None:
    for name in names:
        value = getattr(spec, name)
        if not isinstance(value, Decimal) or not value.is_finite() or (value <= 0 if positive else value < 0):
            raise ValidationError(f"Instrument {name} must be a finite {'positive' if positive else 'non-negative'} "
                                  "Decimal")


@dataclass(frozen=True)
class Mt5InstrumentSpec:
    """XAUUSD CFD spec. `tick_value_loss` = SYMBOL_TRADE_TICK_VALUE_LOSS for 1 lot."""

    symbol: str
    broker_symbol: str
    account_currency: str
    contract_size: Decimal
    point: Decimal
    tick_size: Decimal
    tick_value_loss: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    stops_level_points: int
    margin_per_lot: Decimal
    commission_per_lot_round_trip: Decimal = Decimal("0")

    market = Market.XAUUSD_MT5

    def validate(self) -> None:
        if self.symbol != "XAUUSD" or not self.broker_symbol or not self.account_currency:
            raise ValidationError("MT5 spec must be for XAUUSD with broker symbol and account currency")
        _require_decimal(self, ("contract_size", "point", "tick_size", "tick_value_loss", "volume_min",
                                "volume_max", "volume_step", "margin_per_lot"), positive=True)
        _require_decimal(self, ("commission_per_lot_round_trip",), positive=False)
        if type(self.stops_level_points) is not int or self.stops_level_points < 0:
            raise ValidationError("stops_level_points must be a non-negative int")
        if not is_multiple(self.volume_min, self.volume_step) or self.volume_max < self.volume_min:
            raise ValidationError("Inconsistent MT5 volume limits")

    @property
    def size_step(self) -> Decimal:
        return self.volume_step

    @property
    def min_size(self) -> Decimal:
        return self.volume_min

    @property
    def max_size(self) -> Decimal:
        return self.volume_max

    @property
    def price_tick(self) -> Decimal:
        return self.tick_size

    @property
    def min_stop_distance(self) -> Decimal:
        return self.stops_level_points * self.point

    def value_per_price_unit(self, size: Decimal) -> Decimal:
        """Account-currency P&L per 1.0 move in price for `size` lots."""
        return size * self.tick_value_loss / self.tick_size

    def round_trip_cost(self, size: Decimal, entry: Decimal, exit_price: Decimal) -> Decimal:
        return size * self.commission_per_lot_round_trip


@dataclass(frozen=True)
class BinanceSpotSpec:
    """BTCUSDT spot filters (PRICE_FILTER, LOT_SIZE, NOTIONAL/MIN_NOTIONAL) and taker fee."""

    symbol: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    taker_fee_rate: Decimal

    market = Market.BTCUSDT_BINANCE_SPOT

    @property
    def account_currency(self) -> str:
        return self.quote_asset

    def validate(self) -> None:
        if (self.symbol, self.base_asset, self.quote_asset) != ("BTCUSDT", "BTC", "USDT"):
            raise ValidationError("Binance spec must be BTCUSDT (BTC/USDT)")
        _require_decimal(self, ("tick_size", "step_size", "min_qty", "max_qty", "min_notional"), positive=True)
        _require_decimal(self, ("taker_fee_rate",), positive=False)
        if self.taker_fee_rate >= Decimal("0.01"):
            raise ValidationError("Implausible taker fee rate")
        if self.max_qty < self.min_qty:
            raise ValidationError("Inconsistent LOT_SIZE filter")

    @property
    def size_step(self) -> Decimal:
        return self.step_size

    @property
    def min_size(self) -> Decimal:
        return self.min_qty

    @property
    def max_size(self) -> Decimal:
        return self.max_qty

    @property
    def price_tick(self) -> Decimal:
        return self.tick_size

    @property
    def min_stop_distance(self) -> Decimal:
        return self.tick_size

    def value_per_price_unit(self, size: Decimal) -> Decimal:
        return size

    def round_trip_cost(self, size: Decimal, entry: Decimal, exit_price: Decimal) -> Decimal:
        return size * (entry + exit_price) * self.taker_fee_rate


InstrumentSpec = Mt5InstrumentSpec | BinanceSpotSpec
