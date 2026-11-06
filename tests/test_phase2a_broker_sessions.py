"""Broker server-time zones, NY market-hours calendar, observed broker sessions (SYNTHETIC bars)."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from phase2_helpers import random_walk
from trading.clock import EET_EU, NEW_YORK, NY_CLOSE
from trading.common import ValidationError
from trading.daily import DayBoundary
from trading.marketcal.holidays import (broker_xauusd_calendar, compare_with_bars, mark_broker_verified,
                                        observed_session_profile, profile_calendar)
from trading.sessions import XAUUSD_NY_MARKET_HOURS, SessionWindow, TradingCalendar

UTC = timezone.utc


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def test_eet_eu_differs_from_ny_close_only_in_dst_mismatch_weeks():
    # 2026-03-16: US already on DST (since Mar 8), EU not until Mar 29.
    assert EET_EU.to_server(utc(2026, 3, 16, 12)).hour == 14 and NY_CLOSE.to_server(utc(2026, 3, 16, 12)).hour == 15
    # Outside mismatch weeks they agree.
    for moment in (utc(2026, 1, 15, 12), utc(2026, 7, 15, 12), utc(2026, 11, 6, 12)):
        assert EET_EU.to_server(moment) == NY_CLOSE.to_server(moment)
    # 2026-10-26..30: EU off DST (Oct 25), US not until Nov 1.
    assert EET_EU.to_server(utc(2026, 10, 27, 12)).hour == 14 and NY_CLOSE.to_server(utc(2026, 10, 27, 12)).hour == 15


@pytest.mark.parametrize("moment,is_open", [
    (utc(2026, 3, 16, 21, 30), False),  # 17:30 NY (EDT) daily break, even in the mismatch week
    (utc(2026, 3, 16, 22, 30), True),
    (utc(2026, 1, 13, 22, 30), False),  # 17:30 NY (EST)
    (utc(2026, 7, 3, 20, 59), True),    # Friday 16:59 NY
    (utc(2026, 7, 3, 21, 0), False),    # Friday 17:00 NY close
    (utc(2026, 7, 5, 22, 0), True),     # Sunday 18:00 NY open
    (utc(2026, 7, 5, 21, 59), False),
])
def test_ny_market_hours_calendar(moment, is_open):
    assert XAUUSD_NY_MARKET_HOURS.is_open(moment) is is_open


def test_risk_day_rolls_at_17_new_york():
    boundary = DayBoundary.server(NEW_YORK, XAUUSD_NY_MARKET_HOURS.risk_day_shift)
    summer_close = utc(2026, 7, 7, 21)  # 17:00 EDT
    assert boundary.trading_day(summer_close - timedelta(minutes=1)) != boundary.trading_day(summer_close)
    winter_close = utc(2026, 12, 8, 22)  # 17:00 EST
    assert boundary.trading_day(winter_close - timedelta(minutes=1)) != boundary.trading_day(winter_close)


def test_constraints_intersect_and_never_widen():
    broker = TradingCalendar(symbol="XAUUSD", server_tz=EET_EU,
                             windows=tuple(SessionWindow(d, 60, 23 * 60) for d in range(5)))
    combined = replace(XAUUSD_NY_MARKET_HOURS, constraints=(broker,))
    t = utc(2026, 7, 7, 20, 30)  # 16:30 NY: market open, broker (server 23:30) closed
    assert XAUUSD_NY_MARKET_HOURS.is_open(t) and not combined.is_open(t)
    assert "broker/constraint calendar" in combined.tradeable_reason(t)
    assert combined.tradeable_reason(utc(2026, 7, 7, 14)) is None
    with pytest.raises(ValidationError):
        replace(XAUUSD_NY_MARKET_HOURS, constraints=(replace(broker, symbol="BTCUSDT"),)).validate()


def _broker_bars(weeks=10):
    """SYNTHETIC broker: NY market hours but closes at server 23:00 EET (the 2026 MetaQuotes-Demo pattern)."""
    broker = TradingCalendar(symbol="XAUUSD", server_tz=EET_EU,
                             windows=tuple(SessionWindow(d, 60, 23 * 60) for d in range(5)))
    cal = replace(XAUUSD_NY_MARKET_HOURS, constraints=(broker,))
    return random_walk("XAUUSD", "15m", utc(2026, 8, 30, 22), 92 * 5 * weeks, price=4000.0, vol=0.001,
                       calendar=cal)


def test_observed_profile_detects_shortened_broker_session():
    bars = _broker_bars()
    broad = TradingCalendar(symbol="XAUUSD", server_tz=EET_EU, windows=tuple(SessionWindow(d, 0, 1440)
                                                                               for d in range(7)))
    profile = observed_session_profile(bars, broad, weeks=8)
    cal = profile_calendar(broad, profile)
    tuesday = [(w.start_minute, w.end_minute) for w in cal.windows if w.weekday == 1]
    assert tuesday == [(60, 23 * 60)] and cal.verification == "broker_observed"


def test_broker_calendar_verification_allows_stricter_but_not_permissive():
    bars = _broker_bars()
    cal, prov = broker_xauusd_calendar(bars, server_tz=EET_EU, start=bars[0].open_time.date(),
                                       end=bars[-1].close_time.date() + timedelta(1), weeks=8)
    recent = [b for b in bars if b.open_time >= bars[-1].open_time - timedelta(weeks=8)]
    assert mark_broker_verified(cal, recent, offset_verified=True, min_bars=500).verification == "broker_verified"
    assert any("01:00-23:00" in d for d in prov["broker_profile"]["differences_vs_server_day"])
    # The plain market-hours calendar is too permissive for this broker -> cannot be verified.
    with pytest.raises(ValidationError, match="missing while calendar open"):
        mark_broker_verified(XAUUSD_NY_MARKET_HOURS, recent, offset_verified=True, min_bars=500)
    assert compare_with_bars(recent, XAUUSD_NY_MARKET_HOURS)["missing_in_open_time"]


def test_connector_history_is_chunked_and_deduplicated():
    import calendar as stdcal
    from test_phase2_connectors import FakeMt5, connect
    rows = [{"time": stdcal.timegm(datetime(2026, 7, 7, 10).timetuple()), "open": 1.0, "high": 1.0, "low": 1.0,
             "close": 1.0, "tick_volume": 1, "spread": 1}]
    fake = FakeMt5(rates=rows)
    conn, _ = connect(fake)
    conn.server_tz = EET_EU
    bars = conn.history("XAUUSD", "15m", utc(2026, 5, 1), utc(2026, 7, 8), chunk_days=30)
    assert len(bars) == 1 and len([c for c in fake.calls if c[0] == "copy_rates_range"]) == 3
