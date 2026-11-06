"""Per-account state snapshots. Each market has its own, never shared."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from trading.common import Market, Side, ValidationError, check_identifier, require_utc


@dataclass(frozen=True)
class OpenPosition:
    position_id: str
    symbol: str
    side: Side
    size: Decimal
    entry_price: Decimal
    stop_price: Decimal | None
    # Remaining account-currency loss if the stop fills (incl. costs). None = unknown.
    risk_to_stop: Decimal | None


@dataclass(frozen=True)
class AccountSnapshot:
    """Reconciled view of one demo account.

    `available_funds` is MT5 free margin, or the free (unlocked) quote balance on
    Binance spot. `day_start_equity` is equity at the start of the risk day.
    `reconciled` must only be True after orders/positions/balances matched the
    broker/exchange; adapters set it, the risk engine trusts nothing else.
    """

    market: Market
    account_id: str
    is_demo: bool
    currency: str
    equity: Decimal
    balance: Decimal
    available_funds: Decimal
    day_start_equity: Decimal
    snapshot_at: datetime
    reconciled: bool
    open_positions: tuple[OpenPosition, ...] = ()
    pending_order_ids: tuple[str, ...] = ()
    # Latched by trading.daily.DailyLossLedger for the rest of the risk day.
    daily_loss_halted: bool = False
    notes: dict = field(default_factory=dict, hash=False, compare=False)

    def validate(self) -> None:
        if not isinstance(self.market, Market):
            raise ValidationError("Unknown account market")
        check_identifier(self.account_id, "account_id")
        if not isinstance(self.currency, str) or not self.currency:
            raise ValidationError("Account currency required")
        for name in ("equity", "balance", "available_funds", "day_start_equity"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValidationError(f"{name} must be a finite Decimal")
        if self.equity <= 0 or self.day_start_equity <= 0:
            raise ValidationError("Equity and day-start equity must be positive")
        if self.available_funds < 0:
            raise ValidationError("Available funds cannot be negative")
        require_utc(self.snapshot_at, "snapshot_at")
        if any(type(v) is not bool for v in (self.is_demo, self.reconciled, self.daily_loss_halted)):
            raise ValidationError("is_demo/reconciled/daily_loss_halted must be booleans")

    def open_stop_risk(self) -> Decimal | None:
        """Aggregate account-currency stop risk; None if any position's risk is unknown."""
        total = Decimal("0")
        for position in self.open_positions:
            if position.stop_price is None or position.risk_to_stop is None:
                return None
            if not position.risk_to_stop.is_finite() or position.risk_to_stop < 0:
                return None
            total += position.risk_to_stop
        return total

    def daily_loss_fraction(self) -> Decimal:
        return (self.day_start_equity - self.equity) / self.day_start_equity
