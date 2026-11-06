"""Phase 2C research helpers: baselines, eligibility maths, trade statistics, tick reader (SYNTHETIC/fake)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from phase2_helpers import xau_m15
from research.phase2c_robustness import _session, breakdown, eligibility, stop_buckets, trade_stats
from trading.common import Side
from trading.replay import HistoryView
from trading.strategies.baselines import H1TrendBaseline, H1TrendBaselineParams, RandomEntryBaseline, RandomEntryParams

UTC = timezone.utc


def _signals(strategy, bars):
    return [s for i in range(len(bars)) if (s := strategy(HistoryView(bars, i + 1))) is not None]


def test_random_baseline_is_seeded_and_reproducible():
    bars = xau_m15(1500)
    a = _signals(RandomEntryBaseline(RandomEntryParams(seed=3, entry_probability=0.02), tick_size=D("0.01")), bars)
    b = _signals(RandomEntryBaseline(RandomEntryParams(seed=3, entry_probability=0.02), tick_size=D("0.01")), bars)
    c = _signals(RandomEntryBaseline(RandomEntryParams(seed=4, entry_probability=0.02), tick_size=D("0.01")), bars)
    assert [s.signal_id for s in a] == [s.signal_id for s in b] != [s.signal_id for s in c]
    assert {s.side for s in a} == {Side.LONG, Side.SHORT}


def test_baselines_are_causal():
    bars = xau_m15(2500)
    for make in (lambda: RandomEntryBaseline(RandomEntryParams(seed=1, entry_probability=0.02), tick_size=D("0.01")),
                 lambda: H1TrendBaseline(H1TrendBaselineParams(ema_fast=5, ema_slow=20), tick_size=D("0.01"))):
        full = {s.signal_id for s in _signals(make(), bars)}
        prefix = {s.signal_id for s in _signals(make(), bars[:1500])}
        cutoff = bars[1499].close_time.strftime("%Y%m%dT%H%M")
        assert prefix == {sid for sid in full if sid.rsplit("-", 2)[-2] <= cutoff}


def test_min_lot_eligibility_maths():
    trades = [{"stop_distance": d, "r": r} for d, r in ((3, 1.0), (8, -1.0), (14, 2.0), (20, -1.0), (40, -1.0))]
    rows = eligibility(trades, ["3000", "10000"], slippage=0.10, spread=0.13, commission=0.0)
    # 3,000 USD: budget 15 USD; 0.01 lot loses 1 USD per 1.00 move -> max stop 15 - 0.33 = 14.67
    assert rows["3000"]["max_stop_distance_usd"] == pytest.approx(14.67)
    assert rows["3000"]["eligible_trades"] == 3 and rows["3000"]["eligible_share"] == 0.6
    assert rows["10000"]["max_stop_distance_usd"] == pytest.approx(49.67) and rows["10000"]["eligible_trades"] == 5
    with_commission = eligibility(trades, ["3000"], commission=7.0)["3000"]
    assert with_commission["max_stop_distance_usd"] == pytest.approx(14.67 - 0.07, abs=1e-6)


def _trade(r, pnl, stop=10.0, when="2024-03-05T14:00:00+00:00", exit_reason="stop"):
    return {"r": r, "pnl": pnl, "gross": pnl + 1, "costs": 1.0, "cost_r": 0.05, "mfe_r": max(r, 0.5), "mae_r": 1.0,
            "stop_distance": stop, "opened_at": when, "exit_reason": exit_reason, "regime": "up-lowvol",
            "session": "ny_am", "year": int(when[:4]), "weekday": "Tue", "side": "long", "volatility": "mid"}


def test_trade_stats_bootstrap_and_concentration():
    trades = [_trade(2.0, 20.0, exit_reason="take_profit")] * 5 + [_trade(-1.0, -10.0)] * 15
    stats = trade_stats(trades, resamples=500, seed=7, equity=10000)
    assert stats["trades"] == 20 and stats["mean_r"] == pytest.approx(-0.25)
    lo, hi = stats["mean_r_ci95"]
    assert lo <= -0.25 <= hi
    assert stats["net_pnl"] == -50.0 and stats["net_pnl_ex_top5"] == -150.0
    assert stats["longest_losing_streak"] == 15 and stats["exit_reasons"] == {"take_profit": 5, "stop": 15}
    assert breakdown(trades, "session")["ny_am"]["trades"] == 20
    assert set(stop_buckets(trades)) == {"10-15"}


def test_session_buckets_in_new_york_time():
    assert _session(datetime(2026, 7, 7, 12, 30, tzinfo=UTC)) == "ny_am"     # 08:30 EDT
    assert _session(datetime(2026, 7, 7, 8, 0, tzinfo=UTC)) == "london"      # 04:00 EDT
    assert _session(datetime(2026, 1, 13, 23, 0, tzinfo=UTC)) == "asia"      # 18:00 EST
    assert _session(datetime(2026, 1, 13, 18, 0, tzinfo=UTC)) == "ny_pm"     # 13:00 EST


def test_read_ticks_converts_server_time_and_drops_invalid():
    import calendar as stdcal
    from test_phase2_connectors import FakeMt5, connect
    from trading.clock import EET_EU
    from trading.connectors.mt5 import read_ticks

    base = stdcal.timegm(datetime(2026, 11, 6, 16, 0).timetuple()) * 1000  # server wall time (EET = UTC+2)

    class TickFake(FakeMt5):
        def copy_ticks_range(self, name, start, end, flags):
            self.calls.append(("ticks", start, end))
            if start.hour != 16:
                return []
            return [{"time_msc": base, "bid": 4000.0, "ask": 4000.13},
                    {"time_msc": base + 500, "bid": 4000.2, "ask": 4000.1},  # crossed -> dropped
                    {"time_msc": base + 900, "bid": 0.0, "ask": 4000.1}]     # invalid -> dropped

    fake = TickFake()
    conn, _ = connect(fake)
    conn.server_tz = EET_EU
    ticks = read_ticks(conn, "XAUUSD", datetime(2026, 11, 6, 14, tzinfo=UTC), datetime(2026, 11, 6, 20, tzinfo=UTC),
                       chunk=timedelta(hours=2))
    assert ticks == [(datetime(2026, 11, 6, 14, tzinfo=UTC), 4000.0, 4000.13)]
    assert len([c for c in fake.calls if c[0] == "ticks"]) == 3


def test_swap_counts_server_rollovers_with_wednesday_triple():
    from research.phase2c_robustness import swap_usd
    from trading.clock import EET_EU
    swap = {"long_points_per_lot_night": -12.6, "short_points_per_lot_night": -4.6, "usd_per_point_per_lot": 1.0,
            "triple_weekday_server": "Wednesday"}
    tue_evening = datetime(2026, 11, 3, 20, tzinfo=UTC)  # server Tue 22:00
    trade = {"side": "long", "size": D("0.10"), "opened_at": tue_evening, "closed_at": tue_evening + timedelta(hours=1)}
    assert swap_usd(trade, swap, EET_EU) == (0.0, 0)
    trade["closed_at"] = tue_evening + timedelta(days=2)  # crosses Tue->Wed (1) and Wed->Thu (x3)
    cost, nights = swap_usd(trade, swap, EET_EU)
    assert nights == 4 and cost == pytest.approx(0.10 * -12.6 * 4)
    trade["side"] = "short"
    assert swap_usd(trade, swap, EET_EU)[0] == pytest.approx(0.10 * -4.6 * 4)
