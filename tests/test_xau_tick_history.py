"""Tick-built XAUUSD M1 bars, historical FOMC parsing and the frozen 2019-2020 pre-registration.
All market data here is SYNTHETIC; no terminal is used."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from research.xau_tick_history import _chunk_bars, parse_fomc_historical
from trading.clock import EET_EU

TICK = np.dtype([("time", "<i8"), ("bid", "<f8"), ("ask", "<f8"), ("last", "<f8"), ("volume", "<u8"),
                 ("time_msc", "<i8"), ("flags", "<u4"), ("volume_real", "<f8")])


class FakeMt5:
    COPY_TICKS_INFO = 2

    def __init__(self, ticks):
        self.ticks, self.calls = ticks, []

    def copy_ticks_range(self, name, start, end, flags):
        self.calls.append((start, end))
        lo, hi = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        return self.ticks[(self.ticks["time_msc"] >= lo) & (self.ticks["time_msc"] < hi)]

    def last_error(self):
        return (1, "Success")


class FakeConn:
    server_tz = EET_EU

    def __init__(self, mt5):
        self.mt5 = mt5

    def _require(self):
        return self.mt5


def _ticks(rows):
    """rows: (utc datetime, bid, ask) -> structured array with server-time milliseconds."""
    arr = np.zeros(len(rows), dtype=TICK)
    for i, (t, bid, ask) in enumerate(rows):
        srv = EET_EU.to_server(t).replace(tzinfo=timezone.utc)
        arr[i]["time_msc"] = int(srv.timestamp() * 1000)
        arr[i]["bid"], arr[i]["ask"] = bid, ask
    return arr


def test_bars_are_bid_ohlc_in_utc_with_min_spread_and_bad_ticks_dropped():
    t0 = datetime(2019, 3, 5, 14, 0, tzinfo=timezone.utc)  # EET (UTC+2) in March before the EU switch
    rows = [(t0.replace(second=1), 1300.10, 1300.35), (t0.replace(second=20), 1300.50, 1300.70),
            (t0.replace(second=40), 1299.90, 1300.20), (t0.replace(second=59), 1300.00, 1300.27),
            (t0.replace(minute=1, second=5), 1300.40, 1300.30),  # ask < bid: dropped
            (t0.replace(minute=1, second=30), 0.0, 1300.30),      # zero bid: dropped
            (t0.replace(minute=2, second=0), 1301.00, 1301.22)]
    hist = np.zeros((24, 5000), dtype=np.int64)
    bars, n = _chunk_bars(FakeConn(FakeMt5(_ticks(rows))), "XAUUSD", t0, t0.replace(hour=15), hist)
    assert n == 5 and list(bars.index.strftime("%H:%M")) == ["14:00", "14:02"]
    first = bars.iloc[0]
    assert (first.open, first.high, first.low, first.close) == (1300.10, 1300.50, 1299.90, 1300.00)
    assert first.tick_volume == 4 and first.spread_points == 20  # min of 25, 20, 30, 27 points
    assert bars.index.tz is not None and str(bars.index.tz) == "UTC"
    assert hist.sum() == 5 and hist[9].sum() == 5  # 14:00 UTC = 09:00 New York (EST)


def test_summer_server_offset_is_three_hours():
    t0 = datetime(2019, 7, 9, 14, 0, tzinfo=timezone.utc)
    bars, _ = _chunk_bars(FakeConn(FakeMt5(_ticks([(t0.replace(second=3), 1400.0, 1400.2)]))), "XAUUSD", t0,
                          t0.replace(hour=15), None)
    assert bars.index[0] == t0
    assert bars.iloc[0].server_time == int(t0.timestamp()) + 3 * 3600


HTML = """
<h5 class="panel-heading panel-heading--shaded">January 28-29 Meeting - 2020</h5>
<h5 class="panel-heading panel-heading--shaded">March 2 (unscheduled) Meeting - 2020</h5>
<h5 class="panel-heading panel-heading--shaded">March 17-18 (cancelled) Meeting - 2020</h5>
<h5 class="panel-heading panel-heading--shaded">March 19 (notation vote) - 2020</h5>
<h5 class="panel-heading panel-heading--shaded">April/May 30-1 Meeting - 2019</h5>
"""


def test_historical_fomc_keeps_only_scheduled_meetings_at_14_new_york():
    ev = parse_fomc_historical(HTML, retrieved_at=datetime(2026, 11, 8, tzinfo=timezone.utc), source_url="x")
    ids = [e["event_id"] for e in ev]
    assert ids == ["fomc-2020-01-29", "fomc-2020-03-18", "fomc-2019-05-01"]
    assert ev[0]["scheduled_at"] == "2020-01-29T19:00:00+00:00"   # 14:00 EST
    assert ev[1]["scheduled_at"] == "2020-03-18T18:00:00+00:00"   # 14:00 EDT
    with pytest.raises(ValueError):
        parse_fomc_historical("<p>nothing</p>", retrieved_at=datetime(2026, 11, 8, tzinfo=timezone.utc), source_url="x")


def test_preregistration_freezes_variants_and_risk():
    from research.mtf_scalp4h_oos import frozen_variants
    from trading.mtf.scalp4h import Scalp4HParams
    from trading.mtf.strategies import ScalpParams
    pre = json.loads(Path("research/forward_test/xau_scalp4h_oos_2019_2020_preregistration.json").read_text())
    v = frozen_variants(pre)
    assert isinstance(v["B"], ScalpParams) and v["B"].reward_to_risk == 2.0
    assert v["B4H"] == Scalp4HParams(h1_confirm=True, reward_to_risk=2.5, pull_atr=0.5, trigger_bars=5)
    assert v["B4H_only"].h1_confirm is False and v["B4H_only"].reward_to_risk == 1.75
    assert pre["account"]["max_intended_risk_per_trade"] == 0.005 and pre["orders"] == "none"
    assert pre["data"]["evaluation_span_utc"][0].startswith("2019-01-01")
