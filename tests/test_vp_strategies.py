"""Value-area rejection / breakout-retest rules on SYNTHETIC candles: exact signals, invalidation, 4H filter,
one signal per level set, and no look-ahead (prefix invariance)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading.mtf.features import atr
from trading.mtf.frames import Frame
from trading.vp.strategies import VPParams, value_area_candidates

VAH, VAL, POC = 101.0, 99.0, 100.0


def _frame(extra: list[tuple[float, float, float, float]], warm: int = 30) -> Frame:
    rows = [(100.0, 100.5, 99.5, 100.0)] * warm + extra
    idx = pd.date_range("2024-01-02T00:00Z", periods=len(rows), freq="5min")
    return Frame("5m", pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0))


def _levels(n: int, pid=None) -> dict:
    pid = np.zeros(n, dtype=int) if pid is None else np.asarray(pid)
    return {"pid": pid, "vah": np.full(n, VAH), "val": np.full(n, VAL), "poc": np.full(n, POC), "total": np.full(n, 5.0)}


def _atr(f: Frame) -> np.ndarray:
    return atr(*(f.df[k].to_numpy() for k in ("high", "low", "close")), 14)


def test_rejection_short_after_excursion_above_vah_and_close_back_inside():
    f = _frame([(100.8, 101.6, 100.7, 101.4), (101.4, 101.5, 100.4, 100.6)])
    c = value_area_candidates(f, _levels(len(f)), VPParams(strategy="rejection", min_stop_atr=0.1), allow_short=True)
    assert len(c) == 1
    row, a = c.iloc[0], _atr(f)[-1]
    assert row.side == -1 and row.time_ns == f.close_ns[-1] and row.entry_ref == 100.6
    assert row.stop == pytest.approx(101.6 + 0.10 * a)
    assert row.rr == pytest.approx((100.6 - POC) / (row.stop - 100.6))
    assert row.tag == "VPA" and row.min_net_rr == 1.0 and row.max_hold_minutes == 240


def test_rejection_long_to_opposite_boundary_and_no_signal_without_return_inside():
    f = _frame([(99.2, 99.3, 98.4, 98.6), (98.6, 99.4, 98.5, 99.3)])
    c = value_area_candidates(f, _levels(len(f)), VPParams(strategy="rejection", target="opposite", min_stop_atr=0.1),
                              allow_short=True)
    assert list(c.side) == [1] and c.iloc[0].rr == pytest.approx((VAH - 99.3) / (99.3 - c.iloc[0].stop))
    still_outside = _frame([(99.2, 99.3, 98.4, 98.6), (98.6, 98.9, 98.5, 98.8)])
    assert value_area_candidates(still_outside, _levels(len(still_outside)), VPParams(min_stop_atr=0.1), allow_short=True).empty


def test_breakout_retest_confirm_long():
    f = _frame([(100.8, 101.6, 100.7, 101.5),   # break: close above VAH, previous close inside
                (101.5, 101.9, 101.4, 101.8),
                (101.8, 101.8, 101.05, 101.3),  # retest: low within VAH + 0.25 ATR, close still above
                (101.3, 101.7, 101.2, 101.6)])  # confirmation: closes above VAH
    p = VPParams(strategy="breakout", reward_to_risk=2.0, min_stop_atr=0.1)
    c = value_area_candidates(f, _levels(len(f)), p, allow_short=True)
    assert len(c) == 1
    row, a = c.iloc[0], _atr(f)[-1]
    assert row.side == 1 and row.time_ns == f.close_ns[-1] and row.entry_ref == 101.6
    assert row.stop == pytest.approx(101.05 - 0.10 * a) and row.rr == 2.0 and row.tag == "VPB"


@pytest.mark.parametrize("bars", [
    [(100.8, 101.6, 100.7, 101.5), (101.5, 101.6, 100.6, 100.9), (100.9, 101.7, 100.9, 101.6)],   # back inside first
    [(100.8, 101.6, 100.7, 101.5), (101.5, 101.6, 101.0, 101.3), (101.3, 101.4, 100.5, 100.8)],   # confirmation fails
])
def test_breakout_invalidated(bars):
    f = _frame(bars)
    assert value_area_candidates(f, _levels(len(f)), VPParams(strategy="breakout", min_stop_atr=0.1), allow_short=True).empty


def test_4h_filter_and_long_only():
    f = _frame([(100.8, 101.6, 100.7, 101.4), (101.4, 101.5, 100.4, 100.6)])
    lv = _levels(len(f))
    p = VPParams(strategy="rejection", htf_filter=True, min_stop_atr=0.1)
    assert value_area_candidates(f, lv, p, allow_short=True, d4=np.full(len(f), 1.0)).empty
    assert len(value_area_candidates(f, lv, p, allow_short=True, d4=np.full(len(f), -1.0))) == 1
    assert value_area_candidates(f, lv, VPParams(min_stop_atr=0.1), allow_short=False).empty  # spot: no shorts
    with pytest.raises(ValueError):
        value_area_candidates(f, lv, p, allow_short=True)


def test_one_signal_per_side_per_level_set_and_reset_on_new_set():
    pattern = [(100.8, 101.6, 100.7, 101.4), (101.4, 101.5, 100.4, 100.6), (100.6, 100.7, 100.5, 100.6)]
    f = _frame(pattern * 2)
    n = len(f)
    same = value_area_candidates(f, _levels(n), VPParams(min_stop_atr=0.1), allow_short=True)
    pid = np.zeros(n, dtype=int)
    pid[n - 3:] = 1
    new = value_area_candidates(f, _levels(n, pid), VPParams(min_stop_atr=0.1), allow_short=True)
    assert len(same) == 1 and len(new) == 2 and list(new.level_pid) == [0, 1]


def test_no_lookahead_prefix_invariance():
    rng = np.random.default_rng(3)
    c = 100 + np.cumsum(rng.normal(0, 0.4, 600))
    o = np.concatenate([[100], c[:-1]])
    h, low = np.maximum(o, c) + rng.random(600) * 0.3, np.minimum(o, c) - rng.random(600) * 0.3
    idx = pd.date_range("2024-01-02T00:00Z", periods=600, freq="5min")
    df = pd.DataFrame({"open": o, "high": h, "low": low, "close": c, "volume": 1.0}, index=idx)
    pid = np.arange(600) // 96
    lv = {"pid": pid, "vah": 100 + np.sin(pid) + 1, "val": 100 + np.sin(pid) - 1, "poc": 100 + np.sin(pid), "total": np.ones(600)}
    for strat in ("rejection", "breakout"):
        p = VPParams(strategy=strat, min_stop_atr=0.1)
        full = value_area_candidates(Frame("5m", df), lv, p, allow_short=True)
        cut = 400
        part = value_area_candidates(Frame("5m", df.iloc[:cut]), {k: v[:cut] for k, v in lv.items()}, p, allow_short=True)
        assert len(full) > 0
        pd.testing.assert_frame_equal(full[full.time_ns <= df.index[cut - 1].value + 300 * 10**9].reset_index(drop=True), part)


def test_protocol_grid_and_risk_are_frozen():
    proto = json.loads(Path("research/configs/vp_v1_protocol.json").read_text())
    from research.vp_research import variant_grid
    assert proto["risk"]["max_intended_risk_per_trade"] == 0.005 and proto["risk"]["orders"] == "none"
    sizes = {v: len(variant_grid(proto, v)) for v in proto["variants"]}
    assert sizes == {"A": 24, "A_4H": 24, "B": 24, "B_4H": 24} and sum(sizes.values()) == proto["trials_per_market"]
    assert all(g.htf_filter for g in variant_grid(proto, "B_4H")) and not any(g.htf_filter for g in variant_grid(proto, "A"))
