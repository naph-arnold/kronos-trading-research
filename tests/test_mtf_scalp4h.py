"""Experimental 4H-led scalper (scalp4h-v1): causality, mandatory 4H bias, H1 alignment, versioning.
All market data here is SYNTHETIC."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tests.test_mtf_framework import frames_from, walk
from trading.mtf.frames import Frame, align
from trading.mtf.scalp4h import STRATEGY_VERSION, TAG, Scalp4HParams, h1_confirmation, scalp4h_candidates
from trading.mtf.strategies import ScalpParams, _direction

P = Scalp4HParams(dir_ema=10, struct_k=1)


@pytest.mark.parametrize("h1_confirm", [True, False])
def test_candidates_are_prefix_invariant(h1_confirm):
    base = walk(9000, seed=5)
    p = Scalp4HParams(dir_ema=10, struct_k=1, h1_confirm=h1_confirm)
    full = scalp4h_candidates(frames_from(base), p, allow_short=True)
    assert len(full) > 0
    cut = 6000
    part = scalp4h_candidates(frames_from(Frame("1m", base.df.iloc[:cut])), p, allow_short=True)
    cutoff = base.close_ns[cut - 1]
    pd.testing.assert_frame_equal(part.reset_index(drop=True),
                                  full[full.time_ns <= cutoff].reset_index(drop=True), check_exact=False, rtol=1e-12)


def _at_signal(frames, values, f_htf, times):
    idx = align(frames["1m"], f_htf)
    pos = np.searchsorted(frames["1m"].close_ns, times)
    return values[idx[pos]], idx[pos]


@pytest.mark.parametrize("seed", [5, 8])
def test_every_entry_agrees_with_the_completed_4h_bias_and_h1(seed):
    frames = frames_from(walk(9000, seed=seed))
    c = scalp4h_candidates(frames, P, allow_short=True)
    assert len(c) > 0 and set(c.tag) == {TAG}
    d4, i4 = _at_signal(frames, _direction(frames["4h"], P.dir_ema, P.dir_slope_bars), frames["4h"], c.time_ns.to_numpy())
    assert (i4 >= 0).all() and (d4 == c.side.to_numpy()).all()
    assert (frames["4h"].close_ns[i4] <= c.time_ns.to_numpy()).all()  # completed 4H candles only
    h1, _ = _at_signal(frames, h1_confirmation(frames["1h"], P.struct_k, P.h1_ema), frames["1h"], c.time_ns.to_numpy())
    assert (h1 == c.side.to_numpy()).all()


def test_h1_alignment_only_removes_entries_and_4h_is_never_replaced():
    frames = frames_from(walk(9000, seed=8))
    aligned = scalp4h_candidates(frames, P, allow_short=True)
    only4h = scalp4h_candidates(frames, Scalp4HParams(dir_ema=10, struct_k=1, h1_confirm=False), allow_short=True)
    keys = set(zip(only4h.time_ns, only4h.side))
    assert set(zip(aligned.time_ns, aligned.side)) <= keys
    assert len(aligned) <= len(only4h)


def test_spot_mode_is_long_only():
    c = scalp4h_candidates(frames_from(walk(9000, seed=5)), P, allow_short=False)
    assert (c.side == 1).all()


def test_versioned_separately_and_baseline_unchanged():
    assert STRATEGY_VERSION == "scalp4h-v1" and TAG != "B"
    # the previously tested H1-led scalper keeps its parameters
    assert asdict(ScalpParams()) == {"dir_ema": 50, "dir_slope_bars": 3, "struct_ema": 20, "pull_ema": 20, "pull_bars": 3,
                                     "pull_atr": 0.25, "atr_n": 14, "trigger_bars": 5, "trigger_ema": 20,
                                     "stop_buffer_atr": 0.10, "min_stop_atr": 0.5, "reward_to_risk": 1.5,
                                     "min_net_rr": 1.5, "max_hold_minutes": 60}
    # shared lower-timeframe rules match the baseline so the comparison isolates the direction hierarchy
    b, n = asdict(ScalpParams()), asdict(Scalp4HParams())
    for k in ("pull_ema", "pull_bars", "pull_atr", "atr_n", "trigger_bars", "trigger_ema", "stop_buffer_atr",
              "min_stop_atr", "reward_to_risk", "min_net_rr", "max_hold_minutes", "dir_ema", "dir_slope_bars"):
        assert b[k] == n[k]


@pytest.mark.parametrize("market", ["btcusdt", "xauusd"])
def test_separate_market_configs_keep_risk_limits(market):
    cfg = json.loads(Path(f"research/configs/mtf_scalp4h_v1_{market}.json").read_text())
    assert cfg["market"] == market and cfg["strategy_version"] == STRATEGY_VERSION and cfg["research_only"]
    assert cfg["risk"] == {"max_intended_risk_per_trade": 0.005, "daily_loss_halt": 0.015,
                           "max_aggregate_open_risk": 0.01, "enforced_by": "trading.risk.evaluate (unchanged)"}
    assert cfg["hierarchy"][0].startswith("4h") and "mandatory" in cfg["hierarchy"][0]
    assert cfg["allow_short"] is (market == "xauusd")
