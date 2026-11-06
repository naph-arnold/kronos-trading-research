"""Deterministic, fail-closed pre-trade risk engine (per account, per market).

Sizing rule: risk_budget = min(equity * risk_fraction, equity * aggregate_cap - open_stop_risk)
with risk_fraction <= 0.5% (hard ceiling). Loss per unit uses the executable
side of the quote, adverse slippage on both entry and stop, and round-trip
costs. Size is rounded *down* to the venue step; if the venue minimum would
exceed the budget the trade is rejected, never rounded up.

Planned risk is not a loss guarantee: gaps, partial fills, slippage beyond the
allowance and stop failures can produce larger realized losses.

AI/agent/TradingView opinions are not inputs to this module.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from trading.breaker import CircuitBreaker
from trading.common import Market, Side, ValidationError, floor_to_step, is_multiple, require_utc
from trading.config import DemoEnvironment, ExecutionMode
from trading.instruments import BinanceSpotSpec, InstrumentSpec, Mt5InstrumentSpec
from trading.marketdata import Quote
from trading.sessions import TradingCalendar
from trading.signals import TradeSignal
from trading.state import AccountSnapshot

HARD_MAX_RISK_FRACTION = Decimal("0.005")
MAX_CLOCK_SKEW = timedelta(seconds=2)


@dataclass(frozen=True)
class RiskLimits:
    market: Market
    max_spread: Decimal  # price units; required, no default
    slippage_allowance: Decimal  # price units applied adversely to entry and stop
    risk_fraction: Decimal = HARD_MAX_RISK_FRACTION
    daily_loss_halt_fraction: Decimal = Decimal("0.015")  # proposed guardrail, to validate
    aggregate_risk_fraction: Decimal = Decimal("0.01")  # proposed guardrail, to validate
    max_open_positions: int = 1
    max_signal_age: timedelta = timedelta(minutes=5)
    max_quote_age: timedelta = timedelta(seconds=10)
    max_account_age: timedelta = timedelta(seconds=30)
    max_entry_deviation_fraction: Decimal = Decimal("0.002")
    max_margin_utilization: Decimal = Decimal("0.5")

    def validate(self) -> None:
        if not isinstance(self.market, Market):
            raise ValidationError("Unknown market in limits")
        decimals = ("max_spread", "slippage_allowance", "risk_fraction", "daily_loss_halt_fraction",
                    "aggregate_risk_fraction", "max_entry_deviation_fraction", "max_margin_utilization")
        for name in decimals:
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValidationError(f"Limit {name} must be a finite non-negative Decimal")
        if not 0 < self.risk_fraction <= HARD_MAX_RISK_FRACTION:
            raise ValidationError(f"risk_fraction must be in (0, {HARD_MAX_RISK_FRACTION}]")
        if not 0 < self.daily_loss_halt_fraction < 1 or not 0 < self.aggregate_risk_fraction < 1:
            raise ValidationError("Daily halt and aggregate cap must be in (0, 1)")
        if self.max_spread <= 0 or not 0 < self.max_margin_utilization <= 1:
            raise ValidationError("max_spread must be positive and margin utilization in (0, 1]")
        if type(self.max_open_positions) is not int or self.max_open_positions < 1:
            raise ValidationError("max_open_positions must be a positive int")
        for name in ("max_signal_age", "max_quote_age", "max_account_age"):
            value = getattr(self, name)
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ValidationError(f"{name} must be a positive timedelta")


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    market: str
    signal_id: str
    dedup_key: str
    size: Decimal
    reasons: tuple[str, ...]
    evaluated_at: str
    risk_budget: Decimal | None = None
    planned_risk: Decimal | None = None
    loss_per_unit: Decimal | None = None
    effective_entry: Decimal | None = None
    live_trading_enabled: bool = False

    def rejected(self, reason: str) -> "RiskDecision":
        return replace(self, approved=False, size=Decimal("0"), reasons=self.reasons + (reason,))

    def to_record(self) -> dict:
        return {key: (str(value) if isinstance(value, Decimal) else value) for key, value in asdict(self).items()}


def _age_reason(label: str, moment: datetime, now: datetime, max_age: timedelta) -> str | None:
    if moment - now > MAX_CLOCK_SKEW:
        return f"{label} is future-dated"
    if now - moment > max_age:
        return f"{label} is stale ({now - moment} > {max_age})"
    return None


def evaluate(
    signal: TradeSignal,
    *,
    account: AccountSnapshot,
    instrument: InstrumentSpec,
    quote: Quote,
    limits: RiskLimits,
    environment: DemoEnvironment,
    breaker: CircuitBreaker,
    now: datetime,
    duplicate_check=None,
    session_calendar: TradingCalendar | None = None,
) -> RiskDecision:
    """Return an approved size or a rejection with every reason found. Never raises."""
    base = RiskDecision(
        approved=False, market=str(getattr(getattr(signal, "market", None), "value", "unknown")),
        signal_id=str(getattr(signal, "signal_id", "unknown")), dedup_key="", size=Decimal("0"),
        reasons=(), evaluated_at=str(now.isoformat() if isinstance(now, datetime) else now),
    )
    try:
        return _evaluate(base, signal, account, instrument, quote, limits, environment, breaker, now,
                         duplicate_check, session_calendar)
    except Exception as exc:  # fail closed on anything unexpected
        return base.rejected(f"Risk evaluation error: {type(exc).__name__}: {exc}")


def _evaluate(base, signal, account, instrument, quote, limits, environment, breaker, now, duplicate_check,
              session_calendar):
    reasons: list[str] = []

    def reject() -> RiskDecision:
        return replace(base, approved=False, size=Decimal("0"), reasons=tuple(reasons))

    # 1. Environment: demo only.
    if (not isinstance(environment, DemoEnvironment) or environment.mode is not ExecutionMode.DEMO
            or environment.live_trading_enabled):
        reasons.append("Execution environment is not a verified demo environment")
        return reject()
    require_utc(now, "now")

    # 2. Structural validation; cannot size on malformed inputs.
    for label, obj in (("signal", signal), ("account", account), ("instrument", instrument),
                       ("quote", quote), ("limits", limits)):
        try:
            obj.validate()
        except (ValidationError, AttributeError, TypeError) as exc:
            reasons.append(f"Invalid {label}: {exc}")
    if reasons:
        return reject()
    base = replace(base, dedup_key=signal.dedup_key)

    # 3. Consistency between signal, account, instrument and limits.
    markets = {signal.market, account.market, instrument.market, limits.market}
    if len(markets) != 1:
        reasons.append("Signal, account, instrument and limits belong to different markets")
    if breaker.market is not signal.market:
        reasons.append("Circuit breaker belongs to another market")
    if quote.symbol != signal.symbol or instrument.symbol != signal.symbol:
        reasons.append("Quote/instrument symbol does not match signal")
    if instrument.account_currency != account.currency:
        reasons.append("Instrument values are not in the account currency")
    if reasons:
        return reject()

    # 4. Account, breaker and duplicate checks.
    if not account.is_demo:
        reasons.append("Account is not a demo account")
    if not account.reconciled:
        reasons.append("Account/order state is not reconciled (unknown broker state)")
    age = _age_reason("Account snapshot", account.snapshot_at, now, limits.max_account_age)
    if age:
        reasons.append(age)
    reasons.extend(f"Circuit breaker: {reason}" for reason in breaker.reasons())
    if duplicate_check is not None and duplicate_check(signal):
        reasons.append("Duplicate signal (already claimed)")
    if account.pending_order_ids:
        reasons.append("Pending orders exist; refusing to stack orders")
    if len(account.open_positions) >= limits.max_open_positions:
        reasons.append("Maximum concurrent positions reached")
    open_risk = account.open_stop_risk()
    if open_risk is None:
        reasons.append("Open position without a known stop/risk")
    if account.daily_loss_halted:
        reasons.append("Daily loss halt: latched for the rest of the risk day")
    elif account.daily_loss_fraction() >= limits.daily_loss_halt_fraction:
        reasons.append(f"Daily loss halt: equity down {account.daily_loss_fraction():.4%} since day start")

    # Trading session (XAUUSD requires a calendar; absence fails closed).
    if session_calendar is None:
        if signal.market is Market.XAUUSD_MT5:
            reasons.append("Trading session calendar required for XAUUSD")
    elif not isinstance(session_calendar, TradingCalendar) or session_calendar.symbol != signal.symbol:
        reasons.append("Trading session calendar does not match symbol")
    else:
        closed = session_calendar.tradeable_reason(now)
        if closed:
            reasons.append(f"Market session: {closed}")

    # 5. Signal/quote freshness and price sanity.
    age = _age_reason("Signal", signal.generated_at, now, limits.max_signal_age)
    if age:
        reasons.append(age)
    age = _age_reason("Quote", quote.time, now, limits.max_quote_age)
    if age:
        reasons.append(age)
    if quote.spread > limits.max_spread:
        reasons.append(f"Spread {quote.spread} exceeds max {limits.max_spread}")
    if isinstance(instrument, BinanceSpotSpec) and signal.side is not Side.LONG:
        reasons.append("Binance spot is long/flat only")
    for name in ("entry_price", "stop_price", "take_profit"):
        value = getattr(signal, name)
        if value is not None and not is_multiple(value, instrument.price_tick):
            reasons.append(f"{name} {value} not aligned to tick size {instrument.price_tick}")

    long = signal.side is Side.LONG
    entry = quote.ask if long else quote.bid
    deviation = abs(entry - signal.entry_price) / signal.entry_price
    if deviation > limits.max_entry_deviation_fraction:
        reasons.append(f"Market moved {deviation:.4%} from signal entry")
    # Distance from the side of the book that triggers the stop (MT5 stops level rule).
    trigger_distance = (quote.bid - signal.stop_price) if long else (signal.stop_price - quote.ask)
    if trigger_distance <= 0:
        reasons.append("Market already through stop")
    elif trigger_distance < instrument.min_stop_distance:
        reasons.append(f"Stop distance {trigger_distance} below venue minimum {instrument.min_stop_distance}")
    if reasons:
        return reject()

    # 6. Budget and size (round down only).
    equity = account.equity
    per_trade = equity * limits.risk_fraction
    aggregate_room = equity * limits.aggregate_risk_fraction - open_risk
    budget = min(per_trade, aggregate_room)
    base = replace(base, risk_budget=budget, effective_entry=entry)
    if budget <= 0:
        reasons.append("Aggregate open stop-risk cap reached")
        return reject()
    slip = limits.slippage_allowance
    adverse_entry = entry + slip if long else entry - slip
    adverse_stop = signal.stop_price - slip if long else signal.stop_price + slip
    if adverse_stop <= 0:
        reasons.append("Stop minus slippage allowance is not a valid price")
        return reject()
    one = Decimal("1")
    loss_per_unit = (instrument.value_per_price_unit(one) * abs(adverse_entry - adverse_stop)
                     + instrument.round_trip_cost(one, adverse_entry, adverse_stop))
    if loss_per_unit <= 0:
        reasons.append("Non-positive loss per unit; cannot size safely")
        return reject()
    base = replace(base, loss_per_unit=loss_per_unit)
    size = floor_to_step(min(budget / loss_per_unit, instrument.max_size), instrument.size_step)
    if size < instrument.min_size:
        reasons.append(f"Minimum size {instrument.min_size} would risk {instrument.min_size * loss_per_unit} "
                       f"> budget {budget}")
        return reject()
    planned = size * loss_per_unit
    if planned > budget:  # invariant; defence in depth
        reasons.append("Planned risk exceeds budget after rounding")
        return reject()

    # 7. Venue funding/margin constraints.
    if isinstance(instrument, BinanceSpotSpec):
        if size * entry < instrument.min_notional:
            reasons.append(f"Notional {size * entry} below minimum {instrument.min_notional}")
        cost = size * adverse_entry * (one + instrument.taker_fee_rate)
        if cost > account.available_funds:
            reasons.append(f"Insufficient free {instrument.quote_asset}: need {cost}")
    elif isinstance(instrument, Mt5InstrumentSpec):
        margin = size * instrument.margin_per_lot
        if margin > account.available_funds * limits.max_margin_utilization:
            reasons.append(f"Margin {margin} exceeds {limits.max_margin_utilization} of free margin")
    else:
        reasons.append("Unsupported instrument type")
    if reasons:
        return reject()
    return replace(base, approved=True, size=size, planned_risk=planned, reasons=())
