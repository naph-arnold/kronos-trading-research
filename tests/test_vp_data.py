"""Volume-at-price source data: XAUUSD quote-tick bins/bars and the BTC kline cross-check. SYNTHETIC data only;
no terminal and no network."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from research.vp_data import BUCKET_MS, WEIGHT_KINDS, tick_bars, utc_ticks, validate_btc_minutes
from research.xau_tick_history import _chunk_bars
from tests.test_xau_tick_history import FakeConn, FakeMt5, _ticks


def test_quote_tick_bars_match_validated_construction_and_drop_bad_quotes():
    t0 = datetime(2019, 7, 9, 14, 0, tzinfo=timezone.utc)
    rows = [(t0.replace(second=1), 1400.10, 1400.35), (t0.replace(second=20), 1400.50, 1400.70),
            (t0.replace(minute=1, second=5), 1400.40, 1400.30),  # ask < bid: dropped
            (t0.replace(minute=2, second=0), 1401.00, 1401.22)]
    conn = FakeConn(FakeMt5(_ticks(rows)))
    utc_ms, bid, ask, srv = utc_ticks(conn, "XAUUSD", t0, t0.replace(hour=15))
    assert len(bid) == 3 and (np.diff(utc_ms) >= 0).all()
    ours = tick_bars(utc_ms, bid, ask, srv)
    ref, _ = _chunk_bars(conn, "XAUUSD", t0, t0.replace(hour=15), None)
    pd.testing.assert_frame_equal(ours[ref.columns], ref)
    assert (utc_ms // BUCKET_MS * BUCKET_MS == int(t0.timestamp() * 1000)).all()


def test_weight_kinds_label_proxy_explicitly():
    assert "PROXY" in WEIGHT_KINDS["quote_ticks"] and "NOT traded volume" in WEIGHT_KINDS["quote_ticks"]
    assert "Measured" in WEIGHT_KINDS["traded_base_qty"] and "no volume" in WEIGHT_KINDS["tpo_minutes"]


def test_btc_minutes_cross_check_against_klines():
    idx = pd.date_range("2024-06-01T00:00Z", periods=4, freq="1min")
    kl = pd.DataFrame({"open": [10, 11, 12, 13.0], "high_k": [11, 12, 13, 14.0], "low_k": [9, 10, 11, 12.0],
                       "close": [11, 12, 13, 13.5], "volume_k": [1.0, 2.0, 0.0, 4.0]}, index=idx)
    minute = idx.asi8 // 60_000_000_000
    mins = {"minute": minute[[0, 1, 3]], "volume": np.array([1.0, 2.0, 3.0]), "high": np.array([11, 12, 14.0]),
            "low": np.array([9, 10, 12.0]), "first": np.array([10, 11, 13.0]), "last": np.array([11, 12, 13.5]),
            "n": np.array([3, 4, 5])}
    r = validate_btc_minutes(mins, kl, int(idx[0].value // 1e6), int(idx[-1].value // 1e6) + 60_000)
    assert r["minutes_compared"] == 3 and r["kline_minutes_with_volume_but_no_trades"] == 0
    assert r["volume_match_1e-6_pct"] == round(2 / 3 * 100, 4) and r["high_match_pct"] == 100.0


def test_resource_guard_pauses_until_memory_recovers_and_stops_safely(monkeypatch):
    import research.vp_data as vd
    monkeypatch.setattr(vd.time, "sleep", lambda s: None)
    seq = iter([0.5, 0.8, 2.0])
    g = vd.ResourceGuard(1.0, 1.5, max_wait_s=60, status=lambda: {"avail_gb": next(seq), "proc_peak_ws_gb": 0.3})
    g.check("x")
    assert g.pauses == 1 and g.min_seen_gb == 0.5 and g.peak_proc_gb == 0.3
    g2 = vd.ResourceGuard(1.0, 1.5, max_wait_s=30, status=lambda: {"avail_gb": 0.2, "proc_peak_ws_gb": 0.1})
    import pytest
    with pytest.raises(MemoryError):
        g2.check("y")
    assert vd.ResourceGuard(status=lambda: None).check("z") is None


def test_exact_resent_aggtrade_rows_are_dropped_once_and_counted():
    import io

    from research.vp_data import process_aggtrades_csv
    rows = [b"1,100.0,1,1,1,1700000000000,True,True", b"2,100.0,2,2,2,1700000001000,True,True",
            b"3,101.0,4,3,3,1700000002000,True,True", b"2,100.0,2,2,2,1700000001000,True,True",
            b"3,101.0,4,3,3,1700000002000,True,True", b"4,102.0,8,4,4,1700000003000,True,True"]
    st = {k: 0 for k in ("agg_id_gaps", "agg_ids_missing", "trade_id_gaps", "trade_ids_missing", "ts_decreasing",
                         "invalid_rows", "rows", "trades")}
    st.update(last_agg_id=None, last_trade_id=None, last_ts=None, first_ts=None)
    (b, k, w), mins = process_aggtrades_csv(io.BytesIO(b"\n".join(rows) + b"\n"), 1.0, st, chunksize=2)
    assert st["duplicate_rows_dropped"] == 2 and st["agg_id_gaps"] == 0 and st["ts_decreasing"] == 0
    assert w.sum() == 15.0 and st["rows"] == 4


def test_misaligned_klines_fail_closed_unless_explicitly_excluded_and_recorded():
    import hashlib
    import io as _io
    import zipfile

    import pytest

    from research.binance_bulk import month_rows
    good = "1512367200000,1,1,1,1,2.0,1512367259999,2,3,0,0,0"
    bad = "1512367220799,1,1,1,1,0.5,1512367280798,0.5,1,0,0,0"
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BTCUSDT-1m-2017-12.csv", good + "\n" + bad + "\n")
    blob = buf.getvalue()

    def fetch(url):
        return (hashlib.sha256(blob).hexdigest() + "  x").encode() if url.endswith(".CHECKSUM") else blob
    with pytest.raises(ValueError):
        month_rows("BTCUSDT", "1m", "2017-12", fetch)
    rows, meta = month_rows("BTCUSDT", "1m", "2017-12", fetch, drop_misaligned=True)
    assert len(rows) == 1 and meta["dropped_misaligned"]["count"] == 1 and meta["dropped_misaligned"]["volume"] == 0.5
