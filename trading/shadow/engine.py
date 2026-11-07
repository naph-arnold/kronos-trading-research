"""Deterministic shadow-trading engine: live closed bars + live ticks -> strategies -> risk engine ->
simulated fills. It has NO order-routing capability; positions exist only in the shadow store.

Event model (strict time order; a bar-close event at time C precedes ticks stamped >= C):

* Bar close (C): positions opened before C age by one bar; a position reaching its maximum holding
  period exits at the last tick of that bar (time exit). Every strategy then sees the closed bar
  (closed bars only; `HistoryView` prevents lookahead). A signal on a bar closing before the
  pre-registered unseen-data start is recorded as `warmup` and never traded.
* Tick: feeds the market-health monitor; open positions check stop first, then take-profit; then
  pending signals fill at this tick (the first tick at or after the signal bar's close = the next bar's
  open), each subscribed book evaluated independently by the unchanged `trading.risk.evaluate`.

Integrity rules:
* An entry is only simulated if the engine processed its fill tick within `max_decision_latency` of
  real time. After downtime the catch-up still manages open positions (broker-side stops would have
  worked) but every entry decision it would have needed is rejected as "system not live".
* Ticks later than one bar beyond the last ingested closed bar are deferred until that bar arrives,
  so a fill can never skip the bar-close event that created it.
* All state changes from one `process` call commit atomically; on any error the call rolls back and
  in-memory state is rebuilt from the store.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Sequence

from trading.breaker import CircuitBreaker
from trading.common import Market, Side, ValidationError, require_utc
from trading.config import DemoEnvironment
from trading.daily import DailyLossLedger
from trading.health import MarketHealthMonitor
from trading.instruments import Mt5InstrumentSpec
from trading.marketdata import Bar, Quote
from trading.replay import HistoryView
from trading.risk import RiskLimits, evaluate
from trading.sessions import TradingCalendar
from trading.shadow.costs import SwapSpec
from trading.shadow.store import ShadowStore
from trading.signals import TradeSignal
from trading.state import AccountSnapshot, OpenPosition

MARKET = Market.XAUUSD_MT5
M15 = timedelta(minutes=15)
TWO = Decimal("2")
ZERO = Decimal("0")
log = logging.getLogger("trading.shadow")


@dataclass(frozen=True)
class BookSpec:
    """One hypothetical account running one strategy under one commission scenario."""

    book_id: str
    strategy_key: str
    initial_equity: Decimal
    commission: Decimal  # USD per 1.00 lot, round trip (assumption scenario)
    max_holding_bars: int | None = None


@dataclass
class SimPosition:
    book_id: str
    signal_id: str
    strategy_id: str
    side: Side
    size: Decimal
    entry: Decimal  # simulated fill (executable side + slippage)
    entry_mid: Decimal
    entry_spread: Decimal
    opened_at: datetime
    signal_bar_close: datetime
    stop: Decimal
    take_profit: Decimal | None
    planned_risk: Decimal
    risk_budget: Decimal
    bars_held: int = 0
    best: Decimal | None = None  # most favourable executable exit price seen
    worst: Decimal | None = None  # most adverse executable exit price seen

    def to_body(self) -> dict:
        body = asdict(self)
        body["side"] = self.side.value
        return body

    @classmethod
    def from_body(cls, body: dict) -> "SimPosition":
        values = dict(body)
        values["side"] = Side(values["side"])
        for name in ("size", "entry", "entry_mid", "entry_spread", "stop", "planned_risk", "risk_budget"):
            values[name] = Decimal(values[name])
        for name in ("take_profit", "best", "worst"):
            values[name] = None if values[name] is None else Decimal(values[name])
        for name in ("opened_at", "signal_bar_close"):
            values[name] = datetime.fromisoformat(values[name])
        return cls(**values)


@dataclass
class EngineSettings:
    unseen_start: datetime  # pre-registered: bars closing earlier are warm-up only
    max_decision_latency: timedelta = timedelta(seconds=30)
    spec_max_age: timedelta = timedelta(days=1)
    fallback_half_spread: Decimal = Decimal("0.085")  # tick p50 0.17 / 2; used only when a bar has no ticks


@dataclass
class _Pending:
    strategy_key: str
    signal: TradeSignal


@dataclass
class ShadowEngine:
    store: ShadowStore
    books: Sequence[BookSpec]
    strategy_factory: Callable[[str], Callable]
    instruments: dict  # commission (Decimal) -> Mt5InstrumentSpec with that commission assumption
    spec_retrieved_at: datetime
    limits: RiskLimits
    environment: DemoEnvironment
    breaker: CircuitBreaker
    daily: DailyLossLedger
    calendar: TradingCalendar
    event_guard: Callable[[datetime], "str | None"]
    swap: SwapSpec
    settings: EngineSettings
    health: MarketHealthMonitor = field(default_factory=lambda: MarketHealthMonitor(MARKET))

    def __post_init__(self):
        if self.limits.market is not MARKET or self.breaker.market is not MARKET or self.daily.market is not MARKET:
            raise ValueError("Shadow engine is XAUUSD-only")
        if self.daily.halt_fraction != self.limits.daily_loss_halt_fraction:
            raise ValueError("Daily ledger and limits disagree on the halt threshold")
        ids = [b.book_id for b in self.books]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate book ids")
        for book in self.books:
            if book.commission not in self.instruments:
                raise ValueError(f"No instrument spec for commission scenario {book.commission}")
        self.swap.validate()
        with self.store.transaction():
            for book in self.books:
                self.store.ensure_book(book.book_id, book.strategy_key, book.initial_equity, book.commission)
        self.restore()

    # ------------------------------------------------------------------ state
    @property
    def strategy_keys(self) -> list[str]:
        return sorted({b.strategy_key for b in self.books})

    def restore(self) -> dict:
        """Rebuild in-memory state from the store. Strategy state is a pure function of the stored bar
        sequence; regenerated signals must match the recorded ones, otherwise the breaker trips."""
        self.strategies = {key: self.strategy_factory(key) for key in self.strategy_keys}
        self.bars: list[Bar] = []
        regenerated: set[tuple[str, str]] = set()
        for bar in self.store.load_bars():
            self.bars.append(bar)
            view = HistoryView(self.bars, len(self.bars))
            for key, strategy in self.strategies.items():
                signal = strategy(view)
                if signal is not None:
                    regenerated.add((key, signal.signal_id))
        recorded = {(s["strategy_key"], s["signal_id"]) for s in self.store.signals()}
        mismatch = recorded ^ regenerated
        if mismatch:
            self.breaker.trip(f"Shadow strategy state not reproducible after restart ({len(mismatch)} signals differ)")
        self.positions = {k: SimPosition.from_body(v) for k, v in self.store.positions().items()}
        self.realized = {k: v["realized"] for k, v in self.store.books().items()}
        self.pending = [_Pending(s["strategy_key"], _signal_from_body(s["body"]))
                        for s in self.store.signals(status="pending")]
        self.last_bar_close = self.bars[-1].close_time if self.bars else None
        tick = self.store.get_meta("last_tick")
        self.last_tick = None if tick is None else _quote_from_meta(tick)
        self.ticks_in_bar = int(self.store.get_meta("ticks_in_bar") or 0)
        report = self.reconcile()
        return {"bars": len(self.bars), "positions": len(self.positions), "pending": len(self.pending),
                "signals_recorded": len(recorded), "signals_mismatch": len(mismatch), "reconciliation": report}

    def reconcile(self) -> dict:
        """Cross-check books, positions, trades and decisions. Any inconsistency trips the breaker."""
        problems = []
        books = self.store.books()
        trades = self.store.realized_from_trades()
        decisions = self.store.decisions()
        approved = {(d["book_id"], d["signal_id"]) for d in decisions if d["approved"]}
        closed = {(t["book_id"], t["signal_id"]) for t in self.store.trades()}
        open_ = {(b, p["signal_id"]) for b, p in self.store.positions().items()}
        for book_id, info in books.items():
            if info["realized"] != trades.get(book_id, ZERO):
                problems.append(f"{book_id}: realized {info['realized']} != sum of trades {trades.get(book_id, ZERO)}")
            if self.realized.get(book_id, ZERO) != info["realized"]:
                problems.append(f"{book_id}: in-memory realized differs from store")
        if closed & open_:
            problems.append(f"positions both open and closed: {sorted(closed & open_)[:3]}")
        if approved != closed | open_:
            problems.append(f"approved decisions without exactly one position/trade: "
                            f"{sorted(approved ^ (closed | open_))[:3]}")
        unknown = {b for b, _ in open_ | closed} - set(books)
        if unknown:
            problems.append(f"positions/trades for unknown books: {sorted(unknown)}")
        if set(self.positions) != {b for b, _ in open_}:
            problems.append("in-memory positions differ from store")
        if problems:
            self.breaker.trip(f"Shadow reconciliation failed: {problems[0]}")
        return {"ok": not problems, "problems": problems, "books": len(books), "trades": len(closed),
                "open_positions": len(open_)}

    # ------------------------------------------------------------------ main entry point
    def process(self, bars: Sequence[Bar], ticks: Sequence[Quote], wall_now: datetime) -> dict:
        """Ingest newly closed bars and new ticks (both may overlap earlier calls; duplicates are skipped)."""
        require_utc(wall_now, "wall_now")
        try:
            with self.store.transaction():
                summary = self._process(bars, ticks, wall_now)
        except Exception:
            self.restore()
            raise
        return summary

    def _process(self, bars, ticks, wall_now) -> dict:
        new_bars = []
        for bar in sorted(bars, key=lambda b: b.open_time):
            bar.validate()
            if bar.symbol != "XAUUSD" or bar.interval != "15m":
                raise ValidationError("Shadow engine expects XAUUSD M15 bars")
            if bar.close_time > wall_now:
                continue  # still forming
            last = new_bars[-1].close_time if new_bars else self.last_bar_close
            if last is not None and bar.close_time <= last:
                continue  # already processed
            if last is not None and bar.open_time < last:
                raise ValidationError("Overlapping bar")
            new_bars.append(bar)
        horizon = (new_bars[-1].close_time if new_bars else self.last_bar_close)
        horizon = None if horizon is None else horizon + M15
        floor = self.last_tick.time if self.last_tick else None
        new_ticks, deferred = [], 0
        for tick in sorted(ticks, key=lambda q: q.time):
            tick.validate()
            if floor is not None and tick.time <= floor:
                continue
            if horizon is None or tick.time >= horizon or tick.time > wall_now:
                deferred += 1
                continue
            new_ticks.append(tick)
        events = [(b.close_time, 0, b) for b in new_bars] + [(t.time, 1, t) for t in new_ticks]
        events.sort(key=lambda e: (e[0], e[1]))
        counts = {"bars": 0, "ticks": 0, "deferred_ticks": deferred, "signals": 0, "entries": 0, "exits": 0}
        for _, kind, item in events:
            if kind == 0:
                counts["signals"] += self._on_bar_close(item, wall_now)
                counts["bars"] += 1
            else:
                entries, exits = self._on_tick(item, wall_now)
                counts["entries"] += entries
                counts["exits"] += exits
                counts["ticks"] += 1
        if self.last_tick is not None:
            self.store.set_meta("last_tick", f"{self.last_tick.time.isoformat()}|{self.last_tick.bid}|"
                                             f"{self.last_tick.ask}")
        self.store.set_meta("ticks_in_bar", str(self.ticks_in_bar))
        return counts

    # ------------------------------------------------------------------ bar close
    def _on_bar_close(self, bar: Bar, wall_now: datetime) -> int:
        close = bar.close_time
        self.store.add_bar(bar, "live" if close >= self.settings.unseen_start else "warmup_history")
        # Pending signals that never saw a tick in the following bar expire unfilled.
        for pending in self.pending:
            for book in self._books_for(pending.strategy_key):
                self._record_rejection(book, pending.signal, close, ["No executable tick within one bar after the "
                                                                     "signal (market closed or feed gap)"], {})
            self.store.set_signal_status(pending.strategy_key, pending.signal.signal_id, "expired")
        self.pending = []
        if self.ticks_in_bar == 0 and self.positions:
            self.store.event(close, "bar_without_ticks", bar_open=bar.open_time.isoformat(),
                             open_positions=sorted(self.positions))
            for book_id in list(self.positions):
                self._bar_fallback_exit(book_id, bar)
        for book_id in list(self.positions):
            position = self.positions[book_id]
            if position.opened_at >= close:
                continue
            position.bars_held += 1
            book = self._book(book_id)
            if book.max_holding_bars is not None and position.bars_held >= book.max_holding_bars:
                quote = self.last_tick if self.last_tick and self.last_tick.time < close else None
                if quote is None:
                    half = self.settings.fallback_half_spread
                    quote = Quote(symbol="XAUUSD", bid=bar.close - half, ask=bar.close + half, time=close)
                slip = self.limits.slippage_allowance
                price = quote.bid - slip if position.side is Side.LONG else quote.ask + slip
                self._close(book_id, price, (quote.bid + quote.ask) / TWO, close, "time_exit", quote)
            else:
                self.store.update_position(book_id, position.to_body())
        self.bars.append(bar)
        self.last_bar_close = close
        self.ticks_in_bar = 0
        produced = 0
        view = HistoryView(self.bars, len(self.bars))
        for key in self.strategy_keys:
            signal = self.strategies[key](view)
            if signal is None:
                continue
            produced += 1
            if signal.bar_close_time != close or signal.generated_at < close:
                status = "rejected_unanchored"
            elif close < self.settings.unseen_start:
                status = "warmup"
            else:
                status = "pending"
            self.store.record_signal(key, signal.signal_id, close, status, _signal_body(signal))
            if status == "pending":
                self.pending.append(_Pending(key, signal))
        if close < self.settings.unseen_start:
            return produced  # warm-up: indicators only; no book can hold a position, nothing to mark
        for book in self.books:
            equity = self._equity(book.book_id, self.last_tick, close)
            self.store.add_equity(book.book_id, close, equity)
            if book.book_id in self.positions:  # flat equity cannot move; closes and decisions observe anyway
                self._observe(book.book_id, equity, close)
        return produced

    # ------------------------------------------------------------------ ticks
    def _on_tick(self, tick: Quote, wall_now: datetime) -> tuple[int, int]:
        after_gap = self.last_tick is None or tick.time - self.last_tick.time > self.health.max_quote_gap
        self.health.record_quote(tick)
        self.last_tick = tick
        self.ticks_in_bar += 1
        exits = sum(self._check_exit(book_id, tick, after_gap) for book_id in list(self.positions))
        entries = 0
        pending, self.pending = self.pending, []
        for item in pending:
            for book in self._books_for(item.strategy_key):
                entries += self._decide(book, item.signal, tick, wall_now)
            self.store.set_signal_status(item.strategy_key, item.signal.signal_id, "evaluated")
        return entries, exits

    def _check_exit(self, book_id: str, tick: Quote, after_gap: bool = False) -> int:
        p = self.positions[book_id]
        long = p.side is Side.LONG
        exit_side = tick.bid if long else tick.ask
        p.best = exit_side if p.best is None else (max(p.best, exit_side) if long else min(p.best, exit_side))
        p.worst = exit_side if p.worst is None else (min(p.worst, exit_side) if long else max(p.worst, exit_side))
        slip = self.limits.slippage_allowance
        mid = (tick.bid + tick.ask) / TWO
        if (long and tick.bid <= p.stop) or (not long and tick.ask >= p.stop):
            price = tick.bid - slip if long else tick.ask + slip
            overshoot = (p.stop - tick.bid) if long else (tick.ask - p.stop)
            self._close(book_id, price, mid, tick.time, "stop_gap" if after_gap and overshoot > 0 else "stop", tick,
                        overshoot)
            return 1
        if p.take_profit is not None and ((long and tick.bid >= p.take_profit)
                                          or (not long and tick.ask <= p.take_profit)):
            self._close(book_id, p.take_profit, mid, tick.time, "take_profit", tick)
            return 1
        return 0

    def _bar_fallback_exit(self, book_id: str, bar: Bar) -> None:
        """Bar-level, stop-first exit check (replay semantics) for a bar that arrived without ticks."""
        p = self.positions[book_id]
        long = p.side is Side.LONG
        half, slip = self.settings.fallback_half_spread, self.limits.slippage_allowance
        if long and bar.low - half <= p.stop:
            self._close(book_id, min(p.stop, bar.open - half) - slip, p.stop + half, bar.open_time, "stop_bar_fallback")
        elif not long and bar.high + half >= p.stop:
            self._close(book_id, max(p.stop, bar.open + half) + slip, p.stop - half, bar.open_time, "stop_bar_fallback")
        elif p.take_profit is not None and ((long and bar.high - half >= p.take_profit)
                                            or (not long and bar.low + half <= p.take_profit)):
            tp = p.take_profit
            self._close(book_id, tp, tp + half if long else tp - half, bar.open_time, "take_profit_bar_fallback")

    # ------------------------------------------------------------------ decisions
    def _decide(self, book: BookSpec, signal: TradeSignal, tick: Quote, wall_now: datetime) -> int:
        if self.store.has_decision(book.book_id, signal.signal_id):
            return 0  # already decided (idempotent re-processing)
        now = tick.time
        instrument = self.instruments[book.commission]
        pre: list[str] = []
        latency = wall_now - now
        if latency > self.settings.max_decision_latency:
            pre.append(f"System not live at decision time: fill tick processed {latency} later "
                       f"(data interruption or restart catch-up)")
        if self.calendar.verification != "broker_verified":
            pre.append("XAUUSD trading-session calendar not verified against the broker")
        if now - self.spec_retrieved_at > self.settings.spec_max_age:
            pre.append("Instrument specification is stale; re-read venue metadata")
        if self.store.claimed(book.book_id, signal.dedup_key, signal.signal_id):
            pre.append("Duplicate signal (already claimed)")
        equity = self._equity(book.book_id, tick, now)
        position = self.positions.get(book.book_id)
        positions, used = (), ZERO
        if position is not None:
            positions = (OpenPosition(position.signal_id, "XAUUSD", position.side, position.size, position.entry,
                                      position.stop, self._risk_to_stop(position, instrument)),)
            used = position.size * instrument.margin_per_lot
        balance = book.initial_equity + self.realized[book.book_id]
        account = AccountSnapshot(market=MARKET, account_id=_account_id(book.book_id), is_demo=True,
                                  currency=instrument.account_currency, equity=equity, balance=balance,
                                  available_funds=max(equity - used, ZERO), day_start_equity=equity,
                                  snapshot_at=now, reconciled=True, open_positions=positions)
        try:
            account = self.daily.apply(account)
        except Exception as exc:  # fail closed, like the pre-trade gate
            pre.append(f"Daily loss accounting unavailable: {type(exc).__name__}: {exc}")
        guard = self._guard()
        decision = evaluate(signal, account=account, instrument=instrument, quote=tick, limits=self.limits,
                            environment=self.environment, breaker=self.breaker, now=now,
                            duplicate_check=lambda s: self.store.claimed(book.book_id, s.dedup_key, s.signal_id),
                            session_calendar=self.calendar, event_guard=guard)
        for reason in pre:
            decision = decision.rejected(reason)
        context = {"bid": tick.bid, "ask": tick.ask, "spread": tick.spread, "tick_time": now,
                   "processed_at": wall_now, "latency_s": latency.total_seconds(), "equity": equity,
                   "day_start_equity": account.day_start_equity, "daily_loss_halted": account.daily_loss_halted,
                   "risk_budget": decision.risk_budget, "loss_per_lot": decision.loss_per_unit,
                   "signal_entry": signal.entry_price, "signal_stop": signal.stop_price,
                   "signal_take_profit": signal.take_profit, "side": signal.side.value,
                   "stop_distance": abs(signal.entry_price - signal.stop_price),
                   "calendar_reason": self.calendar.tradeable_reason(now), "guard_reason": _safe(guard, now),
                   "commission_per_lot_round_trip": book.commission, "commission_verified": False}
        self.store.record_decision(book.book_id, signal.signal_id, dedup_key=signal.dedup_key, evaluated_at=now,
                                   approved=decision.approved, size=decision.size, planned_risk=decision.planned_risk,
                                   reasons=decision.reasons, context=context)
        if not decision.approved:
            return 0
        long = signal.side is Side.LONG
        slip = self.limits.slippage_allowance
        fill = tick.ask + slip if long else tick.bid - slip
        sim = SimPosition(book.book_id, signal.signal_id, signal.strategy_id, signal.side, decision.size, fill,
                          (tick.bid + tick.ask) / TWO, tick.spread, now, signal.bar_close_time, signal.stop_price,
                          signal.take_profit, decision.planned_risk, decision.risk_budget)
        self.positions[book.book_id] = sim
        self.store.open_position(book.book_id, signal.signal_id, sim.to_body())
        return 1

    def _record_rejection(self, book: BookSpec, signal: TradeSignal, at: datetime, reasons: list[str],
                          context: dict) -> None:
        if not self.store.has_decision(book.book_id, signal.signal_id):
            self.store.record_decision(book.book_id, signal.signal_id, dedup_key=signal.dedup_key, evaluated_at=at,
                                       approved=False, size=ZERO, planned_risk=None, reasons=reasons, context=context)

    def _guard(self) -> Callable[[datetime], "str | None"]:
        def combined(now: datetime) -> str | None:
            reason = self.event_guard(now) if self.event_guard is not None else "No calendar guard (fail closed)"
            if reason:
                return reason
            health = self.health.guard(now)
            return None if health is None else f"market health: {health}"
        return combined

    # ------------------------------------------------------------------ accounting
    def _close(self, book_id: str, price: Decimal, exit_mid: Decimal, when: datetime, reason: str,
               tick: Quote | None = None, stop_overshoot: Decimal | None = None) -> None:
        p = self.positions.pop(book_id)
        book = self._book(book_id)
        instrument = self.instruments[book.commission]
        direction = 1 if p.side is Side.LONG else -1
        value = instrument.value_per_price_unit(p.size)
        price_pnl = value * (price - p.entry) * direction
        commission = instrument.round_trip_cost(p.size, p.entry, price)
        swap, nights = self.swap.charge(p.side, p.size, p.opened_at, when)
        pnl = price_pnl - commission + swap
        gross = value * (exit_mid - p.entry_mid) * direction
        risk_px = abs(p.entry - p.stop) or Decimal("1E-9")
        body = {**p.to_body(), "exit": price, "exit_mid": exit_mid, "closed_at": when, "exit_reason": reason,
                "exit_bid": None if tick is None else tick.bid, "exit_ask": None if tick is None else tick.ask,
                "stop_overshoot": stop_overshoot, "gross_pnl": gross, "spread_slippage_cost": gross - price_pnl, "commission": commission,
                "swap": swap, "swap_nights": nights, "pnl": pnl,
                "r_multiple": pnl / p.planned_risk if p.planned_risk else None,
                "r_multiple_before_commission": (pnl + commission) / p.planned_risk if p.planned_risk else None,
                "mfe_r": None if p.best is None else (p.best - p.entry) * direction / risk_px,
                "mae_r": None if p.worst is None else (p.entry - p.worst) * direction / risk_px,
                "loss_beyond_planned": max(-pnl - p.planned_risk, ZERO)}
        self.realized[book_id] += pnl
        self.store.close_position(book_id, p.signal_id, when, pnl, body)
        self.store.set_realized(book_id, self.realized[book_id])
        self._observe(book_id, self._equity(book_id, tick, when), when)

    def _risk_to_stop(self, p: SimPosition, instrument: Mt5InstrumentSpec) -> Decimal:
        slip = self.limits.slippage_allowance
        stop_fill = p.stop - slip if p.side is Side.LONG else p.stop + slip
        direction = 1 if p.side is Side.LONG else -1
        loss = instrument.value_per_price_unit(p.size) * (p.entry - stop_fill) * direction
        return max(loss + instrument.round_trip_cost(p.size, p.entry, stop_fill), ZERO)

    def _equity(self, book_id: str, quote: Quote | None, at: datetime) -> Decimal:
        book = self._book(book_id)
        equity = book.initial_equity + self.realized[book_id]
        p = self.positions.get(book_id)
        if p is None or quote is None:
            return equity
        instrument = self.instruments[book.commission]
        direction = 1 if p.side is Side.LONG else -1
        exit_price = quote.bid if p.side is Side.LONG else quote.ask
        unrealized = (instrument.value_per_price_unit(p.size) * (exit_price - p.entry) * direction
                      - instrument.round_trip_cost(p.size, p.entry, exit_price)
                      + self.swap.charge(p.side, p.size, p.opened_at, max(at, p.opened_at))[0])
        return equity + unrealized

    def _observe(self, book_id: str, equity: Decimal, at: datetime) -> None:
        try:
            self.daily.observe(_account_id(book_id), max(equity, Decimal("0.01")), at)
        except ValidationError as exc:  # e.g. re-processing after a rollback; entries re-check via apply()
            log.warning("daily ledger observation skipped for %s: %s", book_id, exc)

    def _book(self, book_id: str) -> BookSpec:
        return next(b for b in self.books if b.book_id == book_id)

    def _books_for(self, strategy_key: str) -> list[BookSpec]:
        return [b for b in self.books if b.strategy_key == strategy_key]

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        return {"bars": len(self.bars), "last_bar_close": None if self.last_bar_close is None
                else self.last_bar_close.isoformat(),
                "last_tick": None if self.last_tick is None else self.last_tick.time.isoformat(),
                "pending_signals": [p.signal.signal_id for p in self.pending],
                "open_positions": {k: {"signal_id": v.signal_id, "side": v.side.value, "size": str(v.size),
                                       "entry": str(v.entry), "stop": str(v.stop)} for k, v in self.positions.items()},
                "breaker": self.breaker.reasons(), "calendar_verification": self.calendar.verification}


# ---------------------------------------------------------------------- helpers

def _account_id(book_id: str) -> str:
    return "shadow-" + book_id


def _safe(guard, now):
    try:
        return guard(now)
    except Exception as exc:  # recorded only; evaluate() already fails closed on guard errors
        return f"guard error: {type(exc).__name__}: {exc}"


def _signal_body(signal: TradeSignal) -> dict:
    return {"signal_id": signal.signal_id, "market": signal.market.value, "symbol": signal.symbol,
            "side": signal.side.value, "entry_price": str(signal.entry_price), "stop_price": str(signal.stop_price),
            "take_profit": None if signal.take_profit is None else str(signal.take_profit),
            "strategy_id": signal.strategy_id, "strategy_version": signal.strategy_version,
            "generated_at": signal.generated_at.isoformat(), "bar_close_time": signal.bar_close_time.isoformat(),
            "provenance": signal.provenance}


def _signal_from_body(body: dict) -> TradeSignal:
    values = dict(body)
    for name in ("generated_at", "bar_close_time"):
        values[name] = datetime.fromisoformat(values[name])
    return TradeSignal.create(**values)


def _quote_from_meta(text: str) -> Quote:
    at, bid, ask = text.split("|")
    return Quote(symbol="XAUUSD", bid=Decimal(bid), ask=Decimal(ask), time=datetime.fromisoformat(at))
