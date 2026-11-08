"""Multi-timeframe framework: causality (no look-ahead), resampling, strategies, simulator, hybrid rules.
All market data here is SYNTHETIC."""
from __future__ import annotations

from datetime import timezone
from decimal import Decimal as D

import numpy as np
import pandas as pd
import pytest

from trading.common import Market
from trading.instruments import BinanceSpotSpec, Mt5InstrumentSpec
from trading.mtf.features import atr, confirmed_swings, ema, structure_trend
from trading.mtf.frames import Frame, align, resample
from trading.mtf.simulator import AccountRules, MarketSpec, simulate, summarize
from trading.mtf.strategies import ScalpParams, SelectiveParams, scalp_candidates, selective_candidates
from trading.risk import RiskLimits

UTC = timezone.utc
T0 = pd.Timestamp("2026-03-02 00:00", tz="UTC")
NS_MIN = 60_000_000_000


def walk(n=6000, seed=3, price=70000.0, vol=0.0008, start=T0) -> Frame:
    rng = np.random.default_rng(seed)
    close = price * np.exp(np.cumsum(rng.normal(0.00002, vol, n)))
    open_ = np.concatenate([[price], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, vol / 2, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, vol / 2, n)))
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    return Frame("1m", pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                                     "volume": rng.integers(1, 100, n).astype(float)}, index=idx))


def frames_from(base: Frame) -> dict:
    out = {"1m": base}
    for iv in ("5m", "15m", "1h", "4h"):
        out[iv] = resample(base, iv)
    return out


def test_resample_aggregates_complete_buckets_and_drops_forming_one():
    base = walk(23)
    f5 = resample(base, "5m")
    assert len(f5) == 4  # 23 one-minute candles -> 4 complete 5m buckets; the forming one is dropped
    first = base.df.iloc[:5]
    row = f5.df.iloc[0]
    assert row.open == first.open.iloc[0] and row.close == first.close.iloc[-1]
    assert row.high == first.high.max() and row.low == first.low.min() and not row.partial
    gappy = Frame("1m", base.df.drop(base.df.index[6]))
    assert bool(resample(gappy, "5m").df.partial.iloc[1])


def test_alignment_never_uses_an_unclosed_higher_timeframe_candle():
    base = walk(130)
    f15 = resample(base, "15m")
    idx = align(base, f15)
    for i in range(len(base)):
        if idx[i] >= 0:
            assert f15.close_ns[idx[i]] <= base.close_ns[i]
            if idx[i] + 1 < len(f15):
                assert f15.close_ns[idx[i] + 1] > base.close_ns[i]
    assert idx[13] == -1 and idx[14] == 0  # the first 15m candle becomes visible at the close of minute 15


@pytest.mark.parametrize("fn", [
    lambda h, lo, c: ema(c, 20), lambda h, lo, c: atr(h, lo, c, 14),
    lambda h, lo, c: confirmed_swings(h, lo, 2)[0], lambda h, lo, c: confirmed_swings(h, lo, 2)[1],
    lambda h, lo, c: structure_trend(h, lo, 2).astype(float)])
def test_features_are_prefix_invariant(fn):
    f = walk(800).df
    h, low, c = (f[k].to_numpy() for k in ("high", "low", "close"))
    full = fn(h, low, c)
    for k in (100, 377, 799):
        part = fn(h[:k], low[:k], c[:k])
        np.testing.assert_array_equal(np.nan_to_num(part, nan=-9), np.nan_to_num(full[:k], nan=-9))


@pytest.mark.parametrize("kind", ["A", "B"])
def test_strategy_candidates_are_prefix_invariant(kind):
    base = walk(9000, seed=5)
    gen = (lambda fr: selective_candidates(fr, SelectiveParams(dir_ema=10, struct_k=1, hold_ema=20), allow_short=True)) \
        if kind == "A" else (lambda fr: scalp_candidates(fr, ScalpParams(dir_ema=10), allow_short=True))
    full = gen(frames_from(base))
    assert len(full) > 0
    cut = 6000
    part = gen(frames_from(Frame("1m", base.df.iloc[:cut])))
    cutoff = base.close_ns[cut - 1]
    pd.testing.assert_frame_equal(part.reset_index(drop=True),
                                  full[full.time_ns <= cutoff].reset_index(drop=True), check_exact=False, rtol=1e-12)


# --------------------------------------------------------------------------- simulator

def btc_spec(**kw) -> MarketSpec:
    inst = BinanceSpotSpec(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", tick_size=D("0.01"),
                           step_size=D("0.00001"), min_qty=D("0.00001"), max_qty=D("9000"), min_notional=D("5"),
                           taker_fee_rate=D(kw.pop("fee", "0.001")))
    limits = RiskLimits(market=Market.BTCUSDT_BINANCE_SPOT, max_spread=D("100"), slippage_allowance=D("10"),
                        max_entry_deviation_fraction=D("0.005"))
    return MarketSpec(Market.BTCUSDT_BINANCE_SPOT, inst, limits, lambda t: np.full(len(t), 0.01), "mid",
                      lambda ns: ns // (1440 * NS_MIN), lambda ns: "all", **kw)


def flat_frame(prices, start=T0) -> Frame:
    idx = pd.date_range(start, periods=len(prices), freq="1min", tz="UTC")
    p = np.array(prices, dtype=float)
    return Frame("1m", pd.DataFrame({"open": p[:, 0], "high": p[:, 1], "low": p[:, 2], "close": p[:, 3],
                                     "volume": 1.0}, index=idx))


def cand(minute, side=1, entry=70000.0, stop=69500.0, rr=2.0, min_net=0.0, hold=600, tag="A"):
    return {"time_ns": (T0 + pd.Timedelta(minutes=minute)).value, "side": side, "entry_ref": entry, "stop": stop,
            "rr": rr, "min_net_rr": min_net, "max_hold_minutes": hold, "atr": 500.0, "tag": tag}


def run(rows, prices, spec=None, rules=AccountRules()):
    base = flat_frame(prices)
    return simulate(pd.DataFrame(rows), base, spec or btc_spec(), rules, start_ns=0, end_ns=2**62)


def test_long_entry_target_fees_and_sizing():
    prices = [(70000, 70010, 69990, 70000)] * 3 + [(70000, 71100, 69990, 71050)] + [(71050,) * 4] * 3
    res = run([cand(1)], prices)
    (t,) = res["trades"]
    fill = 70000 + 0.005 + 10  # mid + half spread + slippage
    assert t["entry"] == pytest.approx(fill) and t["exit_reason"] == "take_profit" and t["exit"] == 71000.0
    assert t["planned_risk"] <= 50.0 + 1e-9  # 0.5% of 10,000
    fee = t["size"] * (t["entry"] + t["exit"]) * 0.001
    assert t["pnl"] == pytest.approx(t["size"] * (71000.0 - fill) - fee)


def test_stop_first_and_gap_fill():
    both = [(70000,) * 4, (70000,) * 4, (70000, 71500, 69400, 70000)] + [(70000,) * 4] * 3
    assert run([cand(1)], both)["trades"][0]["exit_reason"] == "stop"
    gap = [(70000,) * 4, (70000,) * 4, (70000,) * 4, (69000, 69100, 68900, 69000)] + [(69000,) * 4] * 2
    t = run([cand(1)], gap)["trades"][0]
    assert t["exit_reason"] == "stop_gap" and t["exit"] == pytest.approx(69000 - 0.005 - 10)


def test_spot_short_rejected_and_net_rr_filter():
    res = run([cand(1, side=-1, stop=70500.0)], [(70000,) * 4] * 5)
    assert not res["trades"] and any("long/flat" in k for k in res["rejections"])
    tiny = run([cand(1, stop=69990.0, rr=2.0, min_net=2.0)], [(70000,) * 4] * 5)  # fees dwarf a 10 USDT stop
    assert not tiny["trades"] and "Net reward-to-risk after costs below minimum" in tiny["rejections"]


def test_daily_latch_blocks_later_entries_the_same_day():
    # first trade gaps far through its stop (>1.5% of equity), second signal later the same day is refused
    prices = [(70000,) * 4] * 3 + [(66000, 66010, 65990, 66000)] + [(66000,) * 4] * 20
    res = run([cand(1, stop=69800.0), cand(10, entry=66000.0, stop=65800.0)], prices)
    assert len(res["trades"]) == 1 and res["trades"][0]["pnl"] < -150
    assert any(k.startswith("Daily loss halt") for k in res["rejections"])


def test_time_exit_and_one_position_rule():
    prices = [(70000, 70050, 69950, 70000)] * 30
    res = run([cand(1, hold=5), cand(3, tag="A", hold=5), cand(8, tag="A", hold=5)], prices)
    assert any(k.startswith("Maximum concurrent") or k.startswith("Conflict - same strategy") for k in res["rejections"])
    assert [t["exit_reason"] for t in res["trades"]] == ["time_exit", "time_exit"]


def test_xau_minimum_lot_and_bid_convention():
    inst = Mt5InstrumentSpec(symbol="XAUUSD", broker_symbol="XAUUSD", account_currency="USD", contract_size=D("100"),
                             point=D("0.01"), tick_size=D("0.01"), tick_value_loss=D("1"), volume_min=D("0.01"),
                             volume_max=D("50"), volume_step=D("0.01"), stops_level_points=0,
                             margin_per_lot=D("4000"), commission_per_lot_round_trip=D("7"))
    limits = RiskLimits(market=Market.XAUUSD_MT5, max_spread=D("1.00"), slippage_allowance=D("0.10"))
    from datetime import timedelta
    from trading.clock import EET_EU
    from trading.sessions import SessionWindow, TradingCalendar
    always = TradingCalendar(symbol="XAUUSD", server_tz=EET_EU, windows=tuple(SessionWindow(d, 0, 1440) for d in range(7)),
                             open_buffer=timedelta(0), close_buffer=timedelta(0))
    spec = MarketSpec(Market.XAUUSD_MT5, inst, limits, lambda t: np.full(len(t), 0.20), "bid",
                      lambda ns: ns // (1440 * NS_MIN), lambda ns: "all", calendar=always)
    no_calendar = MarketSpec(Market.XAUUSD_MT5, inst, limits, lambda t: np.full(len(t), 0.20), "bid",
                             lambda ns: ns // (1440 * NS_MIN), lambda ns: "all")
    prices = [(4000,) * 4] * 3 + [(4000, 4061, 3999, 4060)] + [(4060,) * 4] * 3
    ok = run([cand(1, entry=4000.0, stop=3970.0)], prices, spec)
    assert ok["trades"][0]["entry"] == pytest.approx(4000.30)  # bid candle + 0.20 spread + 0.10 slippage
    small = run([cand(1, entry=4000.0, stop=3970.0)], prices, spec, AccountRules(initial_equity=3000))
    refused = run([cand(1, entry=4000.0, stop=3970.0)], prices, no_calendar)
    assert not refused["trades"] and any("session calendar required" in k for k in refused["rejections"])
    assert not small["trades"] and any(k.startswith("Minimum size") for k in small["rejections"])


def test_hybrid_priority_conflict_and_scalp_share_cap():
    prices = [(70000, 70020, 69980, 70000)] * 200
    rows = [cand(1, tag="B", hold=3), cand(1, tag="A", hold=3), cand(10, tag="B", hold=3), cand(20, tag="B", hold=3)]
    res = run(rows, prices, rules=AccountRules(max_open_positions=2, max_scalp_share=0.5))
    tags = [t["tag"] for t in res["trades"]]
    assert tags[0] == "A" and tags.count("B") >= 1  # A wins the tie; B fills while share allows
    assert "Scalp share cap" in res["rejections"]
    s = summarize(res)
    assert s["trades"] == len(res["trades"]) and s["max_drawdown_pct"] >= 0


# --------------------------------------------------------------------------- advisory interfaces (research only)

def test_advisory_inputs_are_recorded_but_cannot_change_decisions():
    import inspect
    from trading.agents.mtf_advisory import GrokNotActivated, InactiveGrokAgent, RecordedKronos, annotate
    prices = [(70000, 70010, 69990, 70000)] * 3 + [(70000, 71100, 69990, 71050)] + [(71050,) * 4] * 3
    rows = pd.DataFrame([cand(1)])
    agent = InactiveGrokAgent("skeptic", recorded={int(rows.time_ns[0]): {"verdict": "reject everything"}})
    annotated = annotate(rows, [RecordedKronos({int(rows.time_ns[0]): 1.0}), agent])
    assert len(annotated.advisory[0]) == 2 and "advisory" not in rows
    plain = simulate(rows, flat_frame(prices), btc_spec(), AccountRules(), start_ns=0, end_ns=2**62)
    advised = simulate(annotated, flat_frame(prices), btc_spec(), AccountRules(), start_ns=0, end_ns=2**62)
    assert plain["trades"] == advised["trades"]  # advice is never read by the simulator / risk engine
    assert "advis" not in str(inspect.signature(simulate)).lower()
    with pytest.raises(GrokNotActivated):
        agent.live_request("anything")
    with pytest.raises(ValueError):
        InactiveGrokAgent("skeptic", base_url="https://api.other-provider.example")
    with pytest.raises(ValueError):
        InactiveGrokAgent("trader")


def test_gross_is_mid_to_mid_and_costs_include_spread_slippage_and_fees():
    prices = [(70000, 70010, 69990, 70000)] * 3 + [(70000, 71100, 69990, 71050)] + [(71050,) * 4] * 3
    (t,) = run([cand(1)], prices)["trades"]
    assert t["entry_mid"] == pytest.approx(70000.0) and t["exit_mid"] == pytest.approx(71000.0 + 0.005)
    fee = t["size"] * (t["entry"] + t["exit"]) * 0.001
    spread_slip = t["size"] * ((t["entry"] - t["entry_mid"]) + (t["exit_mid"] - t["exit"]))
    assert t["costs"] == pytest.approx(fee + spread_slip) and t["gross"] - t["costs"] == pytest.approx(t["pnl"])


def test_bulk_archive_checksums_microseconds_and_gaps(tmp_path):
    import hashlib
    import io
    import zipfile
    from research.binance_bulk import download
    from research.phase2e_btc_history import load_rows

    def archive(name, lines):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(name.replace(".zip", ".csv"), "\n".join(lines))
        return buf.getvalue()

    ms = 1735689600000  # 2025-01-01T00:00Z
    jan = [f"{ms + k * 60000},1,2,0.5,1.5,10,0,15,0,0,0,0" for k in range(3)]
    feb_start = ms + 31 * 1440 * 60000
    feb = [f"{(feb_start + k * 60000) * 1000},1,2,0.5,1.5,10,0,15,0,0,0,0" for k in (0, 2)]  # microseconds + 1 gap
    blobs = {"BTCUSDT-1m-2025-01.zip": archive("BTCUSDT-1m-2025-01.zip", jan),
             "BTCUSDT-1m-2025-02.zip": archive("BTCUSDT-1m-2025-02.zip", feb)}

    def fetch(url):
        name = url.rsplit("/", 1)[1]
        if name.endswith(".CHECKSUM"):
            return f"{hashlib.sha256(blobs[name[:-9]]).hexdigest()}  {name[:-9]}".encode()
        return blobs[name]
    out = tmp_path / "k.csv"
    info = download("BTCUSDT", "1m", "2025-01", "2025-02", out, fetch=fetch)
    assert info["row_count"] == 5 and info["gaps"] == 2  # Jan->Feb (no data in between) and the Feb missing minute
    rows, manifest = load_rows(out)  # checksum + recorded-gap verification
    assert rows[3]["timestamps"].startswith("2025-02-01T00:00:00")
    tampered = dict(blobs)
    blobs["BTCUSDT-1m-2025-02.zip"] = archive("BTCUSDT-1m-2025-02.zip", feb[:1])
    with pytest.raises(ValueError, match="checksum"):
        download("BTCUSDT", "1m", "2025-01", "2025-02", tmp_path / "x.csv",
                 fetch=lambda u: tampered[u.rsplit("/", 1)[1]] if not u.endswith(".CHECKSUM") else fetch(u))
