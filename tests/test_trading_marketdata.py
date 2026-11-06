"""Canonical bars, quotes, freshness and signal schema."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from trading.common import ValidationError, floor_to_step, to_decimal
from trading.marketdata import Bar, Quote, assert_fresh, bar_from_research_row, validate_series
from trading_helpers import BAR_CLOSE, xau_signal

T0 = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)


def bar(i: int, *, o="100", h="101", lo="99", c="100.5", open_time=None):
    return Bar.create(symbol="XAUUSD", interval="1h", open_time=open_time or T0 + timedelta(hours=i),
                      open=o, high=h, low=lo, close=c, volume="10")


def test_valid_series_and_close_time():
    bars = [bar(i) for i in range(3)]
    assert validate_series(bars) == []
    assert bars[0].close_time == T0 + timedelta(hours=1)


@pytest.mark.parametrize("kwargs", [
    {"h": "99"}, {"lo": "101"}, {"o": "0"}, {"c": "NaN"}, {"o": "-1"},
    {"open_time": T0 + timedelta(minutes=30)},
    {"open_time": datetime(2026, 11, 6, 10)},
    {"open_time": datetime(2026, 11, 6, 12, tzinfo=timezone(timedelta(hours=2)))},
])
def test_invalid_bars_rejected(kwargs):
    with pytest.raises(ValidationError):
        bar(0, **kwargs)


def test_duplicates_out_of_order_and_gaps():
    with pytest.raises(ValidationError):
        validate_series([bar(0), bar(0)])
    with pytest.raises(ValidationError):
        validate_series([bar(1), bar(0)])
    with pytest.raises(ValidationError):
        validate_series([bar(0), bar(2)])
    assert validate_series([bar(0), bar(2)], allow_gaps=True) == [(T0 + timedelta(hours=1), T0 + timedelta(hours=2))]


def test_mixed_symbols_rejected():
    other = Bar.create(symbol="BTCUSDT", interval="1h", open_time=T0 + timedelta(hours=1),
                       open=1, high=1, low=1, close=1)
    with pytest.raises(ValidationError):
        validate_series([bar(0), other])


def test_feed_freshness():
    bars = [bar(0), bar(1)]
    closed = T0 + timedelta(hours=2)
    assert assert_fresh(bars, now=closed + timedelta(minutes=1), max_age=timedelta(minutes=5)) is bars[-1]
    with pytest.raises(ValidationError, match="Stale"):
        assert_fresh(bars, now=closed + timedelta(minutes=6), max_age=timedelta(minutes=5))
    with pytest.raises(ValidationError, match="not closed"):
        assert_fresh(bars, now=closed - timedelta(minutes=1), max_age=timedelta(minutes=5))


def test_quote_validation():
    q = Quote.create(symbol="XAUUSD", bid="2650.0", ask="2650.3", time=T0)
    assert q.spread == D("0.3")
    with pytest.raises(ValidationError):
        Quote.create(symbol="XAUUSD", bid="2651", ask="2650", time=T0)


def test_existing_research_rows_convert():
    row = {"timestamps": "2026-11-06T10:00:00+00:00", "open": "60000.1", "high": "60100", "low": "59900",
           "close": "60050", "volume": "12.5", "amount": "750000"}
    converted = bar_from_research_row(row, symbol="BTCUSDT", interval="1h")
    assert converted.open == D("60000.1") and converted.open_time == T0


def test_decimal_parsing_is_strict():
    assert to_decimal(0.1, "x") == D("0.1")
    for bad in (True, None, float("nan"), "abc", float("inf"), [1]):
        with pytest.raises(ValidationError):
            to_decimal(bad, "x")
    assert floor_to_step(D("0.0999"), D("0.01")) == D("0.09")


def test_signal_lookahead_and_symbol_checks():
    with pytest.raises(ValidationError, match="lookahead"):
        xau_signal(generated_at=BAR_CLOSE - timedelta(seconds=1))
    with pytest.raises(ValidationError):
        xau_signal(symbol="BTCUSDT")
    with pytest.raises(ValidationError):
        xau_signal(signal_id="bad id with spaces")
    with pytest.raises(ValidationError):
        xau_signal(take_profit="2640.00")


def test_dedup_key_ignores_signal_id_but_not_bar():
    a = xau_signal(signal_id="a")
    b = xau_signal(signal_id="b")
    c = xau_signal(signal_id="c", bar_close_time=BAR_CLOSE - timedelta(hours=1))
    assert a.dedup_key == b.dedup_key != c.dedup_key
    assert a.idempotency_key != b.idempotency_key and len(a.idempotency_key) == 32
