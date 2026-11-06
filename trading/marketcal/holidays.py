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
    calendar = replace(template, closures=closures, verification="rule_based")
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


def mark_broker_verified(calendar: TradingCalendar, bars: Sequence[Bar], *, offset_verified: bool,
                         min_bars: int = 2000) -> TradingCalendar:
    """Promote a calendar to "broker_verified" only if the broker's own recent history agrees
    with it (no bars in closed time, no missing bars in open time) and the server offset was
    verified. Otherwise raise; the gate keeps rejecting XAUUSD entries."""
    report = compare_with_bars(bars, calendar)
    if not offset_verified:
        raise ValidationError("Server time offset not verified")
    # The calendar may be STRICTER than the broker (bars printed while it says closed are acceptable:
    # we merely skip those periods) but never more permissive: any bar missing while it says open fails.
    if len(bars) < min_bars or report["missing_in_open_time"]:
        raise ValidationError(f"Broker history does not confirm the calendar ({len(bars)} bars, "
                              f"{len(report['missing_in_open_time'])} missing while calendar open)")
    return replace(calendar, verification="broker_verified")


def broker_xauusd_calendar(bars: Sequence[Bar], *, server_tz, start: date, end: date, weeks: int = 8,
                           broker_closures: Sequence[BrokerClosure] = ()) -> tuple[TradingCalendar, dict]:
    """New York market hours + CME holidays/early closes + broker closures, constrained by the broker's
    sessions observed in its own server time over the last `weeks` (intersection = most restrictive)."""
    from trading.sessions import XAUUSD_NY_MARKET_HOURS, SessionWindow
    market, provenance = build_xauusd_calendar(XAUUSD_NY_MARKET_HOURS, start, end, broker_closures)
    broad = TradingCalendar(symbol="XAUUSD", server_tz=server_tz,
                            windows=tuple(SessionWindow(d, 0, 1440) for d in range(7)),
                            open_buffer=timedelta(0), close_buffer=timedelta(0))
    profile = observed_session_profile(bars, broad, weeks=weeks)
    broker = profile_calendar(broad, profile)
    calendar = replace(market, constraints=(broker,), verification="broker_observed")
    provenance = {**provenance, "broker_profile": {"weeks": weeks, "since": profile["since"], "tz": server_tz.name,
                                                   "differences_vs_server_day": compare_profile_with_calendar(
                                                       broker, broad)}}
    return calendar, provenance


def observed_session_profile(bars: Sequence[Bar], calendar: TradingCalendar, *, weeks: int = 8,
                             min_presence: float = 0.6) -> dict:
    """Which local-time slots (in `calendar.server_tz`, e.g. New York) actually printed bars in the
    broker's most recent `weeks` of history. A slot is "observed open" when it printed in at least
    `min_presence` of the weeks where that weekday traded. Used to detect broker session changes."""
    if not bars:
        raise ValidationError("No bars")
    step = INTERVALS[bars[0].interval]
    cutoff = bars[-1].open_time - timedelta(weeks=weeks)
    recent = [b for b in bars if b.open_time >= cutoff]
    tz = calendar.server_tz
    seen: dict[int, dict[int, set]] = {}
    days_traded: dict[int, set] = {}
    for bar in recent:
        local = tz.to_server(bar.open_time)
        minute = local.hour * 60 + local.minute
        seen.setdefault(local.weekday(), {}).setdefault(minute, set()).add(local.date())
        days_traded.setdefault(local.weekday(), set()).add(local.date())
    slot = int(step.total_seconds() // 60)
    profile = {}
    for weekday, slots in seen.items():
        n_days = len(days_traded[weekday])
        profile[weekday] = sorted(m for m, days in slots.items() if len(days) >= min_presence * n_days)
    return {"weeks": weeks, "slot_minutes": slot, "tz": tz.name, "since": cutoff.isoformat(), "open_slots": profile}


def profile_calendar(base: TradingCalendar, profile: dict) -> TradingCalendar:
    """Restrict `base` sessions to slots the broker was observed trading (intersection; never widens)."""
    from trading.sessions import SessionWindow
    slot = profile["slot_minutes"]
    windows = []
    for weekday, minutes in sorted(profile["open_slots"].items()):
        allowed = [m for m in minutes if any(w.weekday == weekday and w.start_minute <= m < w.end_minute
                                             for w in base.windows)]
        start = prev = None
        for m in allowed + [None]:
            if m is not None and prev is not None and m == prev + slot:
                prev = m
                continue
            if start is not None:
                windows.append(SessionWindow(weekday, start, prev + slot))
            start = prev = m
    calendar = replace(base, windows=tuple(windows), verification="broker_observed")
    calendar.validate()
    return calendar


def compare_profile_with_calendar(profile_cal: TradingCalendar, base: TradingCalendar) -> list[str]:
    """Human-readable session differences (e.g. a broker break longer than the market's)."""
    diffs = []
    names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    for weekday in range(7):
        b = sorted((w.start_minute, w.end_minute) for w in base.windows if w.weekday == weekday)
        p = sorted((w.start_minute, w.end_minute) for w in profile_cal.windows if w.weekday == weekday)
        if b != p:
            fmt = lambda ws: ", ".join(f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}" for s, e in ws)  # noqa
            diffs.append(f"{names[weekday]}: market {fmt(b) or 'closed'} | broker observed {fmt(p) or 'closed'}")
    return diffs
