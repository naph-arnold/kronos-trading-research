"""Parsers/fetchers for official, freely published calendar sources (no API keys).

* BEA release schedule (GDP, Personal Income and Outlays incl. PCE inflation):
  https://www.bea.gov/news/schedule — US government work, public domain. Rows give
  month/day and an Eastern-time release time; the year is inferred from the retrieval
  date (the page lists the coming weeks only).
* ECB Governing Council meeting calendar:
  https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html — monetary-policy
  decisions are published at 14:15 Frankfurt time on the (final) meeting day.
* BLS Public Data API (values only, not schedules): https://api.bls.gov/publicAPI/v2/
  — used to fill `previous`/`actual` for CPI and payroll events whose schedule comes
  from another provider. The BLS website's release-schedule pages refuse automated
  requests (HTTP 403); we do not attempt to bypass that.

Parsers fail closed (raise) when the page layout yields nothing recognisable.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date, datetime, timedelta
from html import unescape
from typing import Callable

from trading.clock import NEW_YORK, ServerTimeZone
from trading.common import ValidationError
from trading.marketcal.events import EconomicEvent

USER_AGENT = "kronos-trading-research/0.2 (research calendar)"
BEA_URL = "https://www.bea.gov/news/schedule"
BEA_PROVIDER = "bea.gov/news/schedule"
ECB_URL = "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html"
ECB_PROVIDER = "ecb.europa.eu/mgcgc"
BLS_API = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
BLS_PROVIDER = "api.bls.gov"
FRANKFURT = ServerTimeZone("europe_frankfurt", timedelta(hours=1), timedelta(hours=2), "eu")
MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"), start=1)}
_TAGS = re.compile(r"<[^>]+>")
_BEA_ROW = re.compile(r'release-date">([^<]+)</div>\s*<small class="text-muted">([^<]+)</small>.*?'
                      r'release-title[^>]*>(.*?)</td>', re.S)
_ECB_ROW = re.compile(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", re.S)

# BLS series used to enrich events: seasonally adjusted CPI-U all items; total nonfarm payrolls.
BLS_SERIES = {"US_CPI": "CUSR0000SA0", "US_NFP": "CES0000000001"}


def _text(fragment: str) -> str:
    return " ".join(unescape(_TAGS.sub(" ", fragment)).split())


def _http_get_text(url: str) -> str:
    from urllib.request import Request, urlopen
    with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def _infer_year(month: int, day: int, retrieved: datetime) -> date:
    """The schedule lists upcoming releases: pick the year putting the date in [retrieved-31d, retrieved+335d]."""
    for year in (retrieved.year, retrieved.year + 1, retrieved.year - 1):
        candidate = date(year, month, day)
        if retrieved.date() - timedelta(days=31) <= candidate <= retrieved.date() + timedelta(days=335):
            return candidate
    raise ValidationError(f"Cannot infer year for {month}/{day}")


def _parse_et_time(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*([AP])M\s*", text, re.I)
    if not match:
        raise ValidationError(f"Unparseable release time {text!r}")
    hour, minute = int(match.group(1)) % 12, int(match.group(2))
    return hour + (12 if match.group(3).upper() == "P" else 0), minute


def _bea_series(title: str) -> tuple[str, str, str] | None:
    lowered = title.lower()
    if lowered.startswith("gdp ("):  # headline Advance/Second/Third Estimate only (not "GDP by County")
        return "US_GDP", "US GDP release", "growth"
    if lowered.startswith("personal income and outlays"):
        return "US_PCE", "US Personal Income and Outlays (PCE inflation)", "inflation"
    return None


def parse_bea_schedule(html: str, *, retrieved_at: datetime) -> list[EconomicEvent]:
    rows = _BEA_ROW.findall(html)
    if not rows:
        raise ValidationError("No BEA schedule rows found; page layout may have changed (fail closed)")
    events = []
    for date_text, time_text, title_html in rows:
        title = _text(title_html)
        series = _bea_series(title)
        if series is None:
            continue
        month_name, day_text = date_text.strip().split()
        day = _infer_year(MONTHS[month_name.lower()], int(day_text), retrieved_at)
        hour, minute = _parse_et_time(time_text)
        scheduled = NEW_YORK.to_utc(datetime(day.year, day.month, day.day, hour, minute))
        period = title.rsplit(",", 1)[-1].strip() if "," in title else None
        key, name, category = series
        events.append(EconomicEvent(
            event_id=f"bea-{key.lower()}-{day.isoformat()}", name=f"{name}: {title}"[:200], country="US",
            currency="USD", scheduled_at=scheduled, impact="high", provider=BEA_PROVIDER, retrieved_at=retrieved_at,
            updated_at=retrieved_at, category=category, series=key, reference_period=period, source_url=BEA_URL))
    for event in events:
        event.validate()
    return events


def parse_ecb_calendar(html: str, *, retrieved_at: datetime) -> list[EconomicEvent]:
    rows = _ECB_ROW.findall(html)
    if not rows:
        raise ValidationError("No ECB calendar rows found; page layout may have changed (fail closed)")
    events = []
    for date_html, text_html in rows:
        text = _text(text_html).lower()
        if "monetary policy meeting" not in text or "non-monetary" in text or "day 1" in text:
            continue
        day = datetime.strptime(_text(date_html), "%d/%m/%Y").date()
        scheduled = FRANKFURT.to_utc(datetime(day.year, day.month, day.day, 14, 15))
        events.append(EconomicEvent(
            event_id=f"ecb-decision-{day.isoformat()}", name="ECB monetary policy decision", country="EA",
            currency="EUR", scheduled_at=scheduled, impact="high", provider=ECB_PROVIDER, retrieved_at=retrieved_at,
            updated_at=retrieved_at, category="central_bank", series="ECB_DECISION", source_url=ECB_URL))
    if not events:
        raise ValidationError("ECB calendar contained no monetary-policy meetings (fail closed)")
    return events


def fetch_bea(http_get: Callable[[str], str] = _http_get_text, now: Callable[[], datetime] | None = None):
    from datetime import timezone
    retrieved = (now or (lambda: datetime.now(timezone.utc)))()
    return parse_bea_schedule(http_get(BEA_URL), retrieved_at=retrieved)


def fetch_ecb(http_get: Callable[[str], str] = _http_get_text, now: Callable[[], datetime] | None = None):
    from datetime import timezone
    retrieved = (now or (lambda: datetime.now(timezone.utc)))()
    return parse_ecb_calendar(http_get(ECB_URL), retrieved_at=retrieved)


def parse_bls_observations(payload: dict) -> dict[str, list[tuple[str, str]]]:
    """{seriesID: [(period 'YYYY-MM', value), ... newest first]} from a BLS API v2 response."""
    if payload.get("status") != "REQUEST_SUCCEEDED":
        raise ValidationError(f"BLS API request failed: {payload.get('message')}")
    out = {}
    for series in payload["Results"]["series"]:
        rows = [(f"{r['year']}-{r['period'][1:]}", r["value"]) for r in series["data"]
                if r["period"].startswith("M") and r["period"] != "M13"]
        out[series["seriesID"]] = rows
    return out


def fetch_bls(series_ids: list[str], http_get: Callable[[str], str] = _http_get_text) -> dict:
    return {sid: parse_bls_observations(json.loads(http_get(BLS_API + sid)))[sid] for sid in series_ids}


def enrich_with_bls(events: list[EconomicEvent], observations: dict[str, list[tuple[str, str]]], *,
                    retrieved_at: datetime) -> list[EconomicEvent]:
    """Fill `actual`/`previous` for US_CPI/US_NFP events whose `reference_period` (YYYY-MM) is published.
    Values come only from BLS; nothing is estimated. Events without data are returned unchanged."""
    enriched = []
    for event in events:
        series_id = BLS_SERIES.get(event.series or "")
        rows = observations.get(series_id or "", [])
        lookup = dict(rows)
        if series_id and event.reference_period in lookup:
            periods = [p for p, _ in rows]
            idx = periods.index(event.reference_period)
            previous = rows[idx + 1][1] if idx + 1 < len(rows) else event.previous
            event = replace(event, actual=lookup[event.reference_period], previous=previous, status="released",
                            updated_at=retrieved_at)
        enriched.append(event)
    return enriched
