"""Automated calendar ingestion: provider registry, refresh, health status and guards.

The service runs independently of any AI component. Each provider is a deterministic
fetch function attributed to one source; a failed refresh keeps the last good data
but is recorded, and staleness is enforced by the guards it builds (fail closed).

Default official providers (no keys): Federal Reserve FOMC calendar, BEA release
schedule (GDP, PCE), ECB meeting calendar. CPI and payroll *schedules* need a
licensed provider or an operator-supplied structured file (`StructuredFileProvider`);
BLS API values can enrich those events with actual/previous figures.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from trading.common import Market, ValidationError, require_utc
from trading.marketcal import fomc, official
from trading.marketcal.blackout import DEFAULT_RULES, BlackoutRule, EventBlackout
from trading.marketcal.events import EconomicEvent, EventStore, load_events_file


@dataclass(frozen=True)
class CalendarProvider:
    name: str
    fetch: Callable[[], list[EconomicEvent]]
    series: frozenset  # series this provider is trusted to supply
    max_age: timedelta
    required: bool = True


def official_providers(*, http_get=None) -> list[CalendarProvider]:
    kwargs = {} if http_get is None else {"http_get": http_get}
    return [
        CalendarProvider(fomc.PROVIDER, lambda: fomc.fetch_fomc_calendar(**kwargs), frozenset({"FOMC_DECISION"}),
                         timedelta(days=14)),
        CalendarProvider(official.BEA_PROVIDER, lambda: official.fetch_bea(**kwargs),
                         frozenset({"US_GDP", "US_PCE"}), timedelta(days=7)),
        CalendarProvider(official.ECB_PROVIDER, lambda: official.fetch_ecb(**kwargs), frozenset({"ECB_DECISION"}),
                         timedelta(days=14), required=False),
    ]


def structured_file_provider(path: Path, *, provider: str, series: set[str],
                             max_age: timedelta = timedelta(days=31)) -> CalendarProvider:
    """Operator/licensed-vendor export (JSON list of EconomicEvent records attributed to `provider`)."""
    return CalendarProvider(provider, lambda: load_events_file(Path(path), provider=provider), frozenset(series),
                            max_age)


# Series whose schedules must be covered before new entries are allowed (per market).
REQUIRED_SERIES = {
    Market.XAUUSD_MT5: ("FOMC_DECISION", "US_CPI", "US_NFP", "US_GDP", "US_PCE"),
    Market.BTCUSDT_BINANCE_SPOT: ("FOMC_DECISION", "US_CPI"),
}


@dataclass
class CalendarService:
    store: EventStore
    providers: list[CalendarProvider]
    history: list[dict] = field(default_factory=list)

    def refresh(self, now: datetime) -> dict:
        require_utc(now, "now")
        results = {}
        for provider in self.providers:
            try:
                events = provider.fetch()
                foreign = {e.provider for e in events} - {provider.name}
                if foreign:
                    raise ValidationError(f"provider returned events attributed to {sorted(foreign)}")
                outside = {e.series for e in events if e.series and e.series not in provider.series}
                if outside:
                    raise ValidationError(f"provider returned series it is not trusted for: {sorted(outside)}")
                if not events:
                    raise ValidationError("provider returned no events")
                change = self.store.upsert(events)
                self.store.record_refresh(provider.name, now, len(events))
                results[provider.name] = {"ok": True, **change}
            except Exception as exc:  # keep last good data; staleness is enforced by guards
                self.store.record_failure(provider.name, now, f"{type(exc).__name__}: {exc}")
                results[provider.name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self.history.append({"at": now.isoformat(), "results": results})
        return results

    def status(self, now: datetime) -> dict:
        out = {}
        for p in self.providers:
            refreshed = self.store.last_refresh(p.name)
            failure = self.store.last_failure(p.name)
            out[p.name] = {"series": sorted(p.series), "required": p.required,
                           "last_refresh": refreshed.isoformat() if refreshed else None,
                           "fresh": refreshed is not None and now - refreshed <= p.max_age,
                           "last_failure": None if failure is None else
                           {"at": failure[0].isoformat(), "error": failure[1]}}
        return out

    def covered_series(self) -> frozenset:
        return frozenset().union(*(p.series for p in self.providers)) if self.providers else frozenset()

    def guard(self, market: Market, *, rules: tuple[BlackoutRule, ...] | None = None,
              required_series: tuple[str, ...] | None = None) -> EventBlackout:
        required = tuple(p.name for p in self.providers if p.required)
        return EventBlackout(store=self.store, market=market, rules=rules or DEFAULT_RULES[market],
                             required_providers=required, max_calendar_age=None,
                             provider_max_age={p.name: p.max_age for p in self.providers},
                             required_series=REQUIRED_SERIES[market] if required_series is None else required_series,
                             covered_series=self.covered_series())
