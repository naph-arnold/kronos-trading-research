"""Official FOMC meeting calendar (Board of Governors of the Federal Reserve System).

Source: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (US
government work, public domain). Each scheduled meeting yields a high-impact
"FOMC rate decision" event at 14:00 New York time on the meeting's final day,
the Fed's standard statement release time. Future-year dates the Fed labels
tentative must be re-fetched; the parser records what the page shows.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from html import unescape
from typing import Callable

from trading.clock import NEW_YORK
from trading.common import ValidationError
from trading.marketcal.events import EconomicEvent

SOURCE_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
PROVIDER = "federalreserve.gov/fomccalendars"
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"), start=1)}  # page uses full names and "Apr/May"-style abbreviations
_PANEL = re.compile(r"<h4>\s*<a[^>]*>\s*(\d{4}) FOMC Meetings\s*</a>\s*</h4>(.*?)(?=<h4>\s*<a[^>]*>\s*\d{4} FOMC"
                    r" Meetings|\Z)", re.S)
_MEETING = re.compile(r'fomc-meeting__month[^"]*"[^>]*>(.*?)</div>\s*<div class="fomc-meeting__date[^"]*"[^>]*>'
                      r"(.*?)</div>", re.S)
_TAGS = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return " ".join(unescape(_TAGS.sub(" ", fragment)).split())


def _last_day(year: int, month_text: str, date_text: str) -> date:
    months = [m.strip().lower()[:3] for m in month_text.split("/")]
    days = re.findall(r"\d{1,2}", date_text)
    if not months or not days or any(m not in MONTHS for m in months):
        raise ValidationError(f"Unparseable FOMC date: {month_text!r} {date_text!r}")
    return date(year, MONTHS[months[-1]], int(days[-1]))


def statement_time_utc(day: date) -> datetime:
    """14:00 New York time on `day`, in UTC (DST-aware)."""
    return NEW_YORK.to_utc(datetime(day.year, day.month, day.day, 14, 0))


def parse_fomc_calendar(html: str, *, retrieved_at: datetime) -> list[EconomicEvent]:
    events = []
    for year_text, panel in _PANEL.findall(html):
        year = int(year_text)
        for month_html, date_html in _MEETING.findall(panel):
            month_text, date_text = _text(month_html), _text(date_html)
            if "notation vote" in date_text.lower():
                continue  # not a market-moving scheduled release
            unscheduled = "unscheduled" in date_text.lower()
            day = _last_day(year, month_text, date_text)
            sep = "*" in date_text
            events.append(EconomicEvent(
                event_id=f"fomc-{day.isoformat()}", name="FOMC rate decision and statement"
                + (" (with Summary of Economic Projections)" if sep else ""),
                country="US", currency="USD", scheduled_at=statement_time_utc(day), impact="high",
                provider=PROVIDER, retrieved_at=retrieved_at, updated_at=retrieved_at, category="central_bank",
                status="tentative" if unscheduled else "scheduled", all_day=unscheduled, source_url=SOURCE_URL,
                series="FOMC_DECISION"))
    if not events:
        raise ValidationError("No FOMC meetings found; page layout may have changed (fail closed)")
    for event in events:
        event.validate()
    return events


def fetch_fomc_calendar(http_get: Callable[[str], str] | None = None) -> list[EconomicEvent]:
    if http_get is None:
        from urllib.request import Request, urlopen

        def http_get(url: str) -> str:
            request = Request(url, headers={"User-Agent": "kronos-trading-research/0.2 (research calendar)"})
            with urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", errors="replace")
    retrieved = datetime.now(timezone.utc)
    return parse_fomc_calendar(http_get(SOURCE_URL), retrieved_at=retrieved)


