"""MetaTrader 5 server-time handling and UTC normalization.

MT5 reports bar/tick times as *server wall-clock* time encoded as Unix seconds
(i.e. as if the server's local time were UTC). The offset is broker-specific;
most XAUUSD brokers use the "New York close" convention: UTC+2 in winter and
UTC+3 while US daylight saving time is in effect, so server midnight equals
17:00 New York, the daily gold break.

The offset must be *configured* (never guessed) and then *verified* against a
fresh tick before use (`verify_server_offset`). Ambiguous or non-existent
server times around DST switches are rejected rather than guessed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from trading.common import ValidationError, require_utc

_EPOCH_NAIVE = datetime(1970, 1, 1)


class ServerClockError(ValidationError):
    """Server time cannot be converted or does not match the configured offset."""


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
    return first_sunday + timedelta(weeks=n - 1)


def us_dst_active(moment: datetime) -> bool:
    """US DST (since 2007): 2nd Sunday of March 07:00 UTC to 1st Sunday of November 06:00 UTC."""
    require_utc(moment, "moment")
    start = datetime.combine(_nth_sunday(moment.year, 3, 2), datetime.min.time(), timezone.utc) + timedelta(hours=7)
    end = datetime.combine(_nth_sunday(moment.year, 11, 1), datetime.min.time(), timezone.utc) + timedelta(hours=6)
    return start <= moment < end


def _last_sunday(year: int, month: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - 6) % 7)


def eu_dst_active(moment: datetime) -> bool:
    """EU/UK summer time: last Sunday of March 01:00 UTC to last Sunday of October 01:00 UTC."""
    require_utc(moment, "moment")
    start = datetime.combine(_last_sunday(moment.year, 3), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    end = datetime.combine(_last_sunday(moment.year, 10), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    return start <= moment < end


_DST_RULES = {"us": us_dst_active, "eu": eu_dst_active}


@dataclass(frozen=True)
class ServerTimeZone:
    name: str
    standard_offset: timedelta
    dst_offset: timedelta | None = None  # None: fixed offset
    dst_rule: str | None = None  # "us" or "eu"

    def __post_init__(self):
        if (self.dst_offset is None) != (self.dst_rule is None):
            raise ValidationError("dst_offset and dst_rule must be given together")
        if self.dst_rule not in (None, *_DST_RULES):
            raise ValidationError(f"Unsupported DST rule {self.dst_rule!r}")
        for offset in (self.standard_offset, self.dst_offset):
            if offset is not None and (abs(offset) > timedelta(hours=14) or offset % timedelta(minutes=15)):
                raise ValidationError("Server UTC offsets must be multiples of 15 minutes within ±14h")

    def offset_at(self, moment: datetime) -> timedelta:
        if self.dst_rule is not None and _DST_RULES[self.dst_rule](moment):
            return self.dst_offset
        require_utc(moment, "moment")
        return self.standard_offset

    def to_server(self, moment: datetime) -> datetime:
        """UTC -> naive server wall-clock time."""
        return (moment + self.offset_at(moment)).replace(tzinfo=None)

    def to_utc(self, server_time: datetime) -> datetime:
        """Naive server wall-clock time -> UTC. Rejects ambiguous/non-existent times."""
        if not isinstance(server_time, datetime) or server_time.tzinfo is not None:
            raise ServerClockError("Server time must be a naive wall-clock datetime")
        offsets = {self.standard_offset} | ({self.dst_offset} if self.dst_offset is not None else set())
        matches = []
        for offset in offsets:
            candidate = server_time.replace(tzinfo=timezone.utc) - offset
            if self.offset_at(candidate) == offset:
                matches.append(candidate)
        if len(matches) != 1:
            raise ServerClockError(f"Server time {server_time.isoformat()} is ambiguous or does not exist "
                                   f"in {self.name}")
        return matches[0]

    def server_epoch_to_utc(self, server_seconds: int | float) -> datetime:
        """Convert an MT5 `time` field (server wall time as epoch seconds) to UTC."""
        if isinstance(server_seconds, bool) or not isinstance(server_seconds, (int, float)):
            raise ServerClockError("MT5 time must be numeric seconds")
        return self.to_utc(_EPOCH_NAIVE + timedelta(seconds=server_seconds))

    def server_msc_to_utc(self, server_milliseconds: int) -> datetime:
        """Convert an MT5 `time_msc` field to UTC."""
        if isinstance(server_milliseconds, bool) or not isinstance(server_milliseconds, int):
            raise ServerClockError("MT5 time_msc must be integer milliseconds")
        return self.to_utc(_EPOCH_NAIVE + timedelta(milliseconds=server_milliseconds))


def fixed_offset(hours: float, name: str | None = None) -> ServerTimeZone:
    offset = timedelta(hours=hours)
    return ServerTimeZone(name or f"UTC{hours:+g}", offset)


LONDON = ServerTimeZone("europe_london", timedelta(0), timedelta(hours=1), "eu")
NEW_YORK = ServerTimeZone("america_new_york", timedelta(hours=-5), timedelta(hours=-4), "us")

# Typical XAUUSD broker convention. Must be confirmed per broker before use.
NY_CLOSE = ServerTimeZone("ny_close_utc+2/+3", timedelta(hours=2), timedelta(hours=3), "us")


def verify_server_offset(tz: ServerTimeZone, *, tick_server_msc: int, utc_now: datetime,
                         max_tick_age: timedelta = timedelta(seconds=60)) -> timedelta:
    """Check a fresh MT5 tick against the configured offset.

    observed = tick server wall time - real UTC now = configured offset - tick age.
    Fails closed if the tick is stale/future or the offsets disagree.
    """
    require_utc(utc_now, "utc_now")
    if isinstance(tick_server_msc, bool) or not isinstance(tick_server_msc, int):
        raise ServerClockError("tick time_msc must be integer milliseconds")
    server_wall = _EPOCH_NAIVE + timedelta(milliseconds=tick_server_msc)
    expected = tz.offset_at(utc_now)
    lag = (utc_now.replace(tzinfo=None) + expected) - server_wall
    if lag < -timedelta(seconds=2):
        raise ServerClockError(f"Server clock ahead of configured offset by {-lag}")
    if lag > max_tick_age:
        raise ServerClockError(f"Tick too old ({lag}) or server offset differs from configured {expected}")
    return expected
