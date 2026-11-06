"""MT5 server-time normalization and XAUUSD session validation."""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone

import pytest

from trading.clock import (NY_CLOSE, ServerClockError, ServerTimeZone, fixed_offset, us_dst_active,
                           verify_server_offset)
from trading.common import ValidationError
from trading.sessions import BTCUSDT_ALWAYS_OPEN, XAUUSD_NY_CLOSE_TEMPLATE, TradingCalendar

UTC = timezone.utc
CAL = XAUUSD_NY_CLOSE_TEMPLATE


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def server_epoch(*args) -> int:
    """How MT5 encodes a server wall-clock time: as if it were UTC epoch seconds."""
    return calendar.timegm(datetime(*args).timetuple())


# --- Clock ----------------------------------------------------------------------

def test_us_dst_boundaries_2026():
    assert not us_dst_active(utc(2026, 3, 8, 6, 59))
    assert us_dst_active(utc(2026, 3, 8, 7, 0))
    assert us_dst_active(utc(2026, 11, 1, 5, 59))
    assert not us_dst_active(utc(2026, 11, 1, 6, 0))


def test_ny_close_offsets_and_round_trip():
    summer, winter = utc(2026, 7, 1, 12), utc(2026, 12, 1, 12)
    assert NY_CLOSE.to_server(summer) == datetime(2026, 7, 1, 15)
    assert NY_CLOSE.to_server(winter) == datetime(2026, 12, 1, 14)
    for moment in (summer, winter, utc(2026, 11, 6, 21, 59)):
        assert NY_CLOSE.to_utc(NY_CLOSE.to_server(moment)) == moment


def test_server_midnight_is_new_york_17h_in_both_seasons():
    # NY 17:00 = 21:00 UTC (EDT) / 22:00 UTC (EST)
    assert NY_CLOSE.to_server(utc(2026, 7, 1, 21)) == datetime(2026, 7, 2, 0)
    assert NY_CLOSE.to_server(utc(2026, 12, 1, 22)) == datetime(2026, 12, 2, 0)


def test_mt5_epoch_fields_convert_to_utc():
    assert NY_CLOSE.server_epoch_to_utc(server_epoch(2026, 11, 6, 16)) == utc(2026, 11, 6, 14)
    assert NY_CLOSE.server_msc_to_utc(server_epoch(2026, 7, 1, 15) * 1000 + 250) == utc(2026, 7, 1, 12, 0, 0, 250000)
    with pytest.raises(ServerClockError):
        NY_CLOSE.server_epoch_to_utc(True)


def test_dst_switch_ambiguous_and_missing_server_times_rejected():
    with pytest.raises(ServerClockError, match="ambiguous"):
        NY_CLOSE.to_utc(datetime(2026, 11, 1, 8, 30))  # repeated hour
    with pytest.raises(ServerClockError, match="ambiguous"):
        NY_CLOSE.to_utc(datetime(2026, 3, 8, 9, 30))  # skipped hour
    with pytest.raises(ServerClockError):
        NY_CLOSE.to_utc(utc(2026, 1, 1))  # aware input rejected


def test_invalid_timezone_configuration_rejected():
    with pytest.raises(ValidationError):
        ServerTimeZone("bad", timedelta(hours=2), timedelta(hours=3))  # DST offset without rule
    with pytest.raises(ValidationError):
        ServerTimeZone("bad", timedelta(minutes=7))
    with pytest.raises(ValidationError):
        ServerTimeZone("bad", timedelta(hours=2), timedelta(hours=3), "eu")


def test_verify_server_offset_with_fresh_tick():
    now = utc(2026, 11, 6, 14)
    tick = server_epoch(2026, 11, 6, 16) * 1000 - 500
    assert verify_server_offset(NY_CLOSE, tick_server_msc=tick, utc_now=now) == timedelta(hours=2)
    with pytest.raises(ServerClockError):  # broker actually on +2, configured +3
        verify_server_offset(fixed_offset(3), tick_server_msc=tick, utc_now=now)
    with pytest.raises(ServerClockError):  # broker actually on +3, configured NY_CLOSE (+2 in Nov)
        verify_server_offset(NY_CLOSE, tick_server_msc=server_epoch(2026, 11, 6, 17) * 1000, utc_now=now)
    with pytest.raises(ServerClockError):  # stale tick cannot verify
        verify_server_offset(NY_CLOSE, tick_server_msc=tick - 120_000, utc_now=now)


# --- Sessions ---------------------------------------------------------------------

@pytest.mark.parametrize("moment,is_open", [
    (utc(2026, 11, 2, 10), True),     # Monday
    (utc(2026, 11, 7, 12), False),    # Saturday
    (utc(2026, 11, 8, 22, 30), False),  # Sunday, server Mon 00:30
    (utc(2026, 11, 8, 23, 0), True),  # Sunday 18:00 NY re-open
    (utc(2026, 11, 4, 22, 30), False),  # daily break (winter)
    (utc(2026, 7, 7, 21, 30), False),   # daily break (summer, one hour earlier in UTC)
    (utc(2026, 7, 7, 22, 30), True),
    (utc(2026, 7, 3, 20, 59), True),    # Friday summer, closes 21:00 UTC
    (utc(2026, 7, 3, 21, 0), False),
])
def test_xauusd_session_open_closed(moment, is_open):
    assert CAL.is_open(moment) is is_open


def test_tradeable_respects_open_and_close_buffers():
    assert "of session open" in CAL.tradeable_reason(utc(2026, 11, 8, 23, 5))
    assert CAL.tradeable_reason(utc(2026, 11, 8, 23, 20)) is None
    assert "of session close" in CAL.tradeable_reason(utc(2026, 11, 4, 21, 50))
    assert CAL.tradeable_reason(utc(2026, 11, 4, 21, 40)) is None
    assert "session closed" in CAL.tradeable_reason(utc(2026, 11, 7, 12))


def test_holiday_and_early_close_closures():
    cal = TradingCalendar(symbol="XAUUSD", server_tz=NY_CLOSE, windows=CAL.windows,
                          closures=((utc(2026, 12, 24, 18), utc(2026, 12, 24, 22)),
                                    (utc(2026, 12, 24, 23), utc(2026, 12, 25, 23))))
    assert "scheduled closure" in cal.tradeable_reason(utc(2026, 12, 24, 19))
    assert "of session close" in cal.tradeable_reason(utc(2026, 12, 24, 17, 50))
    assert not cal.is_open(utc(2026, 12, 25, 12))
    assert cal.tradeable_reason(utc(2026, 12, 24, 17, 30)) is None


def test_invalid_calendar_rejected():
    with pytest.raises(ValidationError):
        TradingCalendar(symbol="XAUUSD", server_tz=NY_CLOSE, windows=()).validate()
    bad = TradingCalendar(symbol="XAUUSD", server_tz=NY_CLOSE, windows=CAL.windows,
                          closures=((utc(2026, 1, 2), utc(2026, 1, 1)),))
    with pytest.raises(ValidationError):
        bad.tradeable_reason(utc(2026, 1, 5, 10))


def test_crypto_calendar_always_tradeable():
    assert BTCUSDT_ALWAYS_OPEN.tradeable_reason(utc(2026, 11, 7, 12)) is None
