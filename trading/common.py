"""Shared primitives: markets, sides, strict Decimal parsing and UTC handling."""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from enum import Enum


class ValidationError(ValueError):
    """Input is malformed or unsafe; callers must fail closed."""


class Market(str, Enum):
    """Independent pipelines. Each has its own account, state and risk limits."""

    XAUUSD_MT5 = "xauusd_mt5"
    BTCUSDT_BINANCE_SPOT = "btcusdt_binance_spot"


MARKET_SYMBOLS = {
    Market.XAUUSD_MT5: "XAUUSD",
    Market.BTCUSDT_BINANCE_SPOT: "BTCUSDT",
}


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")


def check_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.match(value):
        raise ValidationError(f"Invalid {name}: must be 1-128 chars of [A-Za-z0-9_.:-]")
    return value


def to_decimal(value: object, name: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    """Parse a finite Decimal. Floats go through repr() to avoid binary artefacts."""
    if value is None or isinstance(value, bool):
        raise ValidationError(f"Missing or invalid {name}")
    try:
        if isinstance(value, Decimal):
            result = value
        elif isinstance(value, int):
            result = Decimal(value)
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValidationError(f"Non-finite {name}")
            result = Decimal(repr(value))
        elif isinstance(value, str):
            result = Decimal(value.strip())
        else:
            raise ValidationError(f"Unsupported type for {name}: {type(value).__name__}")
    except InvalidOperation as exc:
        raise ValidationError(f"Invalid {name}: {value!r}") from exc
    if not result.is_finite():
        raise ValidationError(f"Non-finite {name}")
    if positive and result <= 0:
        raise ValidationError(f"{name} must be positive")
    if nonnegative and result < 0:
        raise ValidationError(f"{name} must be non-negative")
    return result


def require_utc(moment: object, name: str) -> datetime:
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValidationError(f"{name} must be a timezone-aware datetime")
    if moment.utcoffset() != timedelta(0):
        raise ValidationError(f"{name} must be in UTC")
    return moment


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round *down* to a multiple of step (never rounds size upward)."""
    if step <= 0:
        raise ValidationError("Step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def is_multiple(value: Decimal, step: Decimal) -> bool:
    return step > 0 and value % step == 0
