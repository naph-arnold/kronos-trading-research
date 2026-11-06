"""XAUUSD holiday/early-close closures and broker overrides.

Primary rule-based source: `pandas_market_calendars` "CMEGlobex_Gold" (MIT licence),
which encodes CME Globex gold trading hours, holidays and early closes. Spot XAUUSD
at MT5 brokers normally follows these closures, but brokers can differ, so:
* broker notices are added as `BrokerClosure` overrides (source + reference required);
* closures are *unioned* (any source saying "closed" wins);
* `compare_with_bars` reports where broker history disagrees with the calendar.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Sequence

from trading.common import ValidationError, require_utc
from trading.marketdata import INTERVALS, Bar
from trading.sessions import TradingCalendar

CLOSURE_KINDS = ("holiday", "early_close", "session_change", "suspension", "restriction")
CME_GOLD_CALENDAR = "CMEGlobex_Gold"


@dataclass(frozen=True)
class BrokerClosure:
    start: datetime
    end: datetime
    kind: str
    source: str  # e.g. "broker email", "MT5 news", "broker website"
    reference: str  # URL / message id / document reference
    recorded_at: datetime

    def validate(self) -> None:
        for name in ("start", "end", "recorded_at"):
            require_utc(getattr(self, name), name)
        if self.end <= self.start:
            raise ValidationError("Closure end must follow start")
        if self.kind not in CLOSURE_KINDS:
            raise ValidationError(f"Unknown closure kind {self.kind!r}")
        if not self.source.strip() or not self.reference.strip():
            raise ValidationError("Broker closures require a source and a reference")


def load_broker_closures(path: Path) -> list[BrokerClosure]:
    """JSON list of {start, end, kind, source, reference, recorded_at} (ISO-8601 UTC)."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValidationError("Broker closure file must be a JSON list")
    closures = []
    for item in raw:
        try:
            closure = BrokerClosure(**{k: (datetime.fromisoformat(item[k].replace("Z", "+00:00"))
                                           if k in ("start", "end", "recorded_at") else item[k])
                                       for k in ("start", "end", "kind", "source", "reference", "recorded_at")})
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValidationError(f"Malformed broker closure: {item!r}") from exc
        closure.validate()
        closures.append(closure)
    return closures


def cme_gold_closures(start: date, end: date) -> dict:
    """Closed intervals (UTC) between CME Globex gold sessions in [start, end].

    Includes daily breaks and weekends (harmless duplicates of the session template)
    plus holidays and early closes. Returns provenance with the library version.
    """
    import importlib.metadata

    import pandas_market_calendars as mcal

    calendar = mcal.get_calendar(CME_GOLD_CALENDAR)
    schedule = calendar.schedule(start_date=start.isoformat(), end_date=end.isoformat())
    sessions = [(o.to_pydatetime(), c.to_pydatetime()) for o, c in zip(schedule["market_open"],
                                                                         schedule["market_close"])]
    closures = []
    for (_, close), (next_open, _) in zip(sessions, sessions[1:]):
        if next_open > close:
            closures.append((close, next_open))
    early = [(d.date().isoformat(), c.isoformat()) for d, c in zip(schedule.index, schedule["market_close"])
             if c.to_pydatetime().hour not in (21, 22) or c.to_pydatetime().minute]
    sessions_by_day = {d.date() for d in schedule.index}
    weekdays = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    holidays = [d.isoformat() for d in weekdays if d.weekday() < 5 and d not in sessions_by_day]
    return {
        "closures": closures,
        "holidays": holidays,
        "early_closes": early,
        "provenance": {"provider": "pandas_market_calendars", "calendar": CME_GOLD_CALENDAR,
                       "version": importlib.metadata.version("pandas_market_calendars"), "licence": "MIT",
                       "method": "rule-based exchange calendar (CME Globex gold)",
                       "range": [start.isoformat(), end.isoformat()]},
    }


def build_xauusd_calendar(template: TradingCalendar, start: date, end: date,
                          broker_closures: Sequence[BrokerClosure] = ()) -> tuple[TradingCalendar, dict]:
    """Template sessions + CME gold holidays/early closes + broker overrides (union)."""
    cme = cme_gold_closures(start, end)
    for closure in broker_closures:
        closure.validate()
    closures = tuple(sorted(set(template.closures) | set(cme["closures"])
                            | {(c.start, c.end) for c in broker_closures}))
    calendar = replace(template, closures=closures)
    calendar.validate()
    provenance = {**cme["provenance"], "holidays": cme["holidays"], "early_closes": cme["early_closes"],
                  "broker_closures": [{"start": c.start.isoformat(), "end": c.end.isoformat(), "kind": c.kind,
                                       "source": c.source, "reference": c.reference} for c in broker_closures]}
    return calendar, provenance


def compare_with_bars(bars: Sequence[Bar], calendar: TradingCalendar) -> dict:
    """Non-raising broker-vs-calendar check: bars printed while the calendar says closed, and
    missing bars while it says open. Used to verify broker behaviour, not to filter data."""
    if not bars:
        return {"bars_in_closed_time": [], "missing_in_open_time": []}
    step = INTERVALS[bars[0].interval]
    in_closed = [b.open_time.isoformat() for b in bars if not calendar.is_open(b.open_time)]
    missing = []
    for prev, nxt in zip(bars, bars[1:]):
        moment = prev.close_time
        while moment < nxt.open_time:
            if calendar.is_open(moment):
                missing.append(moment.isoformat())
            moment += step
    return {"bars_in_closed_time": in_closed, "missing_in_open_time": missing,
            "consistent": not in_closed and not missing}
