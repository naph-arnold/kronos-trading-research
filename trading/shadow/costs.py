"""Shadow-trading cost model: spread (from live ticks), slippage, commission and overnight swap.

* Spread is not modelled: entries and exits use the live bid/ask of the tick at the simulated fill time.
* Slippage: a fixed adverse allowance per side (price units), applied to market entries, stops and
  time exits. Take-profits fill at the limit price (no positive slippage).
* Commission: USD per 1.00 lot per ROUND TRIP (open + close), charged when the position closes.
  The broker's commission is UNVERIFIED (MT5 does not expose it); scenarios are assumptions.
* Swap: broker `symbol_info` swap in points per lot per night; one rollover per server midnight crossed,
  the configured server weekday's rollover counts three times (weekend financing).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from trading.clock import ServerTimeZone
from trading.common import Side, ValidationError

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True)
class SwapSpec:
    long_points_per_lot_night: Decimal
    short_points_per_lot_night: Decimal
    usd_per_point_per_lot: Decimal
    triple_weekday_server: str
    server_tz: ServerTimeZone
    source: str
    verified: bool

    def validate(self) -> None:
        if self.triple_weekday_server not in WEEKDAYS:
            raise ValidationError("Unknown triple-swap weekday")
        for name in ("long_points_per_lot_night", "short_points_per_lot_night", "usd_per_point_per_lot"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValidationError(f"{name} must be a finite Decimal")

    def rollovers(self, opened: datetime, until: datetime) -> int:
        """Server midnights crossed in (opened, until]; the triple weekday's rollover counts 3."""
        start, end = self.server_tz.to_server(opened).date(), self.server_tz.to_server(until).date()
        nights, day = 0, start
        while day < end:
            nights += 3 if WEEKDAYS[day.weekday()] == self.triple_weekday_server else 1
            day += timedelta(days=1)
        return nights

    def charge(self, side: Side, size: Decimal, opened: datetime, until: datetime) -> tuple[Decimal, int]:
        """Signed account-currency swap (negative = cost) and the rollover count."""
        nights = self.rollovers(opened, until)
        points = self.long_points_per_lot_night if side is Side.LONG else self.short_points_per_lot_night
        return size * points * self.usd_per_point_per_lot * nights, nights
