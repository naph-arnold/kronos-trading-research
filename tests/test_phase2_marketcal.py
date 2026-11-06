"""Market-calendar intelligence: official FOMC parsing, event store, blackouts, holidays."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from trading.common import Market, ValidationError
from trading.marketcal.blackout import BlackoutRule, EventBlackout
from trading.marketcal.events import EconomicEvent, EventStore, load_events_file
from trading.marketcal.fomc import PROVIDER, parse_fomc_calendar, statement_time_utc
from trading.marketcal.holidays import (BrokerClosure, build_xauusd_calendar, cme_gold_closures, compare_with_bars,
                                        load_broker_closures)
from trading.risk import evaluate
from trading.sessions import XAUUSD_NY_CLOSE_TEMPLATE
from trading_helpers import ENV, NOW, XAU_CALENDAR, account, breaker, limits, xau_quote, xau_signal, xau_spec

UTC = timezone.utc
FIXTURE = Path(__file__).parent / "data" / "fomc_calendar_2026.html"
RETRIEVED = datetime(2026, 11, 6, 12, tzinfo=UTC)


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def event(name="US CPI", at=utc(2026, 11, 12, 13, 30), impact="high", currency="USD", **kw):
    values = dict(event_id=kw.pop("event_id", "cpi-2026-11"), name=name, country="US", currency=currency,
                  scheduled_at=at, impact=impact, provider="test-provider", retrieved_at=RETRIEVED,
                  updated_at=RETRIEVED)
    values.update(kw)
    return EconomicEvent(**values)


# --- FOMC (official source) --------------------------------------------------------------

def test_parse_official_2026_fomc_calendar_fixture():
    events = parse_fomc_calendar(FIXTURE.read_text(encoding="utf-8"), retrieved_at=RETRIEVED)
    days = [e.event_id for e in events]
    assert days == ["fomc-2026-01-28", "fomc-2026-03-18", "fomc-2026-04-29", "fomc-2026-06-17",
                    "fomc-2026-07-29", "fomc-2026-09-16", "fomc-2026-10-28", "fomc-2026-12-09"]
    assert all(e.impact == "high" and e.currency == "USD" and e.provider == PROVIDER for e in events)
    assert "Summary of Economic Projections" in events[1].name and "Summary" not in events[0].name


def test_statement_time_is_14h_new_york_dst_aware():
    assert statement_time_utc(date(2026, 1, 28)) == utc(2026, 1, 28, 19)   # EST
    assert statement_time_utc(date(2026, 6, 17)) == utc(2026, 6, 17, 18)   # EDT


def test_cross_month_and_notation_vote_rows():
    html = ('<h4><a id="1">2027 FOMC Meetings</a></h4>'
            '<div class="fomc-meeting__month x"><strong>Apr/May</strong></div>'
            '<div class="fomc-meeting__date x">30-1</div>'
            '<div class="fomc-meeting__month x"><strong>May</strong></div>'
            '<div class="fomc-meeting__date x">22 (notation vote)</div>')
    events = parse_fomc_calendar(html, retrieved_at=RETRIEVED)
    assert [e.event_id for e in events] == ["fomc-2027-05-01"]


def test_layout_change_fails_closed():
    with pytest.raises(ValidationError):
        parse_fomc_calendar("<html>redesigned page</html>", retrieved_at=RETRIEVED)


# --- Event store -------------------------------------------------------------------------------

def test_store_upserts_tracks_revisions_and_provider_ownership():
    store = EventStore()
    assert store.upsert([event()]) == {"received": 1, "changed": 1}
    assert store.upsert([event()])["changed"] == 0
    released = event(actual="3.1", forecast="3.0", previous="2.9", status="released",
                     updated_at=RETRIEVED + timedelta(hours=1))
    store.upsert([released])
    assert [r["actual"] for r in store.revisions("cpi-2026-11")] == [None, "3.1"]
    assert store.between(utc(2026, 11, 12), utc(2026, 11, 13))[0].actual == "3.1"
    with pytest.raises(ValidationError):
        store.upsert([event(provider="other")])


def test_event_validation():
    for bad in (dict(impact="huge"), dict(currency="US"), dict(at=datetime(2026, 11, 12, 13, 30))):
        with pytest.raises(ValidationError):
            store = EventStore()
            store.upsert([event(**bad)])


def test_load_events_file_requires_matching_provider(tmp_path):
    path = tmp_path / "events.json"
    path.write_text(json.dumps([event().to_json()]), encoding="utf-8")
    assert load_events_file(path, provider="test-provider")[0].name == "US CPI"
    with pytest.raises(ValidationError):
        load_events_file(path, provider="someone-else")


# --- Blackouts ------------------------------------------------------------------------------------

def test_blackout_window_rules_and_currency_filter():
    store = EventStore()
    store.upsert([event(), event(event_id="ecb", name="ECB decision", currency="EUR",
                                 at=utc(2026, 11, 12, 13, 15))])
    guard = EventBlackout(store=store, market=Market.XAUUSD_MT5, max_calendar_age=None)
    assert "US CPI" in guard(utc(2026, 11, 12, 13, 0))
    assert guard(utc(2026, 11, 12, 12, 59)) is None
    assert "US CPI" in guard(utc(2026, 11, 12, 14, 0))
    assert guard(utc(2026, 11, 12, 14, 1)) is None  # EUR event ignored by USD rule


def test_central_bank_events_get_wider_window_for_gold():
    store = EventStore()
    store.upsert(parse_fomc_calendar(FIXTURE.read_text(encoding="utf-8"), retrieved_at=RETRIEVED))
    guard = EventBlackout(store=store, market=Market.XAUUSD_MT5, max_calendar_age=None)
    decision = utc(2026, 12, 9, 19)
    assert guard(decision - timedelta(minutes=55)) and guard(decision + timedelta(minutes=85))
    assert guard(decision + timedelta(minutes=95)) is None


def test_stale_or_missing_calendar_fails_closed_and_all_day_events():
    store = EventStore()
    guard = EventBlackout(store=store, market=Market.BTCUSDT_BINANCE_SPOT, required_providers=(PROVIDER,),
                          max_calendar_age=timedelta(days=7))
    assert "missing or stale" in guard(NOW)
    store.record_refresh(PROVIDER, NOW - timedelta(days=1), 8)
    assert guard(NOW) is None
    store.record_refresh(PROVIDER, NOW - timedelta(days=8), 8)
    assert "stale" in guard(NOW)
    store.upsert([event(event_id="unsched", status="tentative", all_day=True, at=utc(2026, 11, 6))])
    all_day = EventBlackout(store=store, market=Market.BTCUSDT_BINANCE_SPOT, max_calendar_age=None,
                            rules=(BlackoutRule(),))
    assert "time unconfirmed" in all_day(NOW)


def test_risk_engine_rejects_during_event_blackout():
    store = EventStore()
    store.upsert([event(at=NOW + timedelta(minutes=10))])
    guard = EventBlackout(store=store, market=Market.XAUUSD_MT5, max_calendar_age=None)
    decision = evaluate(xau_signal(), account=account(Market.XAUUSD_MT5), instrument=xau_spec(), quote=xau_quote(),
                        limits=limits(Market.XAUUSD_MT5), environment=ENV, breaker=breaker(Market.XAUUSD_MT5),
                        now=NOW, session_calendar=XAU_CALENDAR, event_guard=guard)
    assert not decision.approved and any("Event blackout" in r for r in decision.reasons)


def test_guard_errors_fail_closed():
    def broken(now):
        raise RuntimeError("calendar db locked")
    decision = evaluate(xau_signal(), account=account(Market.XAUUSD_MT5), instrument=xau_spec(), quote=xau_quote(),
                        limits=limits(Market.XAUUSD_MT5), environment=ENV, breaker=breaker(Market.XAUUSD_MT5),
                        now=NOW, session_calendar=XAU_CALENDAR, event_guard=broken)
    assert not decision.approved and decision.size == D("0")


# --- Holidays / broker closures --------------------------------------------------------------------

def test_cme_gold_christmas_holiday_and_early_close():
    result = cme_gold_closures(date(2026, 12, 21), date(2026, 12, 31))
    assert "2026-12-25" in result["holidays"]
    assert any(day == "2026-12-24" for day, _ in result["early_closes"])
    calendar, provenance = build_xauusd_calendar(XAUUSD_NY_CLOSE_TEMPLATE, date(2026, 12, 21), date(2026, 12, 31))
    assert not calendar.is_open(utc(2026, 12, 25, 12))
    assert calendar.is_open(utc(2026, 12, 23, 12)) and not calendar.is_open(utc(2026, 12, 24, 20))
    assert XAUUSD_NY_CLOSE_TEMPLATE.is_open(utc(2026, 12, 25, 12))  # template alone would be wrong
    assert provenance["provider"] == "pandas_market_calendars" and provenance["licence"] == "MIT"


def test_broker_closures_union_and_validation(tmp_path):
    suspension = BrokerClosure(utc(2026, 12, 22, 10), utc(2026, 12, 22, 11), "suspension", "broker email",
                               "msg-123", utc(2026, 12, 20))
    calendar, provenance = build_xauusd_calendar(XAUUSD_NY_CLOSE_TEMPLATE, date(2026, 12, 21), date(2026, 12, 31),
                                                 [suspension])
    assert not calendar.is_open(utc(2026, 12, 22, 10, 30))
    assert provenance["broker_closures"][0]["reference"] == "msg-123"
    path = tmp_path / "closures.json"
    path.write_text(json.dumps([{"start": "2026-12-22T10:00:00Z", "end": "2026-12-22T11:00:00Z",
                                 "kind": "suspension", "source": "", "reference": "x",
                                 "recorded_at": "2026-12-20T00:00:00Z"}]), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_broker_closures(path)


def test_compare_with_bars_reports_broker_disagreement():
    from phase2_helpers import xau_m15
    bars = xau_m15(200)
    assert compare_with_bars(bars, XAUUSD_NY_CLOSE_TEMPLATE)["consistent"]
    holiday = bars[50].open_time
    cal = XAUUSD_NY_CLOSE_TEMPLATE.__class__(**{**XAUUSD_NY_CLOSE_TEMPLATE.__dict__,
                                                "closures": ((holiday, holiday + timedelta(hours=1)),)})
    report = compare_with_bars(bars, cal)
    assert not report["consistent"] and report["bars_in_closed_time"][0] == holiday.isoformat()
