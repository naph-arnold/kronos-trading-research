"""Kronos Monte Carlo validation: correctness on synthetic fixtures (software tests only) and isolation guarantees.
No test reads or writes a runner's state."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import numpy as np
import pytest

from research.montecarlo import engine
from research.montecarlo.costs import adjusted_r, extra_cost_usd
from research.montecarlo.evidence import calendar_periods, effective_sample_size, summarize
from research.montecarlo.records import (RecordError, TradeDataset, from_phase2_report, from_shadow_store,
                                         synthetic_fixture, validate)
from research.montecarlo.simulate import (MAX_RISK_FRACTION, sample_calendar_blocks, sample_iid,
                                          sample_regime_conditional, sample_regime_stratified, sample_trade_blocks,
                                          simulate_paths)

UTC = timezone.utc
FAST = {"scenarios": 400, "batch_size": 100, "min_trades": 10, "min_months_with_trades": 2, "min_free_memory_mb": 0,
        "low_priority": False, "block_lens": [2, 4], "min_regime_trades": 5}
REGS = ["up-lowvol", "up-highvol", "down-lowvol", "down-highvol"]


def _fixture(n=40, regimes=True, seed=3):
    rng = np.random.default_rng(seed)
    r = [float(x) for x in np.round(rng.choice([-1.0, -1.05, 1.5, 0.4], size=n, p=[0.45, 0.05, 0.35, 0.15]), 4)]
    return synthetic_fixture(r, regimes=[REGS[i % 4] for i in range(n)] if regimes else None)


def _rng(s=1):
    return np.random.Generator(np.random.PCG64(s))


# --------------------------------------------------------------------------- sampling

def test_iid_bootstrap_draws_only_observed_trades_and_is_seeded():
    a, b = sample_iid(10, 500, _rng(5)), sample_iid(10, 500, _rng(5))
    assert (a == b).all() and a.min() >= 0 and a.max() <= 9
    assert abs(np.bincount(a.ravel(), minlength=10) / a.size - 0.1).max() < 0.02  # uniform over trades


def test_trade_blocks_keep_consecutive_runs_circularly():
    idx = sample_trade_blocks(10, 50, _rng(2), block_len=4)
    assert idx.shape == (50, 10)
    for row in idx:
        for start in range(0, 8, 4):
            blk = row[start:start + 4]
            assert all((blk[i + 1] - blk[i]) % 10 == 1 for i in range(len(blk) - 1))  # dependence preserved
    with pytest.raises(ValueError):
        sample_trade_blocks(10, 1, _rng(), block_len=0)


def test_calendar_blocks_keep_horizon_including_empty_periods():
    periods = [np.array([0, 1]), np.array([], dtype=np.int64), np.array([2])]
    idx, picks = sample_calendar_blocks(periods, 200, _rng(4))
    assert picks.shape == (200, 3)  # horizon = 3 periods, always
    for row, pk in zip(idx, picks):
        assert (row >= 0).sum() == sum(len(periods[p]) for p in pk)  # variable trade count per path


def test_regime_sampling_respects_labels():
    reg = np.array(REGS * 5)
    strat = sample_regime_stratified(reg, 100, _rng(1))
    assert (reg[strat] == reg[None, :]).all()  # each slot keeps its observed regime
    cond = sample_regime_conditional(reg, "down-highvol", 20, 100, _rng(1))
    assert set(reg[cond].ravel()) == {"down-highvol"}
    with pytest.raises(ValueError):
        sample_regime_conditional(reg, "sideways", 20, 1, _rng())


def test_regime_classification_reproduces_the_research_rule():
    from trading.evaluation import regime_labels
    from trading.marketdata import Bar
    t0 = datetime(2030, 1, 1, tzinfo=UTC)
    bars = []
    for i in range(400):  # rising then falling, volatility switching
        p = D(1000 + (i if i < 250 else 500 - i)) + D(i % 7)
        w = D(5 if (i // 50) % 2 else 1)
        bars.append(Bar.create(symbol="XAUUSD", interval="15m", open_time=t0 + timedelta(minutes=15 * i), open=p,
                               high=p + w, low=p - w, close=p))
    labels = regime_labels(bars)
    assert labels[bars[240].close_time].startswith("up-") and labels[bars[399].close_time].startswith("down-")
    assert labels[bars[0].close_time] == "warmup" and {"highvol", "lowvol"} <= {v.split("-")[-1] for v in labels.values()}


# --------------------------------------------------------------------------- path metrics

def _sim(r, days=None, policy="fixed_0.5pct", highvol=None, **kw):
    r = np.array([r], dtype=float)
    days = np.array([days if days is not None else list(range(r.shape[1]))])
    return simulate_paths(r, days, None if highvol is None else np.array([highvol]), policy, **kw)


def test_drawdown_streak_and_recovery_exact():
    m = _sim([1, -1, -1, -1, 2, 2, -1])
    eq = 1.0
    path = []
    for x in [1, -1, -1, -1, 2, 2, -1]:
        eq += x * 0.005 * eq
        path.append(eq)
    peak = np.maximum.accumulate(path)
    assert m["ending_equity"][0] == pytest.approx(path[-1])
    assert m["max_drawdown_pct"][0] == pytest.approx(100 * max(1 - p / k for p, k in zip(path, peak)))
    assert m["longest_losing_streak"][0] == 3
    assert m["longest_recovered_spell"][0] == 4      # underwater after trades 2,3,4,5 -> recovered at trade 6
    assert bool(m["censored"][0]) is True and m["censored_spell"][0] == 1  # final loss leaves an open spell


def test_unrecovered_path_is_censored_not_recovered():
    m = _sim([1, -1, -1, -1])
    assert m["longest_recovered_spell"][0] == 0 and bool(m["censored"][0]) and m["censored_spell"][0] == 3
    assert m["time_underwater_pct"][0] == pytest.approx(75.0)


def test_daily_loss_threshold_crossing_and_halt():
    # three -1R losses on the same day = -1.49%; a fourth would cross 1.5% only via a larger loss
    m = _sim([-1, -1, -1, -1.2, -1], days=[1, 1, 1, 1, 1])
    assert bool(m["daily_loss_crossed"][0]) and m["trades_skipped_by_halt"][0] == 1
    m2 = _sim([-1, -1, -1, -1], days=[1, 1, 2, 2])
    assert not bool(m2["daily_loss_crossed"][0]) and m2["trades_skipped_by_halt"][0] == 0


def test_loss_threshold_probabilities():
    m = _sim([-1.0] * 25)   # 1 - 0.995^25 = 11.8% drawdown
    assert bool(m["dd_ge_0.1"][0]) and bool(m["equity_loss_ge_0.1"][0]) and not bool(_sim([-1.0] * 5)["dd_ge_0.05"][0])


def test_adaptive_policies_never_exceed_the_ceiling_and_reduce_only():
    r = [-1, -1, -1, -1, -1, -1, -1, 2, 2]
    fixed = _sim(r)
    for pol, extra in (("drawdown_step", {}), ("loss_streak", {}), ("volatility_regime", {"highvol": [True] * 9})):
        m = _sim(r, policy=pol, **extra)
        assert m["max_fraction_used"] <= MAX_RISK_FRACTION
        assert m["max_drawdown_pct"][0] <= fixed["max_drawdown_pct"][0] + 1e-12  # never sizes up after losses
    with pytest.raises(ValueError):
        _sim(r, max_fraction=0.006)
    with pytest.raises(ValueError):
        _sim(r, reduced_fraction=0.01)
    with pytest.raises(ValueError):
        _sim(r, policy="martingale")


# --------------------------------------------------------------------------- costs

def test_adverse_execution_costs_xau_and_btc():
    ds = synthetic_fixture([1.0, -1.0])
    ds.trades = [replace(t, size=0.02, planned_risk=10.0) for t in ds.trades]
    sc = {"kind": "assumed", "contract_size": 100, "extra_spread": 0.2, "extra_slippage": 0.1, "latency_slippage": 0.05,
          "extra_commission": 7.0}
    # 0.02 lots x 100 oz x (0.2 + 2*0.1 + 2*0.05) + 0.02 x 7 = 1.0 + 0.14
    assert extra_cost_usd(ds, sc) == pytest.approx([1.14, 1.14])
    assert adjusted_r(ds, sc) == pytest.approx([t.r_net - 0.114 for t in ds.trades])
    assert (adjusted_r(ds, {"kind": "recorded"}) == np.array([t.r_net for t in ds.trades])).all()
    btc = synthetic_fixture([1.0], market="btcusdt")
    btc.trades = [replace(btc.trades[0], entry=60000.0, exit=62000.0, size=0.01)]
    assert extra_cost_usd(btc, {"extra_bps_per_side": 5}) == pytest.approx([0.0005 * 122000 * 0.01])


# --------------------------------------------------------------------------- records, provenance, isolation

def test_validation_refuses_duplicates_inconsistent_r_and_out_of_period():
    ds = synthetic_fixture([1.0, -1.0, 0.5])
    dup = TradeDataset(**{**ds.__dict__, "trades": ds.trades + [ds.trades[0]]})
    with pytest.raises(RecordError, match="duplicate"):
        validate(dup)
    bad = TradeDataset(**{**ds.__dict__, "trades": [replace(ds.trades[0], r_net=2.0)]})
    with pytest.raises(RecordError, match="R-multiple"):
        validate(bad)
    with pytest.raises(RecordError, match="outside"):
        validate(ds, period=("2031-01-01T00:00:00+00:00", "2031-02-01T00:00:00+00:00"))
    bad_cost = TradeDataset(**{**ds.__dict__, "trades": [replace(ds.trades[0], costs=5.0)]})
    with pytest.raises(RecordError, match="gross"):
        validate(bad_cost)


def test_fingerprint_detects_edits_and_partition_is_kept():
    ds = _fixture()
    d = ds.to_json()
    assert TradeDataset.from_json(d).fingerprint() == ds.fingerprint()
    d["trades"][0]["net_pnl"] += 1
    with pytest.raises(RecordError, match="fingerprint"):
        TradeDataset.from_json(d)
    assert ds.partition == "synthetic_fixture"
    with pytest.raises(ValueError, match="software tests only"):
        engine.run(ds, FAST)


def test_phase2_report_loader_checks_count_and_provenance(tmp_path):
    trades = [{"signal_id": f"s{i}", "side": "long", "size": "0.01", "entry": "100", "exit": "101",
               "opened_at": f"2026-0{1 + i}-05T10:00:00+00:00", "closed_at": f"2026-0{1 + i}-05T12:00:00+00:00",
               "exit_reason": "take_profit", "pnl": "1.5", "gross_pnl": "1.75", "costs": "0.25", "planned_risk": "1",
               "r_multiple": "1.5", "bars_held": 2} for i in range(3)]
    rep = {"config_sha256": "c", "dataset": {"sha256": "d"}, "initial_equity": "3000", "commission_scenarios": ["0"],
           "holdout_period": ["2026-01-01", "2026-12-01"],
           "strategies": {"x": {"holdout": {"trades": 3, "start": "2026-01-01T00:00:00+00:00",
                                            "end": "2026-12-01T00:00:00+00:00"}, "holdout_trades": trades,
                                "configurations": 4}, "y": {}}}
    p = tmp_path / "r.json"
    p.write_text(json.dumps(rep))
    ds = from_phase2_report(p, "xauusd", "x")
    assert ds.partition == "historical_holdout" and ds.book["initial_equity"] == 3000
    assert ds.provenance["file_sha256"] and ds.provenance["dataset_sha256"] == "d"
    assert ds.selection == {"strategies_in_report": 2, "configurations_tried": 4,
                            "selection_method": "walk-forward selection on training windows; holdout evaluated once"}
    rep["strategies"]["x"]["holdout"]["trades"] = 4
    p.write_text(json.dumps(rep))
    with pytest.raises(RecordError, match="summary says"):
        from_phase2_report(p, "xauusd", "x")


def _forward_store(tmp_path, *, warm=False):
    from trading.shadow.store import ShadowStore
    db = tmp_path / "shadow.sqlite"
    s = ShadowStore(db)
    close = (datetime.now(UTC) - timedelta(days=3)).replace(microsecond=0)
    with s.transaction():
        for b in ("orb_newyork.e10000.c7", "orb_newyork.e3000.c0"):
            s.ensure_book(b, "orb_newyork", D("10000"), D("7"))
        s.record_signal("orb_newyork", "sig1", close, "warmup" if warm else "evaluated", {"side": "long"})
        body = {"side": "long", "entry": "2010", "exit": "2000", "size": "0.1", "opened_at": close.isoformat(),
                "closed_at": (close + timedelta(hours=1)).isoformat(), "pnl": "-50", "gross_pnl": "-46",
                "spread_slippage_cost": "2", "commission": "2", "swap": "0", "planned_risk": "50"}
        for b in ("orb_newyork.e10000.c7", "orb_newyork.e3000.c0"):  # the same signal in two alternative books
            s.open_position(b, "sig1", {})
            s.close_position(b, "sig1", close + timedelta(hours=1), D("-50"), body)
    s.close()
    return db


def test_forward_store_loader_uses_one_book_and_refuses_warmup(tmp_path):
    db = _forward_store(tmp_path)
    before = db.read_bytes()
    ds = from_shadow_store(db, "xauusd", "orb_newyork", "orb_newyork.e10000.c7", forward_start=(datetime.now(UTC) - timedelta(days=10)).isoformat())
    assert len(ds.trades) == 1 and ds.partition == "forward" and ds.trades[0].r_net == -1.0  # one book, not two
    assert db.read_bytes() == before  # read-only
    with pytest.raises(RecordError, match="warm-up"):
        from_shadow_store(db, "xauusd", "orb_newyork", "orb_newyork.e10000.c7", forward_start=(datetime.now(UTC) - timedelta(days=1)).isoformat())
    (tmp_path / "w").mkdir()
    warm = _forward_store(tmp_path / "w", warm=True)
    with pytest.raises(RecordError, match="warm-up"):
        from_shadow_store(warm, "xauusd", "orb_newyork", "orb_newyork.e10000.c7",
                          forward_start=(datetime.now(UTC) - timedelta(days=10)).isoformat())


# --------------------------------------------------------------------------- engine

def test_insufficient_evidence_produces_no_distribution():
    ds = synthetic_fixture([1.0, -1.0, 1.5])
    ds.partition = "forward"
    rep = engine.run(ds, {**FAST, "min_trades": 30})
    assert rep["status"] == "insufficient_evidence" and rep["variants"] == [] and "No distribution" in rep["reason"]
    empty = TradeDataset("btcusdt", "donchian", "forward", {"initial_equity": 10000}, {}, {}, [])
    assert engine.run(empty, FAST)["status"] == "insufficient_evidence"


def test_seed_reproducibility_resume_and_worker_independence(tmp_path):
    ds = _fixture()
    a = engine.run(ds, FAST, allow_fixture=True)
    b = engine.run(ds, FAST, allow_fixture=True, checkpoint_dir=tmp_path)
    assert a["status"] == "complete" and a["result_sha256"] == b["result_sha256"]
    c = engine.run(ds, FAST, allow_fixture=True, checkpoint_dir=tmp_path)  # resumes entirely from checkpoint
    assert c["result_sha256"] == a["result_sha256"]
    d = engine.run(ds, {**FAST, "seed": 7}, allow_fixture=True)
    assert d["result_sha256"] != a["result_sha256"]
    keys = {v["method"] for v in a["variants"]}
    assert keys == {"iid", "trade_block", "calendar_block", "regime_stratified", "regime_conditional"}
    assert all(v["scenarios"] == 400 for v in a["variants"])
    assert a["risk_sizing"]["operational_ceiling"] == 0.005


def test_cancellation_keeps_a_resumable_checkpoint(tmp_path):
    ds = _fixture()
    cancel = tmp_path / "CANCEL"
    cancel.write_text("")
    rep = engine.run(ds, FAST, allow_fixture=True, checkpoint_dir=tmp_path / "ck", cancel_file=cancel)
    assert rep["status"] == "cancelled" and rep["variants"] == []
    cancel.unlink()
    full = engine.run(ds, FAST, allow_fixture=True, checkpoint_dir=tmp_path / "ck", cancel_file=cancel)
    assert full["status"] == "complete" and full["result_sha256"] == engine.run(ds, FAST, allow_fixture=True)["result_sha256"]


def test_resource_check_defers_heavy_jobs(monkeypatch):
    monkeypatch.setattr(engine, "available_memory_mb", lambda: 10.0)
    rep = engine.run(_fixture(), {**FAST, "min_free_memory_mb": 400}, allow_fixture=True)
    assert rep["status"] == "deferred_insufficient_resources" and rep["variants"] == []


def test_evidence_reports_dependence_concentration_and_multiple_testing():
    ds = _fixture(n=60)
    ds.selection = {"strategies_in_report": 5}
    ev = summarize(ds, seed=1, comparisons=5)
    lo, hi = ev["mean_r_ci95"]
    alo, ahi = ev["multiple_testing"]["mean_r_ci_adjusted"]
    assert alo <= lo <= ev["mean_r"] <= hi <= ahi  # adjusted interval is wider
    assert ev["dependence"]["n_eff"] <= 60 and 0 < ev["concentration"]["top5_share_of_positive_r"] <= 1
    assert sum(ev["regime_coverage"].values()) == 60
    assert effective_sample_size(np.array([1.0, 1.0, -1.0, -1.0] * 10))["n_eff"] < 40  # positive autocorrelation
    weeks, labels = calendar_periods(ds, "week")
    assert sum(len(w) for w in weeks) == 60 and len(labels) == len(weeks)


# --------------------------------------------------------------------------- isolation

def test_module_has_no_order_or_runner_control_path():
    src = "".join(p.read_text(encoding="utf-8") for p in Path("research/montecarlo").glob("*.py"))
    for forbidden in ("order_send", "order_check", "MetaTrader5", "x.ai", "XAI_API_KEY", "STOP\"", "KILL\"",
                      "Unregister-ScheduledTask", "subprocess"):
        assert forbidden not in src
    assert "mode=ro" in src  # runner stores are opened read-only


def test_running_experiments_preregistrations_are_unchanged():
    """Shadow-runner behaviour is defined by these frozen files; the Monte Carlo branch must not alter them."""
    from research.phase2d_shadow import normalized_sha256
    for cfg_path in ("research/configs/phase2d_shadow_xauusd.json", "research/configs/phase2e_binance_shadow.json",
                     "research/configs/xau_selective_a_shadow.json"):
        cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
        assert normalized_sha256(Path(cfg["preregistration"])) == cfg["preregistration_sha256"], cfg_path
