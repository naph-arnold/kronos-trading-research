"""XAUUSD scalp v2 generators: pre-registered rules, causality, risk-safe stops. Synthetic data (tests only)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.mtf_research import frames_for
from trading.mtf.frames import Frame
from trading.mtf.scalp_v2 import HYPOTHESES, h3_sweep_displacement

PREREG = Path("research/forward_test/xau_scalp_v2_preregistration.json")


def _frame(n=6 * 24 * 60, seed=3, start="2030-01-07T00:00Z"):
    rng = np.random.default_rng(seed)
    t = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    vol = 0.2 + 0.15 * np.sin(np.arange(n) / 700.0) ** 2           # alternating quiet / active regimes
    drift = 0.02 * np.sign(np.sin(np.arange(n) / 2500.0))          # trending legs
    close = 2000 + np.cumsum(drift + vol * rng.standard_normal(n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    spread = rng.uniform(0.05, 0.4, n)
    df = pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + spread, "low": np.minimum(open_, close) - spread,
                       "close": close, "volume": 1.0}, index=pd.DatetimeIndex(t, name="open_time"))
    return Frame("1m", df)


def test_preregistration_is_committed_and_matches_the_code():
    p = json.loads(PREREG.read_text(encoding="utf-8"))
    assert set(p["hypotheses"]) == set(HYPOTHESES)
    assert p["common"]["stop_atr_multiples_research_variable"] == [1.5, 2.5, 4.0]
    assert p["common"]["reward_to_risk"] == 2.0 and p["common"]["risk"]["risk_fraction"] == "0.005"
    assert "B4H" in p["not_reactivated"]


@pytest.mark.parametrize("name", list(HYPOTHESES))
def test_generators_are_causal_and_stops_are_on_the_right_side(name):
    full = _frame()
    frames = frames_for(full)
    cands = HYPOTHESES[name](frames, 2.5)
    cut = full.df.index[len(full.df) * 2 // 3]
    part = frames_for(Frame("1m", full.df[full.df.index < cut]))
    early = HYPOTHESES[name](part, 2.5)
    before = cands[cands.time_ns <= early.time_ns.max()] if len(early) else cands.iloc[:0]
    # future data never changes an earlier signal (allowing the last partial-bucket signals to differ)
    common = set(map(tuple, early[["time_ns", "side"]].to_numpy()[:-2])) if len(early) > 2 else set()
    assert common <= set(map(tuple, before[["time_ns", "side"]].to_numpy()))
    if len(cands):
        assert ((cands.entry_ref - cands.stop) * cands.side > 0).all()
        assert (cands.rr == 2.0).all() and (cands.max_hold_minutes == 240).all()
        hours = (cands.time_ns // 3_600_000_000_000) % 24
        assert ((hours >= 7) & (hours < 20)).all()


def test_wider_stop_multiple_never_narrows_the_stop():
    frames = frames_for(_frame())
    for name, fn in HYPOTHESES.items():
        a, b = fn(frames, 1.5), fn(frames, 4.0)
        m = a.merge(b, on=["time_ns", "side"], suffixes=("_a", "_b"))
        if len(m):
            assert ((m.entry_ref_b - m.stop_b) * m.side >= (m.entry_ref_a - m.stop_a) * m.side - 1e-9).all(), name


def test_h3_detects_asian_sweep_then_displacement_short():
    t = pd.date_range("2030-01-08T00:00Z", periods=10 * 60, freq="1min", tz="UTC")
    n = len(t)
    close = np.full(n, 2000.0) + 0.3 * np.sin(np.arange(n) / 5.0)   # Asian range about 1999.7 - 2000.3 (+/- wick)
    open_ = close.copy()
    high, low = close + 0.2, close - 0.2
    k = int((pd.Timestamp("2030-01-08T08:00Z") - t[0]) / pd.Timedelta(minutes=1))
    high[k:k + 5] = 2003.0          # 08:00-08:04: sweep above the Asian high ...
    close[k + 4] = open_[k + 4] = 2000.0  # ... closing back inside
    for j in range(k + 5, k + 10):  # 08:05-08:09: bearish displacement
        open_[j] = 2000.0 - (j - k - 5) * 0.8
        close[j] = open_[j] - 0.8
        high[j], low[j] = open_[j] + 0.05, close[j] - 0.05
    close[k + 10:] = close[k + 9]
    open_[k + 10:] = close[k + 9]
    high[k + 10:], low[k + 10:] = close[k + 9] + 0.2, close[k + 9] - 0.2
    df = pd.DataFrame({"open": open_, "high": np.maximum.reduce([high, open_, close]),
                       "low": np.minimum.reduce([low, open_, close]), "close": close, "volume": 1.0},
                      index=pd.DatetimeIndex(t, name="open_time"))
    c = h3_sweep_displacement(frames_for(Frame("1m", df)), 1.5)
    assert len(c) == 1 and c.side.iloc[0] == -1
    assert c.stop.iloc[0] >= 2003.0  # never inside the sweep extreme


def test_balance_equity_series_uses_recorded_checks_only(tmp_path):
    from command_center.account import balance_equity_series
    from trading.shadow.store import ShadowStore
    from datetime import datetime, timedelta, timezone
    state = tmp_path / "x"
    s = ShadowStore(state / "shadow.sqlite")
    t0 = datetime(2026, 11, 7, tzinfo=timezone.utc)
    with s.transaction():
        s.event(t0, "mt5_account_baseline", balance=3000.0, equity=3000.0)
        s.event(t0 + timedelta(minutes=5), "mt5_account_check", balance=3000.0, equity=2999.5)
        s.event(t0 + timedelta(minutes=10), "mt5_account_check", error="disconnected")  # no figures: skipped
    s.close()
    out = balance_equity_series({"markets": {"xauusd": {"state_dir": str(state)}}}, None)
    assert out["points"] == 2 and [p["value"] for p in out["equity"]] == [3000.0, 2999.5]
