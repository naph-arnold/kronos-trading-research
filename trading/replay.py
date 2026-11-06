"""Deterministic offline replay: strategy -> risk engine -> simulated fills.

No-lookahead contract: at the close of bar i the strategy receives only
bars[0..i]; an emitted signal can execute no earlier than the open of bar i+1.
Every signal passes through the same deterministic risk engine as demo trading.

Fill model (deliberately pessimistic; research only, not a broker simulator):
* Bars are treated as mid prices; bid = mid - spread/2, ask = mid + spread/2.
* Entry at next bar open on the executable side plus slippage allowance.
* If stop and take-profit are both touched within one bar, the stop wins.
* A bar opening beyond the stop fills at that open (gap) minus slippage.
* Take-profit fills at the take-profit price (no positive slippage).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable, Sequence

from trading.breaker import CircuitBreaker
from trading.common import MARKET_SYMBOLS, Market, Side
from trading.config import DemoEnvironment
from trading.daily import DailyLossLedger, DayBoundary
from trading.instruments import BinanceSpotSpec, InstrumentSpec
from trading.ledger import SignalLedger
from trading.marketdata import Bar, Quote, validate_series
from trading.risk import RiskLimits, evaluate
from trading.sessions import TradingCalendar, validate_session_bars
from trading.signals import TradeSignal
from trading.state import AccountSnapshot, OpenPosition

Strategy = Callable[[tuple[Bar, ...]], "TradeSignal | None"]
TWO = Decimal("2")


@dataclass
class _Position:
    signal_id: str
    side: Side
    size: Decimal
    entry: Decimal
    stop: Decimal
    take_profit: Decimal | None
    planned_risk: Decimal
    opened_at: datetime


def _pnl(instrument: InstrumentSpec, pos: _Position, exit_price: Decimal) -> Decimal:
    direction = 1 if pos.side is Side.LONG else -1
    gross = instrument.value_per_price_unit(pos.size) * (exit_price - pos.entry) * direction
    return gross - instrument.round_trip_cost(pos.size, pos.entry, exit_price)


def _drawdown(curve: list[Decimal]) -> Decimal:
    peak, worst = curve[0], Decimal("0")
    for value in curve:
        peak = max(peak, value)
        worst = max(worst, (peak - value) / peak)
    return worst


def run_replay(
    bars: Sequence[Bar],
    strategy: Strategy,
    *,
    instrument: InstrumentSpec,
    limits: RiskLimits,
    environment: DemoEnvironment,
    initial_equity: Decimal,
    spread: Decimal,
    allow_gaps: bool = False,
    session_calendar: TradingCalendar | None = None,
    validate_sessions: bool = False,
) -> dict:
    market = limits.market
    if market is Market.XAUUSD_MT5:
        if session_calendar is None:
            raise ValueError("XAUUSD replay requires a trading session calendar")
        boundary = DayBoundary.server(session_calendar.server_tz)
    else:
        boundary = DayBoundary.utc()
    gaps = (validate_session_bars(bars, session_calendar) if validate_sessions
            else validate_series(bars, allow_gaps=allow_gaps))
    symbol = MARKET_SYMBOLS[market]
    if bars[0].symbol != symbol:
        raise ValueError("Bars do not belong to this market")
    half = spread / TWO
    slip = limits.slippage_allowance
    breaker = CircuitBreaker(market)
    ledger = SignalLedger()
    cash = initial_equity  # realized equity
    position: _Position | None = None
    pending: TradeSignal | None = None
    daily = DailyLossLedger(":memory:", market=market, boundary=boundary,
                            halt_fraction=limits.daily_loss_halt_fraction)
    curve = [initial_equity]
    trades, decisions = [], []

    def mark(price_mid: Decimal) -> Decimal:
        if position is None:
            return cash
        exit_side = price_mid - half if position.side is Side.LONG else price_mid + half
        return cash + _pnl(instrument, position, exit_side)

    def close(exit_price: Decimal, when: datetime, reason: str) -> None:
        nonlocal cash, position
        pnl = _pnl(instrument, position, exit_price)
        cash += pnl
        trades.append({"signal_id": position.signal_id, "side": position.side.value, "size": position.size,
                       "entry": position.entry, "exit": exit_price, "opened_at": position.opened_at,
                       "closed_at": when, "exit_reason": reason, "pnl": pnl,
                       "planned_risk": position.planned_risk})
        position = None

    for i, bar in enumerate(bars):
        # 1. Execute the previous bar's signal at this bar's open.
        if pending is not None:
            signal, pending = pending, None
            now = bar.open_time
            quote = Quote(symbol=symbol, bid=bar.open - half, ask=bar.open + half, time=now)
            equity = mark(bar.open)
            used = Decimal("0")
            positions = ()
            if position is not None:
                used = (position.size * position.entry if isinstance(instrument, BinanceSpotSpec)
                        else position.size * instrument.margin_per_lot)
                positions = (OpenPosition(position.signal_id, symbol, position.side, position.size, position.entry,
                                          position.stop, position.planned_risk),)
            account = AccountSnapshot(
                market=market, account_id="replay", is_demo=True, currency=instrument.account_currency,
                equity=equity, balance=cash, available_funds=max(equity - used, Decimal("0")),
                day_start_equity=equity, snapshot_at=now, reconciled=True, open_positions=positions)
            account = daily.apply(account)
            decision = evaluate(signal, account=account, instrument=instrument, quote=quote, limits=limits,
                                environment=environment, breaker=breaker, now=now, duplicate_check=ledger.seen,
                                session_calendar=session_calendar)
            if decision.approved and not ledger.claim(signal, now=now):
                decision = decision.rejected("Duplicate signal (concurrent claim)")
            decisions.append(decision.to_record())
            if decision.approved:
                long = signal.side is Side.LONG
                fill = quote.ask + slip if long else quote.bid - slip
                position = _Position(signal.signal_id, signal.side, decision.size, fill, signal.stop_price,
                                     signal.take_profit, decision.planned_risk, now)

        # 2. Manage the open position through this bar (stop first).
        if position is not None:
            long = position.side is Side.LONG
            bid_open, ask_open = bar.open - half, bar.open + half
            bid_low, ask_high = bar.low - half, bar.high + half
            opened_this_bar = position.opened_at == bar.open_time
            if long and bid_low <= position.stop:
                gapped = not opened_this_bar and bid_open <= position.stop
                close((bid_open if gapped else position.stop) - slip, bar.open_time, "stop_gap" if gapped else "stop")
            elif not long and ask_high >= position.stop:
                gapped = not opened_this_bar and ask_open >= position.stop
                close((ask_open if gapped else position.stop) + slip, bar.open_time, "stop_gap" if gapped else "stop")
            elif position.take_profit is not None and (
                    (long and bar.high - half >= position.take_profit)
                    or (not long and bar.low + half <= position.take_profit)):
                close(position.take_profit, bar.open_time, "take_profit")

        # 3. Strategy sees closed bars only.
        candidate = strategy(tuple(bars[: i + 1]))
        if candidate is not None:
            if candidate.bar_close_time != bar.close_time or candidate.generated_at < bar.close_time:
                decisions.append({"approved": False, "signal_id": candidate.signal_id,
                                  "reasons": ["Signal not anchored to the just-closed bar"]})
            elif i + 1 >= len(bars):
                decisions.append({"approved": False, "signal_id": candidate.signal_id,
                                  "reasons": ["No subsequent bar to execute on"]})
            else:
                pending = candidate
        curve.append(mark(bar.close))
        daily.observe("replay", max(curve[-1], Decimal("0.01")), bar.close_time)

    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [-t["pnl"] for t in trades if t["pnl"] <= 0]
    final = mark(bars[-1].close)
    reasons: dict[str, int] = {}
    for record in decisions:
        for reason in record.get("reasons", ()):
            key = reason.split(":")[0].split("(")[0].strip()
            reasons[key] = reasons.get(key, 0) + 1
    return {
        "research_only": True,
        "market": market.value,
        "bars": len(bars),
        "gaps": len(gaps),
        "initial_equity": initial_equity,
        "final_equity": final,
        "net_return_pct": (final / initial_equity - 1) * 100,
        "max_drawdown_pct": _drawdown(curve) * 100,
        "trades": len(trades),
        "win_rate_pct": Decimal(len(wins)) / len(trades) * 100 if trades else Decimal("0"),
        "profit_factor": (sum(wins) / sum(losses)) if losses and sum(losses) > 0 else None,
        "approved": sum(1 for d in decisions if d.get("approved")),
        "rejected": sum(1 for d in decisions if not d.get("approved")),
        "rejection_reasons": reasons,
        "open_position_at_end": position is not None,
        "trade_ledger": trades,
        "decisions": decisions,
        "limitations": "Bar-level pessimistic fills; no partial fills, latency, queue position or swaps/funding.",
    }
