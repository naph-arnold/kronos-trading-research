"""Pre-trade gate: risk evaluation + atomic duplicate claim + mandatory audit.

The gate produces decisions only. It has no order-routing capability. Advisory
inputs (AI agents, Kronos, TradingView) are recorded for provenance but are
never passed to the risk engine and cannot change its outcome.
"""
from __future__ import annotations

import logging
from datetime import datetime

from trading.audit import AuditLog, log_event
from trading.breaker import CircuitBreaker
from trading.config import DemoEnvironment
from trading.daily import DailyLossLedger
from trading.instruments import InstrumentSpec
from trading.ledger import SignalLedger
from trading.marketdata import Quote
from trading.risk import RiskDecision, RiskLimits, evaluate
from trading.sessions import TradingCalendar
from trading.signals import TradeSignal
from trading.state import AccountSnapshot


class PreTradeGate:
    def __init__(self, *, environment: DemoEnvironment, limits: RiskLimits, breaker: CircuitBreaker,
                 ledger: SignalLedger, audit: AuditLog, daily_ledger: DailyLossLedger,
                 session_calendar: TradingCalendar | None = None, logger: logging.Logger | None = None):
        if breaker.market is not limits.market or daily_ledger.market is not limits.market:
            raise ValueError("Breaker, daily ledger and limits must belong to the same market")
        if daily_ledger.halt_fraction != limits.daily_loss_halt_fraction:
            raise ValueError("Daily ledger and limits disagree on the daily halt threshold")
        self.daily_ledger = daily_ledger
        self.session_calendar = session_calendar
        self.environment = environment
        self.limits = limits
        self.breaker = breaker
        self.ledger = ledger
        self.audit = audit
        self.logger = logger or logging.getLogger(f"trading.gate.{limits.market.value}")

    def evaluate(self, signal: TradeSignal, *, account: AccountSnapshot, instrument: InstrumentSpec,
                 quote: Quote, now: datetime, advisory: dict | None = None) -> RiskDecision:
        daily_error = None
        try:  # Day-start equity and the halt latch always come from the persisted ledger.
            account = self.daily_ledger.apply(account)
        except Exception as exc:
            daily_error = f"Daily loss accounting unavailable: {type(exc).__name__}: {exc}"
        decision = evaluate(signal, account=account, instrument=instrument, quote=quote, limits=self.limits,
                            environment=self.environment, breaker=self.breaker, now=now,
                            duplicate_check=self.ledger.seen, session_calendar=self.session_calendar)
        if daily_error:
            decision = decision.rejected(daily_error)
        if decision.approved:
            try:
                claimed = self.ledger.claim(signal, now=now)
            except Exception as exc:  # fail closed
                decision = decision.rejected(f"Duplicate ledger unavailable: {type(exc).__name__}")
            else:
                if not claimed:
                    decision = decision.rejected("Duplicate signal (concurrent claim)")
        record = {
            "type": "pre_trade_decision",
            "decision": decision.to_record(),
            "signal": signal,
            "account": account,
            "quote": quote,
            "limits": self.limits,
            "advisory_inputs_not_used_by_risk": advisory or {},
        }
        try:
            record_hash = self.audit.append(record)
        except Exception as exc:  # no audit, no approval
            self.breaker.trip(f"Audit write failed: {type(exc).__name__}")
            decision = decision.rejected(f"Audit write failed: {type(exc).__name__}")
            record_hash = None
        log_event(self.logger, "pre_trade_decision", approved=decision.approved, signal_id=decision.signal_id,
                  market=decision.market, size=decision.size, reasons=list(decision.reasons),
                  audit_sha256=record_hash)
        return decision
