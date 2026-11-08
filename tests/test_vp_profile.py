"""Volume Profile engine: deterministic POC/VAH/VAL, tie-breaking, causality, periods. SYNTHETIC data only."""
from __future__ import annotations

import io

import numpy as np
import pandas as pd
import pytest

from trading.vp.profile import (BinStore, bin_multiple, build_profile, completed_profiles, levels_at, periods,
                                point_of_control, tpo_store, value_area)

NS_MIN = 60_000_000_000


def test_value_area_expands_toward_larger_side_until_target():
    # total 12, target 8.4: POC=2 (5) -> add up (3 > 2) = 8 -> add down (2 > 1) = 10 >= 8.4
    assert value_area(np.array([1, 2, 5, 3, 1.0]), 0.70) == (2, 1, 3)
    assert value_area(np.array([1, 2, 5, 3, 1.0]), 1.0) == (2, 0, 4)


def test_poc_ties_prefer_centre_then_lower_price():
    assert point_of_control(np.array([5, 1, 5.0])) == 0          # equidistant from centre -> lower price
    assert point_of_control(np.array([1, 5, 1, 5, 1.0])) == 1
    assert point_of_control(np.array([5, 1, 1, 1, 5, 1.0])) == 4  # centre 2.5: index 4 is closer than 0


def test_expansion_tie_adds_both_sides_and_rows_two():
    assert value_area(np.array([2, 4, 2.0]), 0.9) == (1, 0, 2)
    # rows=2 compares pairs: above (1+1)=2 vs below (3+0)=3 -> below first
    assert value_area(np.array([0, 3, 6, 1, 1.0]), 0.75, rows=2) == (2, 0, 2)


@pytest.mark.parametrize("w", [np.zeros(3), np.array([1.0, -1.0]), np.array([]), np.array([np.nan, 1.0])])
def test_invalid_weights_rejected(w):
    with pytest.raises(ValueError):
        point_of_control(w)


def test_profile_prices_are_bin_edges_and_poc_centre():
    bins = np.array([1000, 1001, 1002, 1003])  # base bin 0.10 -> prices 100.0 .. 100.4
    w = np.array([1, 4, 2, 1.0])
    p = build_profile(bins, w, base_bin=0.1, bin_bps=0.1, va_pct=0.7, weight_kind="quote_ticks", start_ns=0,
                      end_ns=1, buckets=1)
    assert p.bin_size == pytest.approx(0.1)
    assert (p.poc_idx, p.va_lo_idx, p.va_hi_idx) == (1, 1, 2)
    assert p.val == pytest.approx(100.1) and p.vah == pytest.approx(100.3) and p.poc == pytest.approx(100.15)
    assert p.va_share() >= 0.7


def test_bin_multiple_scales_with_price():
    assert bin_multiple(60_000, 2, 5) == 2 and bin_multiple(110_000, 2, 5) == 4 and bin_multiple(1300, 2, 0.1) == 3
    assert bin_multiple(10, 1, 5) == 1


def _store():
    t0 = pd.Timestamp("2024-01-02T00:00Z").value // 1_000_000
    q = 15 * 60_000
    bucket = np.array([t0, t0, t0 + q, t0 + q, t0 + 2 * q, t0 + 3 * q])
    bins = np.array([100, 101, 101, 102, 103, 110])
    w = np.array([1.0, 2.0, 3.0, 1.0, 5.0, 9.0])
    return BinStore(bucket, bins, w, base_bin=1.0, weight_kind="traded_base_qty"), t0 * 1_000_000


def test_store_uses_only_buckets_completed_inside_the_period():
    s, t0 = _store()
    q = 15 * NS_MIN
    p = s.profile(t0, t0 + 2 * q, bin_bps=1, va_pct=1.0)
    assert p.buckets == 2 and p.total == pytest.approx(7.0)
    # a period ending mid-bucket excludes that bucket
    assert s.profile(t0, t0 + 2 * q + NS_MIN, bin_bps=1, va_pct=1.0).total == pytest.approx(7.0)
    assert s.profile(t0 + NS_MIN, t0 + 2 * q, bin_bps=1, va_pct=1.0).total == pytest.approx(4.0)
    assert s.profile(t0, t0 + 2 * q, bin_bps=1, va_pct=1.0, min_buckets=3) is None


def test_developing_profile_is_causal():
    s, t0 = _store()
    q = 15 * NS_MIN
    t = t0 + 2 * q + 7 * NS_MIN
    before = s.developing(t0, t, bin_bps=1, va_pct=0.7)
    bk = np.concatenate([s.bucket_ns // 1_000_000, [(t0 + 2 * q) // 1_000_000]])
    order = np.argsort(bk, kind="stable")
    s2 = BinStore(bk[order], np.concatenate([s.bins, [90]])[order], np.concatenate([s.w, [100.0]])[order],
                  base_bin=1.0, weight_kind="traded_base_qty")
    after = s2.developing(t0, t, bin_bps=1, va_pct=0.7)  # data in the still-forming bucket must not matter
    assert np.array_equal(before.weights, after.weights) and before.vah == after.vah
    assert s.developing(t0, t0 + 5 * NS_MIN, bin_bps=1, va_pct=0.7) is None


def test_profiles_are_reproducible():
    s, t0 = _store()
    a = s.profile(t0, t0 + 60 * NS_MIN, bin_bps=1, va_pct=0.7)
    b = s.profile(t0, t0 + 60 * NS_MIN, bin_bps=1, va_pct=0.7)
    assert (a.poc, a.vah, a.val) == (b.poc, b.vah, b.val) and np.array_equal(a.weights, b.weights)


def test_xau_periods_follow_new_york_17h_across_dst():
    days = periods("xau_day", pd.Timestamp("2019-03-08T00:00Z"), pd.Timestamp("2019-03-13T00:00Z"))
    lab = {d[2]: d for d in days}
    # Monday 2019-03-11 (US DST began Sunday 03-10): Sun 17:00 EDT = 21:00 UTC -> Mon 21:00 UTC
    assert pd.Timestamp(lab["2019-03-11"][0], tz="UTC") == pd.Timestamp("2019-03-10T21:00Z")
    assert pd.Timestamp(lab["2019-03-11"][1], tz="UTC") == pd.Timestamp("2019-03-11T21:00Z")
    # Friday 2019-03-08 in winter: Thu 22:00 UTC -> Fri 22:00 UTC
    assert pd.Timestamp(lab["2019-03-08"][1], tz="UTC") == pd.Timestamp("2019-03-08T22:00Z")
    sess = [p for p in periods("xau_session", pd.Timestamp("2019-07-02T00:00Z"), pd.Timestamp("2019-07-03T00:00Z"))
            if p[2].startswith("2019-07-02")]
    assert [(p[1] - p[0]) // (3600 * 10**9) for p in sess] == [10, 5, 9]
    s8 = periods("utc_session8", pd.Timestamp("2024-01-02T00:00Z"), pd.Timestamp("2024-01-03T00:00Z"))
    assert all(e - s == 8 * 3600 * 10**9 for s, e, _ in s8)


def test_levels_use_only_profiles_already_completed_and_not_stale():
    s, t0 = _store()
    q = 15 * NS_MIN
    profs = [s.profile(t0, t0 + 2 * q, bin_bps=1, va_pct=0.7), s.profile(t0 + 2 * q, t0 + 4 * q, bin_bps=1, va_pct=0.7)]
    t = np.array([t0 + q, t0 + 2 * q, t0 + 4 * q - 1, t0 + 4 * q, t0 + 4 * q + 10 * 86_400 * 10**9])
    lv = levels_at(profs, t, max_age_ns=4 * 86_400 * 10**9)
    assert list(lv["pid"]) == [-1, 0, 0, 1, -1]
    assert np.isnan(lv["vah"][0]) and lv["vah"][3] == profs[1].vah


def test_completed_profiles_drop_low_coverage_periods():
    s, t0 = _store()
    full = completed_profiles(s, "utc_day", pd.Timestamp("2024-01-02T00:00Z"), pd.Timestamp("2024-01-03T00:00Z"),
                              bin_bps=1, va_pct=0.7, min_coverage=0.0)
    none = completed_profiles(s, "utc_day", pd.Timestamp("2024-01-02T00:00Z"), pd.Timestamp("2024-01-03T00:00Z"),
                              bin_bps=1, va_pct=0.7, min_coverage=0.5)
    assert len(full) == 1 and full[0].buckets == 4 and none == []


def test_tpo_store_counts_one_per_bin_touched_per_minute():
    idx = pd.date_range("2024-01-02T00:00Z", periods=3, freq="1min")
    bars = pd.DataFrame({"low": [100.2, 100.0, 101.5], "high": [101.1, 100.4, 101.5]}, index=idx)
    s = tpo_store(bars, base_bin=1.0)
    assert s.weight_kind == "tpo_minutes"
    got = dict(zip(s.bins.tolist(), s.w.tolist()))
    assert got == {100: 2.0, 101: 2.0}


def test_store_load_verifies_checksum(tmp_path):
    from research.vp_data import save_bins
    man = save_bins(tmp_path / "x", np.array([0, 0]), np.array([2, 1]), np.array([1.0, 2.0]),
                    {"base_bin": 1.0, "weight_kind": "traded_base_qty"})
    s = BinStore.load(tmp_path / "x")
    assert list(s.bins) == [1, 2] and man["rows"] == 2
    (tmp_path / "x.npz").write_bytes((tmp_path / "x.npz").read_bytes() + b"x")
    with pytest.raises(ValueError):
        BinStore.load(tmp_path / "x")


def test_aggtrades_processing_checks_ids_and_normalizes_microseconds():
    from research.vp_data import process_aggtrades_csv
    csv = (b"agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker,is_best_match\n"
           b"10,100.05,1.5,20,21,1700000000000,True,True\n11,100.26,0.5,22,22,1700000030000,False,True\n"
           b"13,99.99,2,24,25,1700000065000000,True,True\n")
    st = {k: 0 for k in ("agg_id_gaps", "agg_ids_missing", "trade_id_gaps", "trade_ids_missing", "ts_decreasing",
                         "invalid_rows", "rows", "trades")}
    st.update(last_agg_id=None, last_trade_id=None, last_ts=None, first_ts=None)
    (b, k, w), mins = process_aggtrades_csv(io.BytesIO(csv), 0.1, st, chunksize=2)
    assert list(k) == [999, 1000, 1002] and list(w) == [2.0, 1.5, 0.5]
    assert st["agg_id_gaps"] == 1 and st["trade_id_gaps"] == 1 and st["rows"] == 3 and st["trades"] == 5
    assert st["last_ts"] == 1700000065000  # microseconds normalized
    assert list(mins.volume) == [2.0, 2.0] and mins.iloc[0].high == 100.26
