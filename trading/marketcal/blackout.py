"""News-related trading safeguards: deterministic no-new-trade windows around events.

`EventBlackout` is passed to the risk engine as `event_guard`. It blocks new
entries from `before` minutes ahead of a matching event until `after` minutes
following it. When calendar data is required but stale (no successful refresh
within `max_calendar_age`), it fails closed. AI output cannot shorten or remove
a blackout: rules are configuration, events come only from the event store.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trading.common import Market, require_utc
from trading.marketcal.events import IMPACTS, EconomicEvent, EventStore


@dataclass(frozen=True)
class BlackoutRule:
    currencies: tuple[str, ...] = ("USD",)
    min_impact: str = "high"
    before: timedelta = timedelta(minutes=30)
    after: timedelta = timedelta(minutes=30)
    categories: tuple[str, ...] | None = None  # None = any category
    all_day_block: bool = True  # events without a time block their whole UTC day

    def matches(self, event: EconomicEvent) -> bool:
        return (event.currency in self.currencies and IMPACTS.index(event.impact) >= IMPACTS.index(self.min_impact)
                and event.status != "cancelled"
                and (self.categories is None or event.category in self.categories))


DEFAULT_RULES = {
    # Gold reacts strongly to US data and Fed decisions; widen around central-bank events.
    Market.XAUUSD_MT5: (BlackoutRule(before=timedelta(minutes=30), after=timedelta(minutes=30)),
                        BlackoutRule(categories=("central_bank",), before=timedelta(minutes=60),
                                     after=timedelta(minutes=90))),
    Market.BTCUSDT_BINANCE_SPOT: (BlackoutRule(before=timedelta(minutes=15), after=timedelta(minutes=30)),),
}


@dataclass
class EventBlackout:
    store: EventStore
    market: Market
    rules: tuple[BlackoutRule, ...] = ()
    required_providers: tuple[str, ...] = ()
    max_calendar_age: timedelta | None = timedelta(days=7)
    provider_max_age: dict | None = None  # per-provider override of max_calendar_age
    required_series: tuple[str, ...] = ()  # e.g. ("FOMC_DECISION", "US_CPI", "US_NFP")
    covered_series: frozenset = frozenset()  # series that configured providers can supply
    _cache: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if not self.rules:
            self.rules = DEFAULT_RULES[self.market]

    def __call__(self, now: datetime) -> str | None:
        require_utc(now, "now")
        missing = [s for s in self.required_series if s not in self.covered_series]
        if missing:
            return f"no verified calendar provider configured for {', '.join(missing)} (fail closed)"
        if self.max_calendar_age is not None or self.provider_max_age:
            for provider in self.required_providers:
                max_age = (self.provider_max_age or {}).get(provider, self.max_calendar_age)
                refreshed = self.store.last_refresh(provider)
                if max_age is not None and (refreshed is None or now - refreshed > max_age):
                    return f"economic calendar from {provider} is missing or stale (fail closed)"
        horizon = max(max(r.before, r.after) for r in self.rules) + timedelta(days=1)
        conflicts = self.store.contradictions(now - horizon, now + horizon)
        if conflicts:
            c = conflicts[0]
            return f"contradictory calendar data for {c['series']}: {c['a']} vs {c['b']} (fail closed)"
        for event in self.store.between(now - horizon, now + horizon):
            for rule in self.rules:
                if not rule.matches(event):
                    continue
                if event.all_day and rule.all_day_block:
                    if event.scheduled_at.date() == now.date():
                        return f"{event.name} ({event.currency}, time unconfirmed) today"
                    continue
                if event.scheduled_at - rule.before <= now <= event.scheduled_at + rule.after:
                    return (f"{event.name} ({event.currency}, {event.impact}) at "
                            f"{event.scheduled_at.isoformat()}")
        return None
