"""Per-market disruption detection (Section 9.4/9.5). Deterministic; independent of AI.

`MarketHealthMonitor` is fed by the read-only connectors (quotes, venue symbol status)
and by operator/venue notices (maintenance windows, session changes). Its `guard`
returns a rejection reason whenever essential information is unavailable, stale or
abnormal, so the risk engine rejects new entries (open positions keep their stops).

One monitor per market: a disruption in one pipeline never blocks the other, and
BTC/USDT is never blocked merely because traditional markets are closed.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Callable

from trading.common import Market, require_utc
from trading.marketdata import Quote

# Normalized venue symbol status. MT5 symbol trade_mode: 4=full, 3=close_only, 0=disabled ...;
# Binance exchangeInfo status: TRADING, BREAK, HALT, ...
MT5_TRADE_MODE = {0: "disabled", 1: "long_only", 2: "short_only", 3: "close_only", 4: "trading"}
BINANCE_STATUS = {"TRADING": "trading", "BREAK": "suspended", "HALT": "suspended", "AUCTION_MATCH": "suspended",
                  "PRE_TRADING": "suspended", "POST_TRADING": "suspended", "END_OF_DAY": "suspended"}


@dataclass(frozen=True)
class VenueWindow:
    """Exchange maintenance / broker restriction window from a venue or operator notice."""

    start: datetime
    end: datetime
    kind: str  # "maintenance", "suspension", "session_change"
    source: str
    reference: str
    verified: bool = True  # unverified session changes block trading until confirmed


@dataclass
class MarketHealthMonitor:
    market: Market
    max_quote_age: timedelta = timedelta(seconds=15)
    max_status_age: timedelta = timedelta(minutes=10)
    abnormal_spread_multiple: Decimal = Decimal("4")
    min_spread_samples: int = 30
    spread_window: int = 500
    max_quote_gap: timedelta = timedelta(seconds=60)
    _spreads: deque = field(default_factory=lambda: deque(maxlen=500), repr=False)
    _last_quote: Quote | None = None
    _last_gap: timedelta | None = None
    _status: tuple[str, datetime, str] | None = None  # (normalized status, checked_at, source)
    _windows: list[VenueWindow] = field(default_factory=list)
    _flags: list[tuple[datetime, str]] = field(default_factory=list)

    # -- inputs ------------------------------------------------------------------------------
    def record_quote(self, quote: Quote) -> None:
        quote.validate()
        if self._last_quote is not None:
            if quote.time < self._last_quote.time:
                self._flag(quote.time, "out-of-order quote")
                return
            self._last_gap = quote.time - self._last_quote.time
            if self._last_gap > self.max_quote_gap:
                self._flag(quote.time, f"missing price updates for {self._last_gap}")
        self._last_quote = quote
        self._spreads.append(quote.spread)

    def record_status(self, status: str, at: datetime, source: str) -> None:
        require_utc(at, "at")
        self._status = (status, at, source)
        if status != "trading":
            self._flag(at, f"venue status {status} ({source})")

    def record_mt5_trade_mode(self, trade_mode: int, at: datetime) -> None:
        self.record_status(MT5_TRADE_MODE.get(trade_mode, "unknown"), at, "mt5.symbol_info.trade_mode")

    def record_binance_status(self, status: str, at: datetime) -> None:
        self.record_status(BINANCE_STATUS.get(status, "unknown"), at, "binance.exchangeInfo.status")

    def add_window(self, window: VenueWindow) -> None:
        require_utc(window.start, "start")
        require_utc(window.end, "end")
        if window.end <= window.start or not window.source.strip() or not window.reference.strip():
            raise ValueError("Venue window needs a valid interval, source and reference")
        self._windows.append(window)

    def _flag(self, at: datetime, message: str) -> None:
        self._flags.append((at, message))
        del self._flags[:-200]

    # -- outputs -----------------------------------------------------------------------------
    def flags(self) -> list[tuple[str, str]]:
        return [(at.isoformat(), message) for at, message in self._flags]

    def guard(self, now: datetime) -> str | None:
        require_utc(now, "now")
        for window in self._windows:
            if window.start <= now < window.end:
                return f"{window.kind} window ({window.source}: {window.reference})"
            if not window.verified and window.start - timedelta(days=7) <= now < window.end:
                return f"unverified {window.kind} notice ({window.source}); confirm before trading"
        if self._status is None:
            return "venue trading status unknown (fail closed)"
        status, checked_at, source = self._status
        if now - checked_at > self.max_status_age:
            return f"venue trading status stale ({now - checked_at})"
        if status != "trading":
            return f"venue status {status} ({source})"
        if self._last_quote is None:
            return "no market data received (fail closed)"
        age = now - self._last_quote.time
        if age > self.max_quote_age:
            return f"market data stale ({age})"
        if self._last_gap is not None and self._last_gap > self.max_quote_gap:
            return f"recent gap in price updates ({self._last_gap})"
        if len(self._spreads) >= self.min_spread_samples:
            typical = median(list(self._spreads)[:-1] or [self._spreads[-1]])
            current = self._spreads[-1]
            if typical > 0 and current > typical * self.abnormal_spread_multiple:
                return f"abnormal spread {current} vs median {typical}"
        return None


def combine_guards(*guards: Callable[[datetime], "str | None"]) -> Callable[[datetime], "str | None"]:
    """Combine deterministic guards; the first rejection reason wins. Guard errors propagate
    to the risk engine, which converts them into rejections (fail closed)."""
    active = [g for g in guards if g is not None]

    def combined(now: datetime) -> str | None:
        for guard in active:
            reason = guard(now)
            if reason:
                return reason
        return None

    return combined
